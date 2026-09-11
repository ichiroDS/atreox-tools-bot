# Self-hosted Telegram Bot API server in --local mode, for the second Railway
# service. Build context is the repository root:
#   RAILWAY_DOCKERFILE_PATH=deploy/telegram-bot-api/Dockerfile
#
# Base image: aiogram/telegram-bot-api, a maintained build of the official
# tdlib/telegram-bot-api source (compiling TDLib ourselves takes ~30 minutes and
# several GB of RAM per deploy). Pinned so a deploy never changes the server.
FROM aiogram/telegram-bot-api:10.3

# nginx serves the files the Bot API downloads (see nginx.conf): in local mode
# getFile answers with a path on *this* container's disk, which the bot - a
# separate service - cannot read directly.
RUN apk add --no-cache nginx

COPY deploy/telegram-bot-api/nginx.conf /etc/nginx/nginx.conf
COPY deploy/telegram-bot-api/entrypoint.sh /atreox-entrypoint.sh
# CRLF-proof the script, and fail the build (not the deploy) on a bad config.
RUN sed -i 's/\r$//' /atreox-entrypoint.sh \
    && chmod +x /atreox-entrypoint.sh \
    && mkdir -p /run/nginx \
    && nginx -t

# TELEGRAM_API_ID and TELEGRAM_API_HASH come from https://my.telegram.org and
# are set as Railway variables - never baked into the image.
ENV TELEGRAM_LOCAL=1 \
    # Railway's private network is IPv6; "::" accepts it.
    TELEGRAM_HTTP_IP_ADDRESS=:: \
    TELEGRAM_HTTP_PORT=8081 \
    FILES_PORT=8082 \
    # Downloaded media older than this is deleted (the bot also deletes each
    # file as soon as its job ends; this is the backstop).
    FILE_TTL_MINUTES=120

EXPOSE 8081 8082
ENTRYPOINT ["/atreox-entrypoint.sh"]
