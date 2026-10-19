// ===== 小道具 =====
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const log = (html) => { $("log").innerHTML = html; };

let checked = null;   // 直近のチェック結果
let checkedUrl = "";  // チェックしたURL(変更されたら再チェックが必要)

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) {
    const detail = Array.isArray(data.detail) ? "入力内容を確認してください" : data.detail;
    throw new Error(detail || r.statusText);
  }
  return data;
}

function formatSize(bytes) {
  if (!bytes) return "";
  const mb = bytes / 1024 / 1024;
  return mb >= 1024 ? `約${(mb / 1024).toFixed(1)}GB` : `約${Math.max(1, Math.round(mb))}MB`;
}

function formatDuration(sec) {
  if (!sec && sec !== 0) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function setBusy(busy) {
  for (const el of document.querySelectorAll("#checkForm button, #dlForm button, #dlForm input, #dlForm select")) {
    if (busy) {
      el.dataset.wasDisabled = el.disabled ? "1" : "";
      el.disabled = true;
    } else {
      el.disabled = el.dataset.wasDisabled === "1";
    }
  }
  if (!busy) updateConstraints();
}

// ===== ダークモード切り替え =====
function systemPrefersDark() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function isDark() {
  const saved = document.documentElement.getAttribute("data-theme");
  return saved ? saved === "dark" : systemPrefersDark();
}

$("themeToggle").addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("theme", next); } catch {}
});

// ===== バージョン表示 =====
fetch("/api/health").then((r) => r.json()).then((d) => {
  $("ver").textContent = `yt-dlp ${d.yt_dlp_version}`;
}).catch(() => {});

// ===== ① チェック =====
$("checkForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  $("result").hidden = true;
  $("bar").hidden = true;
  log("確認中…");
  $("checkBtn").disabled = true;
  try {
    const d = await post("/api/check", { url });
    if (!d.ok) {
      log(`<span class="ng">✘ 取得できませんでした${d.blocked ? "(ブロックの可能性が高い)" : ""}</span>\n${esc(d.error)}`);
      return;
    }
    checked = d;
    checkedUrl = url;
    renderResult();
    log(`<span class="ok">✔ 取得できました。</span>形式を選んでダウンロードしてください。`);
  } catch (err) {
    log(`<span class="ng">エラー:</span> ${esc(err.message)}`);
  } finally {
    $("checkBtn").disabled = false;
  }
});

// URLを書き換えたら、オプションを隠して再チェックを促す
$("url").addEventListener("input", () => {
  if (checked && $("url").value.trim() !== checkedUrl) {
    $("result").hidden = true;
    log("URLが変わりました。もう一度「チェック」を押してください。");
  }
});

function renderResult() {
  const d = checked;
  $("thumb").src = d.thumbnail || "";
  $("thumb").hidden = !d.thumbnail;
  $("title").textContent = d.title || "(タイトル不明)";
  const meta = [d.uploader, formatDuration(d.duration)];
  if (d.playlist_only && d.playlist) meta.push(`再生リスト ${d.playlist.count}本`);
  $("meta").textContent = meta.filter(Boolean).join(" ・ ");

  // 字幕
  const subLabel = (name, kind) => kind === "auto" ? `${name}(自動生成)` : kind ? name : `${name}(なし)`;
  for (const [lang, name] of [["ja", "日本語"], ["en", "英語"]]) {
    const box = document.querySelector(`input[name=sub][value=${lang}]`);
    box.checked = false;
    box.disabled = !d.subtitles[lang];
    $(lang === "ja" ? "subJaLabel" : "subEnLabel").textContent = subLabel(name, d.subtitles[lang]);
  }

  // 再生リスト
  $("playlistBox").hidden = !d.playlist;
  $("playlist").checked = !!d.playlist_only;
  $("playlist").dataset.locked = d.playlist_only ? "1" : "";
  if (d.playlist) {
    $("playlistLabel").textContent = `再生リストをまとめてダウンロード(${d.playlist.count}本)`;
    $("playlistHint").textContent = d.playlist.count > d.playlist.max
      ? `本数が多いため、先頭の${d.playlist.max}本までをダウンロードします。`
      : "1本ずつ間をあけてダウンロードするので、時間がかかります。";
  }

  // そのほかの初期化
  $("start").value = "";
  $("end").value = "";
  $("splitChapters").checked = false;
  $("sponsorblock").checked = false;

  renderQualities();
  renderAudioSize();
  updateConstraints();
  $("result").hidden = false;
}

