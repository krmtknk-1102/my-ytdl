"""
my-ytdl: yt-dlp を使った家庭用ダウンロードサーバー

API
- GET  /api/health          : yt-dlp のバージョンなど
- POST /api/check           : 動画情報の取得(画質・字幕・チャプター・再生リストの有無)
- POST /api/download        : ダウンロードジョブの開始
- GET  /api/jobs/{id}       : 進捗確認
- GET  /api/jobs/{id}/file  : 完成ファイルの受け取り(複数ファイルなら zip)
"""
import logging
import os
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from yt_dlp.utils import download_range_func, parse_duration, sanitize_filename

# ===== 設定(環境変数で変更可) =====
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "30"))   # 再生リストの上限本数
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))  # 同時に動かすジョブ数
MAX_QUEUED_JOBS = int(os.getenv("MAX_QUEUED_JOBS", "5"))          # 順番待ちの上限件数
RETENTION_HOURS = float(os.getenv("RETENTION_HOURS", "6"))        # ファイルを残す時間
CLEANUP_INTERVAL_MINUTES = float(os.getenv("CLEANUP_INTERVAL_MINUTES", "60"))  # 自動掃除の間隔
MIN_FREE_GB = float(os.getenv("MIN_FREE_GB", "5"))                # 空きがこれを下回ったら受け付けない

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}

# ブロックされたときに yt-dlp が出しがちなメッセージ
BLOCK_KEYWORDS = [
    "sign in to confirm", "not a bot", "http error 429",
    "too many requests", "this content isn't available",
]

# SponsorBlock でカットする区間の種類
SPONSOR_CATEGORIES = ["sponsor", "selfpromo", "interaction"]

# 後処理の名前 → 画面に出す日本語
PP_LABELS = {
    "Merger": "映像と音声を結合中…",
    "FFmpegExtractAudio": "音声を変換中…",
    "SponsorBlock": "宣伝区間を確認中…",
    "ModifyChapters": "宣伝区間をカット中…",
    "FFmpegSubtitlesConvertor": "字幕を変換中…",
    "FFmpegEmbedSubtitle": "字幕を埋め込み中…",
    "FFmpegSplitChapters": "チャプターごとに分割中…",
}

# ===== ログ =====
# docker compose logs で見られるよう、標準出力に時刻付きで出す
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("my-ytdl")


