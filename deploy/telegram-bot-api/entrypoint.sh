#!/bin/sh
# Starts the private file endpoint and the media sweeper, then hands PID 1 to
# the official image's entrypoint (which execs telegram-bot-api --local).
set -eu

: "${TELEGRAM_WORK_DIR:=/var/lib/telegram-bot-api}"
: "${TELEGRAM_TEMP_DIR:=/tmp/telegram-bot-api}"
: "${FILE_TTL_MINUTES:=120}"

if [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ]; then
    echo "telegram-bot-api: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set" \
         "(create them at https://my.telegram.org -> API development tools)" >&2
    exit 1
fi

mkdir -p "$TELEGRAM_WORK_DIR" "$TELEGRAM_TEMP_DIR" /run/nginx
chown telegram-bot-api:telegram-bot-api "$TELEGRAM_WORK_DIR" "$TELEGRAM_TEMP_DIR"

nginx -t -q
nginx
echo "file endpoint listening on :${FILES_PORT:-8082}"

# Backstop cleanup. Media sits at <work>/<bot>/<type>/<file> (depth 3); TDLib's
# binlog and databases sit at depth 2 and are never touched. A deleted file is
# simply downloaded again by TDLib if it is ever requested.
(
    while true; do
        sleep 600
        find "$TELEGRAM_WORK_DIR" -mindepth 3 -maxdepth 3 -type f \
            -mmin +"$FILE_TTL_MINUTES" -delete 2>/dev/null || true
        find "$TELEGRAM_TEMP_DIR" -mindepth 1 -type f \
            -mmin +"$FILE_TTL_MINUTES" -delete 2>/dev/null || true
    done
) &

exec /docker-entrypoint.sh