function renderQualities() {
  const compat = $("compat").checked;
  const sel = $("quality");
  const prev = sel.value;
  sel.innerHTML = "";

  const qs = checked.qualities || [];
  const sizeOf = (q) => (compat && q.has_h264 ? q.size_compat : q.size);
  const best = compat ? qs.find((q) => q.has_h264) || qs[0] : qs[0];

  const top = document.createElement("option");
  top.value = "";
  top.textContent = `最高画質${best ? `(${best.quality}p ${formatSize(sizeOf(best))})` : ""}`;
  sel.appendChild(top);

  for (const q of qs) {
    const opt = document.createElement("option");
    opt.value = q.quality;
    const note = compat && !q.has_h264 ? " ※互換形式なし" : "";
    opt.textContent = `${q.quality}p ${formatSize(sizeOf(q))}${note}`.trim();
    sel.appendChild(opt);
  }
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

function renderAudioSize() {
  if (!checked) return;
  const size = $("audioFormat").value === "m4a" ? checked.audio_size_aac || checked.audio_size : checked.audio_size;
  $("audioSize").textContent = size ? `ファイルサイズ: ${formatSize(size)}(1本あたりの目安)` : "";
}

// ===== オプション同士の組み合わせ =====
function currentMode() {
  return document.querySelector("input[name=mode]:checked").value;
}

function updateConstraints() {
  if (!checked) return;
  const mode = currentMode();
  $("videoOpts").hidden = mode !== "video";
  $("audioOpts").hidden = mode !== "audio";
  $("subtitleField").hidden = mode !== "video";
  $("audioQualityField").hidden = $("audioFormat").value !== "mp3";


  const playlist = $("playlist").checked;
  const hasRange = $("start").value.trim() !== "" || $("end").value.trim() !== "";

  // 再生リストのURLしかない場合は、再生リストのチェックを外せない
  $("playlist").disabled = $("playlist").dataset.locked === "1";

  // 再生リストでは区間指定は使えない
  $("start").disabled = playlist;
  $("end").disabled = playlist;

  // 区間指定中はチャプター分割は使えない。チャプターがない動画も不可(再生リストは動画ごとに判断)
  const chaptersAvailable = playlist || checked.chapters > 0;
  $("splitChapters").disabled = hasRange || !chaptersAvailable;
  if ($("splitChapters").disabled) $("splitChapters").checked = false;
  $("chaptersLabel").textContent = !chaptersAvailable
    ? "チャプターごとに別ファイルに分割(この動画にはチャプターがありません)"
    : playlist
      ? "チャプターごとに別ファイルに分割(チャプターがある動画のみ)"
      : `チャプターごとに別ファイルに分割(${checked.chapters}個)`;
}

for (const el of document.querySelectorAll("input[name=mode]")) el.addEventListener("change", updateConstraints);
$("audioFormat").addEventListener("change", () => { updateConstraints(); renderAudioSize(); });
$("compat").addEventListener("change", renderQualities);
$("playlist").addEventListener("change", updateConstraints);
$("start").addEventListener("input", updateConstraints);
$("end").addEventListener("input", updateConstraints);

// ===== ② ダウンロード =====
$("dlForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!checked) return;

  const mode = currentMode();
  const body = {
    url: checkedUrl,
    mode,
    quality: $("quality").value ? Number($("quality").value) : null,
    compat: $("compat").checked,
    audio_format: $("audioFormat").value,
    audio_quality: Number($("audioQuality").value),
    subtitles: mode === "video"
      ? [...document.querySelectorAll("input[name=sub]:checked")].map((b) => b.value)
      : [],
    start: $("start").disabled ? null : $("start").value.trim() || null,
    end: $("end").disabled ? null : $("end").value.trim() || null,
    split_chapters: $("splitChapters").checked,
    playlist: $("playlist").checked,
    sponsorblock: $("sponsorblock").checked,
  };

  log("ダウンロードを開始しています…");
  $("bar").hidden = false;
  $("bar").value = 0;
  setBusy(true);

  try {
    const { job_id } = await post("/api/download", body);
    pollJob(job_id);
  } catch (err) {
    setBusy(false);
    $("bar").hidden = true;
    log(`<span class="ng">エラー:</span> ${esc(err.message)}`);
  }
});

function describeProgress(j) {
  const item = j.item ? `${j.item}本目 ` : "";
  switch (j.status) {
    case "queued":
      return j.ahead ? `順番待ち中…(前に${j.ahead}件)` : "順番待ち中…(まもなく開始します)";
    case "starting":
      return "準備中…";
    case "downloading": {
      const pct = j.progress != null ? `${j.progress}%` : "";
      const speed = j.speed ? `${(j.speed / 1024 / 1024).toFixed(1)}MB/s` : "";
      const eta = j.eta != null ? `残り${formatDuration(j.eta)}` : "";
      const extra = [speed, eta].filter(Boolean).join("・");
      return `${item}${j.stream || ""}をダウンロード中… ${pct}${extra ? `(${extra})` : ""}`;
    }
    case "processing":
      return `${item}${j.step || "後処理中…"}`;
    default:
      return "";
  }
}

function pollJob(jobId) {
  const timer = setInterval(async () => {
    let j;
    try {
      j = await (await fetch(`/api/jobs/${jobId}`)).json();
    } catch {
      return; // 一時的な通信エラーは次の確認で再試行
    }

    if (j.progress != null) $("bar").value = j.progress;

    if (j.status === "done") {
      clearInterval(timer);
      setBusy(false);
      const fileUrl = `/api/jobs/${jobId}/file`;
      const count = j.count > 1 ? `(${j.count}ファイルをzipにまとめました)` : "";
      const warn = j.errors && j.errors.length
        ? `\n<span class="ng">一部うまくいかなかった処理があります:</span>\n${j.errors.map(esc).join("\n")}`
        : "";
      const expires = j.expires
        ? `\n<span class="muted">${new Date(j.expires * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })} まで保存できます(以降は自動で削除されます)</span>`
        : "";
      log(`<span class="ok">✔ 完了</span> ${formatSize(j.size)}${count}\n<a href="${fileUrl}">${esc(j.filename)} を保存</a>(自動的に保存が始まらない場合はここをクリック)${expires}${warn}`);

      // 保存ボタンを押さなくて済むよう、完了したら自動でダウンロードを開始する
      const a = document.createElement("a");
      a.href = fileUrl;
      a.download = j.filename || "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      return;
    }

    if (j.status === "error" || j.detail) {
      clearInterval(timer);
      setBusy(false);
      $("bar").hidden = true;
      log(`<span class="ng">✘ 失敗${j.blocked ? "(ブロックの可能性が高い)" : ""}</span>\n${esc(j.error || j.detail)}`);
      return;
    }

    log(describeProgress(j));
  }, 1000);
}
