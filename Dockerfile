# Production image. Built and run in the cloud; Docker Compose is only used
# for local experimentation and is not required to deploy this.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Per-job workspaces live here and are removed as each job finishes, so the
    # container never needs a persistent disk for user media.
    TEMP_ROOT=/tmp/atreox-tools

# Native tools the media services shell out to (argv arrays only, no shell).
#   ffmpeg                 -> ffmpeg + ffprobe, for Video -> Circle, Voice Note
#                             (libopus) and Media Optimizer (libx264)
#   libimage-exiftool-perl -> exiftool, for Metadata Studio
#   fonts-dejavu-core      -> DejaVuSans, the font the Watermark tool draws with
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg libimage-exiftool-perl fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# Unprivileged runtime user owning the temp workspace root.
RUN useradd --create-home --uid 10001 atreox \
    && mkdir -p ${TEMP_ROOT} \
    && chown -R atreox:atreox ${TEMP_ROOT} /app
USER atreox

# Fail the build if a media tool went missing, ffmpeg cannot encode the Opus
# voice messages need or the H.264 the optimizer writes, or Pillow lacks the
# JPEG/WEBP codecs the photo optimizer uses.
RUN ffmpeg -version > /dev/null \
    && ffprobe -version > /dev/null \
    && ffmpeg -hide_banner -encoders | grep -q libopus \
    && ffmpeg -hide_banner -encoders | grep -q libx264 \
    && python -c "from PIL import features; assert all(map(features.check, ('jpg', 'webp', 'zlib')))" \
    && python -c "from app.services.media.watermark import resolve_font; print(resolve_font())" \
    && exiftool -ver > /dev/null

# The entrypoint waits for the database and applies migrations, then execs the
# command below. Override the command for one-off tasks, e.g.
#   docker run <image> python -m scripts.sync_stickers
ENTRYPOINT ["python", "-m", "scripts.docker_entrypoint"]
CMD ["python", "-m", "app.main"]
