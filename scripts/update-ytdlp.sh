#!/bin/sh
# yt-dlp を最新版にするため、イメージを作り直して入れ替える(サーバーの cron から実行する想定)。
# ダウンロード中のジョブがあるときは、途中で消えないよう何もしない。

cd "$(dirname "$0")/.." || exit 1
PORT="${PORT:-8083}"
now() { date "+%Y-%m-%d %H:%M:%S"; }

# 実行中のジョブがあれば見送る(アプリが応答しないときは、そのまま作り直す)
health=$(curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null)
active=$(echo "$health" | sed -n 's/.*"active_jobs":\([0-9]*\).*/\1/p')
if [ "${active:-0}" -gt 0 ]; then
  echo "$(now) skipped: ${active} job(s) running"
  exit 0
fi

echo "$(now) rebuilding to update yt-dlp"
# ビルドに失敗したときは、今のコンテナをそのまま動かし続ける
if docker compose build --build-arg YTDLP_REFRESH="$(date +%Y%m%d%H%M%S)" && docker compose up -d; then
  docker image prune -f > /dev/null   # 置き換わった古いイメージを削除
  docker builder prune -f --filter until=168h > /dev/null
  version=$(curl -fsS -m 10 --retry 5 --retry-connrefused --retry-delay 2 "http://127.0.0.1:${PORT}/api/health" \
    | sed -n 's/.*"yt_dlp_version":"\([^"]*\)".*/\1/p')
  echo "$(now) done: yt-dlp ${version:-unknown}"
else
  echo "$(now) FAILED: keeping the current container"
  exit 1
fi