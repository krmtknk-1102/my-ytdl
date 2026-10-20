FROM python:3.12-slim
LABEL app=my-ytdl

# ffmpeg: 映像と音声の結合、mp3変換
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp が YouTube の解析に使う JavaScript ランタイム
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
# 値が変わると、この下の pip install がやり直され yt-dlp が最新版になる(update-ytdlp.sh が使う)
ARG YTDLP_REFRESH
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV DOWNLOAD_DIR=/downloads \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]