class SkipPollingFilter(logging.Filter):
    """画面が1秒ごとに呼ぶ進捗確認のアクセスログは多すぎるので出さない"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ('"GET /api/jobs/' in msg and "/file" not in msg) and '"GET /api/health' not in msg


logging.getLogger("uvicorn.access").addFilter(SkipPollingFilter())

ACTIVE_STATUSES = ("queued", "starting", "downloading", "processing")

app = FastAPI(title="my-ytdl")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
job_slots = threading.Semaphore(MAX_CONCURRENT_JOBS)


# ===== リクエストの形 =====
class CheckRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    mode: Literal["video", "audio"] = "video"
    quality: Optional[int] = Field(None, description="画質の上限(短辺のピクセル数)。None なら最高画質")
    compat: bool = True                                   # H.264/AAC を優先
    audio_format: Literal["mp3", "m4a"] = "mp3"
    audio_quality: Literal[128, 192, 320] = 192           # mp3 のビットレート
    subtitles: list[Literal["ja", "en"]] = []
    start: Optional[str] = None                           # 区間指定 例 "1:30"
    end: Optional[str] = None
    split_chapters: bool = False
    playlist: bool = False
    sponsorblock: bool = False


# ===== 共通の小道具 =====
def validate_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_HOSTS:
        raise HTTPException(400, "YouTubeのURLを入力してください")
    return url


def playlist_id_of(url: str) -> Optional[str]:
    return (parse_qs(urlparse(url).query).get("list") or [None])[0]


def is_playlist_only(url: str) -> bool:
    """動画IDを含まない、再生リストだけのURLか"""
    return urlparse(url).path.rstrip("/") == "/playlist"


def base_opts() -> dict:
    return {"noplaylist": True, "quiet": True, "no_warnings": True}


def looks_blocked(message: str) -> bool:
    m = message.lower()
    return any(k in m for k in BLOCK_KEYWORDS)


def parse_time(value: Optional[str], label: str) -> Optional[float]:
    if value is None or not value.strip():
        return None
    seconds = parse_duration(value.strip())
    if seconds is None:
        raise HTTPException(400, f"{label}の形式が正しくありません(例: 1:30 や 1:02:03)")
    return seconds


def remove_path(p: Path):
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def cleanup_old_jobs():
    """保存期間を過ぎたファイルとジョブ情報を消す(実行中のジョブには触らない)"""
    limit = time.time() - RETENTION_HOURS * 3600
    with jobs_lock:
        active = {jid for jid, j in jobs.items() if j.get("status") in ACTIVE_STATUSES}
    removed = 0
    for p in DOWNLOAD_DIR.iterdir():
        if p.name in active:
            continue
        try:
            if p.stat().st_mtime < limit:
                remove_path(p)
                removed += 1
        except FileNotFoundError:
            pass
    with jobs_lock:
        for jid in [jid for jid, j in jobs.items()
                    if jid not in active and j.get("created", 0) < limit]:
            jobs.pop(jid, None)
    if removed:
        log.info("cleanup: removed %d old item(s), free %.1fGB", removed, free_gb())


def cleanup_loop():
    while True:
        try:
            cleanup_old_jobs()
        except Exception:
            log.exception("cleanup failed")
        time.sleep(CLEANUP_INTERVAL_MINUTES * 60)


def free_gb() -> float:
    return shutil.disk_usage(DOWNLOAD_DIR).free / 1024 ** 3


# ===== 動画情報の解析 =====
def fsize(f: dict) -> Optional[int]:
    return f.get("filesize") or f.get("filesize_approx")


def short_side(f: dict) -> Optional[int]:
    """縦長動画(ショート)でも 1080p などと表示できるよう、短い辺を画質とみなす"""
    w, h = f.get("width"), f.get("height")
    if w and h:
        return min(w, h)
    return h


def best_by_bitrate(formats: list[dict]) -> Optional[dict]:
    return max(formats, key=lambda f: f.get("tbr") or 0, default=None)


def summarize_qualities(info: dict) -> tuple[list[dict], Optional[int], Optional[int]]:
    """選べる画質と、おおよそのファイルサイズを返す"""
    formats = info.get("formats") or []
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and short_side(f)]
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]

    best_audio = best_by_bitrate(audios)
    best_aac = best_by_bitrate([a for a in audios if (a.get("acodec") or "").startswith("mp4a")])

    def total(video: Optional[dict], audio: Optional[dict]) -> Optional[int]:
        if not video or not fsize(video):
            return None
        has_audio = video.get("acodec") not in (None, "none")
        if has_audio:
            return fsize(video)
        return fsize(video) + (fsize(audio) or 0) if audio else fsize(video)

    qualities = []
    for q in sorted({short_side(v) for v in videos}, reverse=True):
        same = [v for v in videos if short_side(v) == q]
        h264 = [v for v in same if (v.get("vcodec") or "").startswith("avc1")]
        qualities.append({
            "quality": q,
            "size": total(best_by_bitrate(same), best_audio),
            "size_compat": total(best_by_bitrate(h264), best_aac or best_audio) if h264 else None,
            "has_h264": bool(h264),
        })
    return qualities, (fsize(best_audio) if best_audio else None), (fsize(best_aac) if best_aac else None)


def summarize_subtitles(info: dict) -> dict:
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    result = {}
    for lang in ("ja", "en"):
        if lang in manual:
            result[lang] = "manual"
        elif lang in auto:
            result[lang] = "auto"
        else:
            result[lang] = None
    return result


@app.get("/api/health")
def health():
    with jobs_lock:
        statuses = [j.get("status") for j in jobs.values()]
    return {
        "yt_dlp_version": yt_dlp.version.__version__,
        "active_jobs": sum(s in ACTIVE_STATUSES for s in statuses),  # 更新スクリプトが使う
        "queued_jobs": statuses.count("queued"),
        "free_gb": round(free_gb(), 1),
        "retention_hours": RETENTION_HOURS,
    }


@app.post("/api/check")
def check(req: CheckRequest):
    url = validate_url(req.url)
    list_id = playlist_id_of(url)

    try:
        # 再生リストの概要(本数とタイトル)だけを軽く取得
        playlist = None
        first_entry_url = None
        if list_id:
            try:
                flat_opts = base_opts() | {
                    "extract_flat": "in_playlist",
                    "noplaylist": False,
                    "playlistend": MAX_PLAYLIST_ITEMS,
                }
                with yt_dlp.YoutubeDL(flat_opts) as ydl:
                    pl = ydl.extract_info(f"https://www.youtube.com/playlist?list={list_id}", download=False)
                entries = [e for e in (pl.get("entries") or []) if e]
                playlist = {
                    "title": pl.get("title"),
                    "count": pl.get("playlist_count") or len(entries),
                    "max": MAX_PLAYLIST_ITEMS,
                }
                if entries:
                    first_entry_url = entries[0].get("url")
            except Exception:
                if is_playlist_only(url):
                    raise
                playlist = None  # 動画URLに付いた list= の取得失敗は無視して、動画だけ扱う

        # 画質などの詳細は1本分だけ取得(再生リストだけのURLなら先頭の動画)
        target = url
        if is_playlist_only(url):
            if not first_entry_url:
                raise RuntimeError("再生リストに動画が見つかりません")
            target = first_entry_url

        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = ydl.extract_info(target, download=False)

        qualities, audio_size, audio_size_aac = summarize_qualities(info)
        log.info("check ok: %s (%s)", info.get("title"), url)
        return {
            "ok": True,
            "title": playlist["title"] if is_playlist_only(url) and playlist else info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "qualities": qualities,
            "audio_size": audio_size,
            "audio_size_aac": audio_size_aac,
            "subtitles": summarize_subtitles(info),
            "chapters": len(info.get("chapters") or []),
            "playlist": playlist,
            "playlist_only": is_playlist_only(url),
        }
    except Exception as e:
        msg = str(e)
        blocked = looks_blocked(msg)
        log.warning("check failed%s: %s | %s", " (BLOCKED?)" if blocked else "", url, msg)
        return {"ok": False, "blocked": blocked, "error": msg}


# ===== ダウンロード処理 =====
class JobLogger:
    """yt-dlp のエラーをジョブに記録する(字幕だけ失敗した、などを画面に出すため)"""

    def __init__(self, job_id: str):
        self.job_id = job_id

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.warning("[%s] %s", self.job_id, msg)

    def error(self, msg):
        log.error("[%s] %s", self.job_id, msg)
        with jobs_lock:
            errors = jobs[self.job_id].setdefault("errors", [])
            if len(errors) < 20:
                errors.append(str(msg).replace("ERROR: ", "", 1))


def build_options(req: DownloadRequest, job_id: str, job_dir: Path) -> dict:
    start = parse_time(req.start, "開始時刻")
    end = parse_time(req.end, "終了時刻")

    base = "%(playlist_index)03d %(title).80s" if req.playlist else "%(title).80s"
    opts = base_opts() | {
        "outtmpl": {
            "default": str(job_dir / f"{base}.%(ext)s"),
            "chapter": str(job_dir / base / "%(section_number)02d %(section_title).80s.%(ext)s"),
        },
        "logger": JobLogger(job_id),
        "ignoreerrors": "only_download",  # 字幕の失敗などで全体を止めない
    }

    # --- 再生リスト ---
    if req.playlist:
        opts |= {
            "noplaylist": False,
            "playlistend": MAX_PLAYLIST_ITEMS,
            "sleep_interval": 2,          # 連続アクセスでブロックされないよう、少し間をあける
            "max_sleep_interval": 6,
        }

    # --- 画質・形式 ---
    postprocessors = []
    if req.sponsorblock:
        postprocessors.append({"key": "SponsorBlock", "categories": SPONSOR_CATEGORIES, "when": "after_filter"})

    if req.mode == "video":
        sort = []
        if req.compat:
            sort += ["vcodec:h264"]
        if req.quality:
            sort += [f"res:{req.quality}"]
        if req.compat:
            sort += ["acodec:aac"]
        opts |= {"format": "bv*+ba/b", "format_sort": sort, "merge_output_format": "mp4"}

        if req.subtitles:
            opts |= {
                "writesubtitles": True,
                "writeautomaticsub": True,   # 手動字幕がなければ自動生成字幕
                "subtitleslangs": req.subtitles,
            }
            postprocessors.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
    else:
        if req.audio_format == "m4a":
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "m4a"})
        else:
            opts["format"] = "bestaudio/best"
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(req.audio_quality),
            })

    if req.sponsorblock:
        postprocessors.append({"key": "ModifyChapters", "remove_sponsor_segments": SPONSOR_CATEGORIES})
    if req.mode == "video" and req.subtitles:
        postprocessors.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})
    if req.split_chapters:
        postprocessors.append({"key": "FFmpegSplitChapters", "force_keyframes": False})

    # --- 区間指定 ---
    if start is not None or end is not None:
        s, e = start or 0, end if end is not None else float("inf")
        if s >= e:
            raise HTTPException(400, "終了時刻は開始時刻より後にしてください")
        opts |= {
            "download_ranges": download_range_func(None, [(s, e)]),
            "force_keyframes_at_cuts": True,  # 指定どおりの位置で切る(少し時間がかかる)
        }

    opts["postprocessors"] = postprocessors
    return opts


def collect_outputs(job_dir: Path, split_chapters: bool) -> list[Path]:
    files = [
        p for p in job_dir.rglob("*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl", ".zip"))
    ]
    if split_chapters:
        # 分割できた動画は、分割前の元ファイルを除く
        files = [
            p for p in files
            if not (p.parent == job_dir and (job_dir / p.stem).is_dir())
        ]
    return sorted(files)


def make_hooks(job_id: str):
    def progress_hook(d):
        info = d.get("info_dict") or {}
        with jobs_lock:
            job = jobs[job_id]
            if info.get("playlist_index") and info.get("n_entries"):
                job["item"] = f"{info['playlist_index']}/{info['n_entries']}"
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                job["status"] = "downloading"
                job["progress"] = round(done / total * 100, 1) if total else None
                job["stream"] = "音声" if info.get("vcodec") == "none" else "映像"
                job["speed"] = d.get("speed")
                job["eta"] = d.get("eta")
            elif d["status"] == "finished":
                job["status"] = "processing"
                job["step"] = "後処理中…"

    def pp_hook(d):
        if d["status"] == "started":
            with jobs_lock:
                jobs[job_id]["status"] = "processing"
                jobs[job_id]["step"] = PP_LABELS.get(d.get("postprocessor"), "後処理中…")

    return progress_hook, pp_hook


def run_job(job_id: str, req: DownloadRequest, url: str, opts: dict):
    job_dir = DOWNLOAD_DIR / job_id
    progress_hook, pp_hook = make_hooks(job_id)
    opts |= {"progress_hooks": [progress_hook], "postprocessor_hooks": [pp_hook]}

    with job_slots:  # 同時実行数の制限。空きがなければここで待つ
        with jobs_lock:
            jobs[job_id]["status"] = "starting"
        started = time.time()
        log.info("[%s] start", job_id)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

            files = collect_outputs(job_dir, req.split_chapters)
            if not files:
                with jobs_lock:
                    errors = jobs[job_id].get("errors") or []
                raise RuntimeError(errors[-1] if errors else "出力ファイルが見つかりません")

            if len(files) == 1:
                output = files[0]
                if output.parent != job_dir:  # チャプターが1つだけ、などでサブフォルダ内にある場合
                    output = output.rename(job_dir / output.name)
            else:
                name = sanitize_filename((info or {}).get("title") or "download") or "download"
                output = job_dir / f"{name}.zip"
                with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as z:  # 動画は圧縮済みなので無圧縮でまとめる
                    for f in files:
                        z.write(f, f.relative_to(job_dir))
            # 受け渡すファイル以外(zip化した元ファイル、分割前の動画、作業用ファイル)は消す
            for p in list(job_dir.iterdir()):
                if p != output:
                    remove_path(p)

            with jobs_lock:
                jobs[job_id] |= {
                    "status": "done", "progress": 100, "file": str(output),
                    "count": len(files), "size": output.stat().st_size,
                    "expires": time.time() + RETENTION_HOURS * 3600,  # 画面に「○時まで保存できます」と出す
                }
            log.info(
                "[%s] done: %s (%d files, %.1fMB, %ds)",
                job_id, output.name, len(files), output.stat().st_size / 1024 / 1024, time.time() - started,
            )
        except Exception as e:
            msg = str(e).replace("ERROR: ", "", 1)
            blocked = looks_blocked(msg)
            remove_path(job_dir)  # 途中までのファイルは受け取れないので、すぐ消す
            with jobs_lock:
                jobs[job_id] |= {"status": "error", "error": msg, "blocked": blocked}
            log.error("[%s] failed%s after %ds: %s", job_id, " (BLOCKED?)" if blocked else "", time.time() - started, msg)


@app.post("/api/download")
def download(req: DownloadRequest):
    url = validate_url(req.url)

    # --- 組み合わせのチェック ---
    has_range = bool((req.start or "").strip() or (req.end or "").strip())
    list_id = playlist_id_of(url)
    if req.playlist and not list_id:
        raise HTTPException(400, "このURLは再生リストではありません")
    if is_playlist_only(url) and not req.playlist:
        raise HTTPException(400, "再生リストのURLです。「再生リストをまとめてダウンロード」を選んでください")
    if req.playlist and has_range:
        raise HTTPException(400, "再生リストでは区間指定は使えません")
    if req.split_chapters and has_range:
        raise HTTPException(400, "区間指定とチャプター分割は同時に使えません")

    # --- 空き容量チェック(VPS のディスクが一杯になると他のサイトも止まるため) ---
    if free_gb() < MIN_FREE_GB:
        cleanup_old_jobs()
        if free_gb() < MIN_FREE_GB:
            log.warning("rejected (low disk: %.1fGB free): %s", free_gb(), url)
            raise HTTPException(507, "サーバーの空き容量が不足しています。しばらく待ってからもう一度お試しください")

    # --- 混雑チェック(実行中 + 順番待ちが上限を超えたら受け付けない) ---
    with jobs_lock:
        active = sum(j.get("status") in ACTIVE_STATUSES for j in jobs.values())
    if active >= MAX_CONCURRENT_JOBS + MAX_QUEUED_JOBS:
        log.warning("rejected (busy: %d jobs): %s", active, url)
        raise HTTPException(429, "混み合っています。しばらく待ってからもう一度お試しください")

    job_id = uuid.uuid4().hex[:12]
    job_dir = DOWNLOAD_DIR / job_id
    opts = build_options(req, job_id, job_dir)  # 時刻の形式エラーはここで 400 になる
    job_dir.mkdir(parents=True, exist_ok=True)

    target = f"https://www.youtube.com/playlist?list={list_id}" if req.playlist else url
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "created": time.time()}
    summary = {k: v for k, v in req.model_dump().items() if k != "url" and v not in (None, False, [], "")}
    log.info("[%s] queued: %s %s", job_id, target, summary)
    threading.Thread(target=run_job, args=(job_id, req, target, opts), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with jobs_lock:
        job = dict(jobs.get(job_id) or {})
    if not job:
        raise HTTPException(404, "ジョブが見つかりません")
    if job.get("status") == "queued":
        with jobs_lock:
            job["ahead"] = sum(
                1 for j in jobs.values()
                if j.get("status") == "queued" and j.get("created", 0) < job.get("created", 0)
            )
    if job.get("file"):
        job["filename"] = Path(job.pop("file")).name
    return job


@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "ファイルはまだありません")
    path = Path(job["file"])
    if not path.is_file():
        raise HTTPException(404, "ファイルは保存期間を過ぎたため削除されました")
    return FileResponse(path, filename=path.name)


# 起動時に1回、その後は一定間隔で古いファイルを掃除する
threading.Thread(target=cleanup_loop, daemon=True, name="cleanup").start()

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")