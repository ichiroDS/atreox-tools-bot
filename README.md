# Atreox Tools Bot

A free Telegram utility bot for channel owners, creators and AI influencer
operators. It ships six tools, plus a batch mode:

| Tool | What it does |
| --- | --- |
| 🎥 **Video → Circle** | Turns any video into a native Telegram video note (circle). |
| 🎙 **Voice Note** | Turns an audio file, or the sound of a video, into a native Telegram voice message. |
| 🗜 **Media Optimizer** | Shrinks photos and videos (Small / Balanced / High Quality) and returns them as a File. |
| 🖼 **Watermark** | Draws a handle or custom line on photos and videos, with saved presets. |
| 📦 **Batch Mode** | Runs Clean Metadata, Watermark or the Optimizer over many files in one go. |
| 🧹 **Metadata Studio** | Cleans metadata, or rewrites device / location / capture time. |
| 🎭 **Sticker Finder** | Discovery: six stickers, each from a *different* pack, so the user can open and add the pack they like. |

---

## Architecture

```
app/
  main.py                  entrypoint: bot, dispatcher, middlewares, error handler
  config.py                typed settings (pydantic-settings) - the only reader of env
  logging_config.py        structured logging with a job_id field

  bot/
    texts.py               every user-facing string
    callbacks.py           typed callback-data factories
    errors.py              internal failure -> friendly copy
    routers/               start, circle, voice, optimizer, watermark, batch,
                           metadata, stickers, admin, fallback
    keyboards/             built from the preset/category catalogs, not hardcoded
    states/                FSM state groups
    middlewares/           db session per update, user upsert + activity

  services/
    telegram_files.py      validate + fetch incoming files: cloud URL, local path,
                           or the local server's file endpoint (size limits)
    jobgate.py             heavy-job slots + per-job disk reservations
    timeofday.py           time-of-day intervals + timezone abstraction
    media/
      base.py              MediaInfo, ProcessedFile, ProcessingErrorCode
      probe.py             ffprobe wrapper (+ pure JSON parser)
      circle.py            ffmpeg circle encoder (+ pure split plan / arg builder)
      voice.py             ffmpeg OGG/Opus voice encoder (+ pure probe gate / arg builder)
      optimizer.py         photo/video optimizer: analysis, adaptive preset plans, verify
      image_worker.py      Pillow photo re-encoder, run as its own process
      watermark.py         watermark layout, drawtext builder, verification
      watermark_worker.py  Pillow watermark renderer, run as its own process
      batch.py             one tool applied to each file of a batch, item by item
      metadata.py          ExifTool clean/change/verify (+ pure arg builders)
    stickers/
      catalog.py           configurable categories
      service.py           distinct-pack selection (pure) + async service

  data/device_presets.py   iPhone presets for the Change Metadata wizard
  db/                      SQLAlchemy 2 models, repositories, session factory
  utils/
    temp_files.py          per-job workspace, safe filenames, guaranteed cleanup
    subprocess.py          argv-only execution with timeouts

alembic/                   migrations
scripts/seed_stickers.py   seed the sticker catalog from a JSON/YAML fixture
scripts/cutover_local_bot_api.py  one-time, confirmed move to the local Bot API
deploy/telegram-bot-api/   second Railway service: local Bot API + file endpoint
tests/                     pytest suite
```

**Layering rule:** routers own the Telegram conversation, services own the work,
and the media services own every ffmpeg/ExifTool argument. A router never builds
a tool command.

**Testability rule:** each native-tool integration is split into a *pure
argument builder* (`build_circle_ffmpeg_args`, `build_voice_ffmpeg_args`,
`build_video_args`, `plan_video`, `build_clean_args`, `build_change_args`, `parse_probe_output`) and a thin async runner. The builders
are unit tested; the runners are covered by end-to-end tests that skip when the
tool is absent.

### File handling and safety

- Every job gets `{TEMP_ROOT}/{job-uuid}/`, removed in a `finally` block whether
  the job succeeds, fails or times out.
- User filenames are never used on disk. Only a whitelisted extension is
  borrowed; the on-disk name is a random UUID. Outgoing document names are
  sanitised separately.
- Subprocesses are launched with argument arrays via
  `asyncio.create_subprocess_exec` — no shell is ever spawned, so shell
  metacharacters in a filename are inert. Options are terminated with `--`
  before any path.
- Every subprocess has a timeout and is killed when it expires.
- Size limits are enforced three times: on the size Telegram declares, on the
  size reported by `getFile`, and on the bytes actually streamed - a download
  that outgrows the limit is aborted and its partial file removed.
- Files are never read into memory: downloads stream to disk a megabyte at a
  time, uploads stream from disk (`FSInputFile`).
- Before any upload the output size is checked against what the Bot API server
  accepts, so the bot never starts an upload that is certain to fail.
- A job gate admits media jobs immediately or answers "server busy": large-file
  jobs share `MAX_CONCURRENT_MEDIA_JOBS` slots (one per user at a time), small
  files skip the slots, and every job reserves the disk it expects to need.
- Leftover workspaces from a killed process are swept at startup.
- User media is never stored permanently. Logs record job ids, sizes and exit
  codes — never media content.

### Long videos → circles

A video that fits in one circle (≤ `VIDEO_NOTE_MAX_DURATION`, 60 s) is
converted straight away. A longer one gets a choice — **✂️ Split into
Circles**, **▶️ First Circle Only** or **❌ Cancel**. Splitting tiles the whole
timeline with consecutive 60 s segments plus a shorter final one: each segment
is encoded straight from the source with an accurate input seek, sent with
`sendVideoNote`, and deleted before the next starts, so there is never one big
intermediate encode on disk. Container slop under 0.5 s past a boundary does
not produce a sliver of a circle.

### Audio / video → voice notes

🎙 Voice Note accepts audio (MP3, WAV, M4A, AAC, OGG, FLAC, Opus, …) and video
(MP4, MOV, WebM, MKV, …), as native media or as a File. ffprobe decides what the
file really is: only an allowlist of demuxers is accepted (a playlist, image or
text file is refused before FFmpeg decodes anything), and a video with no audio
track is answered with *"🔇 This video doesn't contain an audio track."*.

The first audio stream is encoded to **OGG/Opus, mono, 48 kHz, 48 kbit/s,
`-application voip`** - speech-tuned, but with no filter that changes pitch,
tempo or loudness. The result is probed again (container `ogg`, codec `opus`,
non-zero duration) and sent with **`sendVoice`** as `voice.ogg`, so Telegram
renders the native waveform bubble. If Telegram ever returns anything other
than a voice message, that message is deleted and the job fails rather than
report success. A recipient whose privacy settings refuse voice messages gets
told how to allow them. Large files go through the same streaming fetch, job
gate and per-job workspace as circles.

### Media Optimizer

The file is fetched and probed first, and the user sees what it is (size,
resolution, duration, codec, frame rate, bitrate, audio) before choosing
**⚡ Small / ⚖️ Balanced / 💎 High Quality**. The Bot API server keeps its copy
between the two steps, as it does for the long-video choice.

**Video** → H.264 + AAC in a faststart MP4: Small caps the short side at 720,
Balanced and High Quality at 1080; nothing is upscaled, the aspect ratio is
kept, rotation is applied to the pixels (no rotation tag left), frame rates
above 60 fps are brought down to 60, silent videos stay silent, lean AAC is
copied, surround is folded to stereo, and all container metadata is dropped.
Presets are *adaptive*: the target bitrate follows output resolution × frame
rate × a per-preset bits-per-pixel, and is capped at a share of what the source
already spends. Encoding is one-pass ABR under a VBV cap, so the size shown on
each button (`⚡ Small — ~18 MB`) is a real estimate. A preset that could not
make the file meaningfully smaller is not offered; if none could, the user is
told *"✅ This file is already well optimized."*

**Photos** keep their format and exact pixel dimensions. JPEG is re-encoded
progressive/optimised at a preset quality that never exceeds the source's own
(High Quality re-uses the source's quantisation tables); EXIF is dropped except
Orientation, and the ICC profile is kept. PNG is lossless for every preset
(opaque alpha dropped, ≤256-colour images palettised, both verified pixel for
pixel; 16-bit PNGs go through FFmpeg to keep every bit) and transparency always
survives. WEBP stays lossy or lossless as it was. Photos get no size estimate -
their compressibility depends on content. Encoding runs in a separate Pillow
process (`image_worker.py`) so a huge decode can be killed by the timeout.

Every output is probed again (container, codec, dimensions, duration, audio,
transparency) and sent as a **File** (`disable_content_type_detection`) named
`atreox_optimized_<name>`. A result that is not at least 2 % smaller is never
sent: *"✅ Your original file is already efficiently compressed."* Long encodes
update the status message at 25/50/75 %, at most every 15 s.

### Watermark

A file, one short line of text, then position / style / size / opacity. The
file is only *referenced* while the wizard runs - nothing is fetched until
Apply - so stepping back and forth costs no transfers. The text is cleaned to a
single line (Unicode "Cf" characters dropped so a watermark cannot hide behind
zero-width or bidi tricks) and capped at 48 characters.

The size is a share of the **frame height** (S 2.5 %, M 3.5 %, L 5 %), inset by
2 % of the shorter side, so it looks the same on a 720p clip and a 4K one; the
text is measured with the real font and shrunk until it fits the width, so a
long handle on a portrait frame never runs off the picture. Photos and videos
draw with the *same* TrueType font (`fonts-dejavu-core` in the image,
`WATERMARK_FONT` to override), which is what lets the layout be planned once
and handed to FFmpeg as an exact pixel size.

**Photos** (Pillow, in its own process) keep their format, their pixel count
and their colour profile; the text is composited through a small mask tile, so
memory stays flat and transparency composites correctly. A photo stored rotated
is written upright - which is the only case where width and height swap.
**Videos** (FFmpeg `drawtext`) keep resolution, frame rate, duration and audio
(copied verbatim when it is already AAC or MP3; silent stays silent), and the
watermark is on every frame. Text and font are placed in the job workspace and
FFmpeg runs *inside* it, so no path ever has to be escaped into a filter graph.
Encoding reuses the optimizer's memory budget, and because a 4K frame is
encoded *as* 4K, frames above 1080p switch to the light encoder (measured
964 MB -> 352 MB, well inside the 1 GB container).

Presets live in `watermark_presets` (max 10 per user, unique name per user) and
store the text *and* the look, so a repeat is two taps. Every read and write is
scoped to the owner's Telegram id.

### Batch Mode

Files arrive one by one or as a Telegram album (which arrives as separate
updates - they join the same batch, and a re-delivered item is ignored). Only
references are kept: nothing is fetched until processing starts, so collecting
costs no transfers. One message counts the batch and is edited as it grows, and
a late album item still joins after **✅ Done Uploading** was pressed.
`MAX_BATCH_FILES` (20) caps it; collection uses its own roomy rate-limit bucket
so a legitimate album never trips the per-media limit, while a flood still does.

A batch runs **one tool over every file**: Clean Metadata, Watermark (a saved
preset, or one configured once for the batch) or the Optimizer (one preset).
Each is the existing service, unchanged - a batch cannot drift from what the
single-file tool does.

Processing is **sequential on purpose**. The container has little memory and one
4K encode already claims a large part of it, so a batch never starts a second
job of its own, and every item still passes the shared job gate - a busy server
makes an item wait briefly rather than fail. Each item gets its own
`batch_<uuid>/item_001/` directory, which is removed as soon as that item is
done; the whole batch workspace goes at the end, whatever happened. Results are
sent as Files as they finish (`atreox_cleaned_`, `atreox_watermarked_`,
`atreox_optimized_`), one progress message is edited as the count rises, a
failed item is recorded and the rest continue, and Cancel stops the run after
the file in flight. An optimizer item that cannot be made meaningfully smaller
keeps its original and is reported as such rather than replaced.

Each file keeps its own `jobs` row (so per-tool stats stay true), and the batch
itself records `batch_started` / `batch_completed` feature events.

---

## Local development

Requires Python 3.10+ (the Docker image runs 3.12), plus `ffmpeg`/`ffprobe` and
`exiftool` on `PATH` for the media tools.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements-dev.txt
cp .env.example .env            # then fill in BOT_TOKEN

alembic upgrade head            # needs a reachable PostgreSQL
python -m app.main
```

### Native tools

| Tool | Used by | Install |
| --- | --- | --- |
| `ffmpeg`, `ffprobe` (with `libopus`, `libx264`, `libfreetype`) | Video → Circle, Voice Note, Media Optimizer, Watermark | `apt install ffmpeg` / `winget install Gyan.FFmpeg` / `brew install ffmpeg` |
| `exiftool` | Metadata Studio | `apt install libimage-exiftool-perl` / `winget install OliverBetz.ExifTool` / `brew install exiftool` |
| Pillow (Python package) | Media Optimizer, Watermark (photos) | `pip install -r requirements.txt` (wheels bundle libjpeg, zlib, libwebp) |
| A TrueType font | Watermark | `apt install fonts-dejavu-core` (the image does this); macOS/Windows system fonts are found automatically |

Binaries are configurable via `FFMPEG_BIN`, `FFPROBE_BIN` and `EXIFTOOL_BIN` if
they are not on `PATH`.

### BotFather setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`, pick a name and username.
2. Copy the token into `BOT_TOKEN` in `.env`.
3. Optional, recommended: `/setcommands` →
   ```
   start - Open the tool menu
   help - How the tools work
   cancel - Cancel the current operation
   ```
4. Optional: `/setprivacy` → *Disable* only if you later add group support. V1 is
   a private-chat bot and does not need it.

---

## Deployment

The image is self-contained: it installs ffmpeg, ffprobe and ExifTool, runs as
an unprivileged user, and its entrypoint waits for the database and applies
migrations before starting the bot. **Docker Compose is not required in
production** - any platform that builds a Dockerfile and sets environment
variables will do (Railway, Fly.io, Render, a plain VPS).

```
ENTRYPOINT ["python", "-m", "scripts.docker_entrypoint"]
CMD        ["python", "-m", "app.main"]
```

On boot the container:

1. waits for `DATABASE_URL` to answer (30 attempts, 2s apart) - a managed
   PostgreSQL is often still starting when the app container is ready;
2. runs `alembic upgrade head`, holding a PostgreSQL advisory lock so two
   instances booting together cannot race;
3. `exec`s the bot, which keeps PID 1 and receives the platform's stop signals.

Startup logs read: `application starting` -> `database connected` ->
`migrations complete` -> `starting Telegram long polling`. The token is never
logged, and database URLs are printed with credentials stripped.

### Deploy checklist

1. Provision PostgreSQL and copy its connection string.
2. Set the environment variables below. `DATABASE_URL` can be pasted in
   exactly as the platform provides it: `postgres://` and `postgresql://` are
   rewritten to `postgresql+asyncpg://` at startup, and `?sslmode=` becomes
   the `?ssl=` spelling asyncpg expects. Only asyncpg is installed - no
   synchronous driver is needed.
3. Deploy from GitHub. The entrypoint migrates the empty database on first boot.
4. Populate the sticker catalog (see *Sticker catalog* below) - a fresh
   database has no packs, so the Sticker Finder would otherwise come up empty.

### Local Docker (optional)

```bash
cp .env.example .env    # fill in BOT_TOKEN
docker compose up --build
```

Compose starts `db` (PostgreSQL 16) and `bot`. There is no separate migration
service: the entrypoint handles it. The bot's `TEMP_ROOT` is a `tmpfs` mount,
so user media never touches a persistent volume.

### Storage

The app needs **no persistent disk**. Every job gets its own directory under
`TEMP_ROOT`, removed in a `finally` block whether the job succeeds or fails, so
an ephemeral container filesystem is exactly right.

---

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | *(required)* | BotFather token. |
| `BOT_API_BASE_URL` | `https://api.telegram.org` | Point at the self-hosted Bot API server (after the cutover) to lift the 20 MB / 50 MB limits. |
| `BOT_API_FILES_URL` | *(unset)* | Local mode: the Bot API server's private file endpoint, e.g. `http://telegram-bot-api.railway.internal:8082`. |
| `BOT_API_LOCAL_DIR` | `/var/lib/telegram-bot-api` | Local mode: the server's `--dir`, used to map its absolute paths to file URLs. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./atreox_tools.db` | Async SQLAlchemy URL. Production: any PostgreSQL URL - `postgres://`, `postgresql://` and `postgresql+asyncpg://` are all accepted and normalised onto asyncpg. Unset locally means SQLite, no server needed. |
| `LOG_LEVEL` | `INFO` | One of CRITICAL/ERROR/WARNING/INFO/DEBUG. |
| `MAX_INPUT_FILE_SIZE_MB` | `2000` | Service limit for uploads. On the cloud API Telegram's 20 MB still caps it. |
| `MAX_OUTPUT_FILE_SIZE_MB` | `1950` | Service limit for files sent back (local API hard ceiling 2000 MB; cloud 50 MB). |
| `PROCESS_TIMEOUT_SECONDS` | `600` | Per native-tool run (one probe, one ExifTool pass, one circle segment, one voice encode, one optimizer encode). |
| `MAX_CONCURRENT_MEDIA_JOBS` | `2` | Large-file jobs at once. Small files never wait on this. |
| `TEMP_DISK_BUDGET_MB` | `0` | Cap on total workspace reservations; `0` = free space under `TEMP_ROOT` minus 512 MB. |
| `TEMP_ROOT` | OS temp dir (image: `/tmp/atreox-tools`) | Root of per-job workspaces. Ephemeral by design. |
| `FFMPEG_BIN` / `FFPROBE_BIN` / `EXIFTOOL_BIN` | tool name | Override binary paths. |
| `WATERMARK_FONT` | *(empty)* | TrueType font for the watermark. Empty = find a known system font. |
| `STICKERS_PER_BATCH` | `6` | Stickers per Sticker Finder batch (one per pack). |
| `MAX_BATCH_FILES` | `20` | Files one Batch Mode run may hold. |
| `VIDEO_NOTE_MAX_DURATION` | `60` | Longest single circle (Telegram's cap is 60 s). Longer videos are offered a split. |
| `VIDEO_NOTE_SIZE` | `384` | Square side of the circle; must be even. |
| `ADMIN_USER_IDS` | *(empty)* | Comma-separated Telegram ids allowed to run `/stats` and the sticker `file_id` helper. |

`.env` is gitignored and is **only** for local development - production reads
the environment directly, so no `.env` file is deployed.

Minimum set for a cloud deployment:

```
BOT_TOKEN=<from @BotFather>
DATABASE_URL=<the platform's PostgreSQL URL, any scheme>
ADMIN_USER_IDS=<your telegram user id>
LOG_LEVEL=INFO
TEMP_ROOT=/tmp/atreox-tools
```

Never commit a token.

---

## Local Bot API server (large files)

The cloud Bot API downloads at most 20 MB and uploads at most 50 MB. A
self-hosted [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api)
in `--local` mode downloads without a size limit and uploads up to 2000 MB.

```
Telegram
   ↓
telegram-bot-api   (Railway service, --local; :8081 Bot API, :8082 file endpoint)
   ↓ private network: telegram-bot-api.railway.internal
atreox-tools-bot
   ↓
PostgreSQL
```

`deploy/telegram-bot-api/` holds the second service: the pinned
`aiogram/telegram-bot-api` image (a build of the official source) plus a small
nginx file endpoint. In local mode `getFile` answers with an absolute path on
the *server's* disk; Railway services cannot share a volume, so the bot streams
that file from `:8082` into its job workspace and `DELETE`s it when the job
ends (a 2-hour TTL sweep is the backstop). Neither port has a public domain.
When the bot *can* see the path (a shared disk), it uses the file in place
without copying.

### Railway service

- Service `telegram-bot-api`, source: this repo,
  `RAILWAY_DOCKERFILE_PATH=deploy/telegram-bot-api/Dockerfile`, no public domain.
- Variables: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` — from
  [my.telegram.org](https://my.telegram.org) → *API development tools*. The
  container refuses to start without them.

### Cutover (one time, deliberate)

Telegram requires `logOut` on the official API before a local server may serve
the bot, and the official API then refuses the token for 10 minutes. Nothing
in the app does this automatically; `scripts/cutover_local_bot_api.py` does it
only on request, after checking everything first. Run it inside the project so
the private network is reachable:

```bash
# 0. The telegram-bot-api service is deployed with its two secrets and is Online.

# 1. Dry run: settings, both local ports, token valid on the official API.
railway ssh --service atreox-tools-bot -- python /app/scripts/cutover_local_bot_api.py \
  --local-url http://telegram-bot-api.railway.internal:8081 \
  --files-url http://telegram-bot-api.railway.internal:8082

# 2. Stage the bot's new variables (no deploy yet).
railway variable set --service atreox-tools-bot --skip-deploys \
  BOT_API_BASE_URL=http://telegram-bot-api.railway.internal:8081
railway variable set --service atreox-tools-bot --skip-deploys \
  BOT_API_FILES_URL=http://telegram-bot-api.railway.internal:8082

# 3. logOut on api.telegram.org, then getMe through the local server.
railway ssh --service atreox-tools-bot -- python /app/scripts/cutover_local_bot_api.py \
  --local-url http://telegram-bot-api.railway.internal:8081 \
  --files-url http://telegram-bot-api.railway.internal:8082 --execute --yes

# 4. Restart the bot on the local server.
railway service redeploy --service atreox-tools-bot --yes
```

The bot is unreachable between steps 3 and 4 (about a minute). Startup then
logs `local_api=True` and `media limits: input=2000MB output=1950MB`.
**Rollback:** delete the two variables, wait 10 minutes after the `logOut`,
and redeploy — the bot is back on `api.telegram.org`.

---

## Migrations

```bash
alembic upgrade head                       # apply
alembic downgrade -1                       # roll back one
alembic revision --autogenerate -m "..."   # new migration (needs a live DB)
alembic upgrade head --sql                 # render DDL without connecting
```

`alembic/env.py` takes the URL from `app.config`, so there is no second place to
configure the database.

---

## Sticker catalog

The Sticker Finder is **not** a pack downloader and does **not** scrape
Telegram. It serves our own curated catalog from the database: enabled packs in
a category are shuffled, one enabled sample is drawn per pack, and no two
stickers in a batch come from the same pack.

Telegram `file_id`s are bot-specific, so they cannot be committed to a fixture.
`scripts/sync_stickers.py` resolves the curated pack names in
`app/services/stickers/packs.py` against the live Bot API and writes the real
ids to the database.

**Run this once after the first production deploy** (the database starts
empty), and again whenever packs are added to the curated list:

```bash
python -m scripts.sync_stickers
```

In a container, override the command - the entrypoint still prepares the
database first:

```bash
docker run --rm \
  -e BOT_TOKEN=... -e DATABASE_URL=postgresql+asyncpg://... \
  <image> python -m scripts.sync_stickers
```

On a platform with a one-off/console command (Railway, Fly, Render), run the
same `python -m scripts.sync_stickers` there.

The sync is idempotent: packs are matched on `telegram_set_name` and their
samples replaced, so re-running refreshes rather than duplicates. A pack
Telegram no longer serves is skipped with a warning instead of failing the run.
Add `--dry-run` to resolve without writing.

Adding a pack means adding its name to `app/services/stickers/packs.py` and
re-running the sync. Categories live in `app/services/stickers/catalog.py`.

`scripts/seed_stickers.py` remains for loading a hand-written fixture whose
`file_id`s you captured yourself; it is not used for the curated catalog.

---

## Analytics

- **New users** — `users` rows, created on first contact.
- **Acquisition source** — `t.me/<bot>?start=<source>`. The payload is
  sanitised (`[A-Za-z0-9_-]`, 64 chars) and written **only on first contact**, so
  a returning user keeps the link that originally brought them in.
- **Feature selected** — a `feature_events` row per tool opened.
- **Successful / failed jobs** — `jobs` rows with status, sizes, and a stable
  internal `error_code` on failure.
- **Watermark presets** — the only user content stored on purpose:
  `watermark_presets` keeps the text and look a user asked to save, and
  nothing else. Uploaded media is never stored.
- **Batches** — `batch_started` / `batch_completed` events, plus the ordinary
  per-file `jobs` rows, so a batch never hides what it actually did.

> Deviation from the original spec, called out deliberately: `feature_events` is
> a fifth table, added because "track feature selected" has no home in the four
> specified tables. The spec said *at least* these tables.

---

## Tests

```bash
pytest                       # whole suite
pytest -q --tb=short
pytest tests/test_media_e2e.py -rs   # real ffmpeg/exiftool, shows skip reasons
```

The suite covers config validation, sticker selection (never two stickers from
one pack), FSM transitions for all three flows, temp-directory cleanup including
the failure path, device-preset validation, time-of-day interval logic including
the midnight wraparound, ffmpeg/ExifTool argument construction, ffprobe parsing,
repositories against SQLite, the seeder, and bot wiring.

`tests/test_media_e2e.py` runs the real tools and **skips** when a tool is
missing, so a bare machine still gets a green suite; the Docker image has both
tools, so everything runs there.

---

## Feature status

| Area | Status |
| --- | --- |
| `/start`, menu, help, deep-link source capture | Working |
| Video → Circle (probe, rotation-aware square crop, scale, H.264 encode, `sendVideoNote`) | Working; verified end-to-end against real ffmpeg |
| Voice Note (probe gate, OGG/Opus encode + verify, `sendVoice`) | Working; verified end-to-end against real ffmpeg and aiogram's request builder |
| Media Optimizer (analysis, adaptive presets, H.264/AAC + JPEG/PNG/WEBP, verify, `sendDocument`) | Working; verified end-to-end against real ffmpeg and Pillow |
| Watermark (text wizard, presets, Pillow + `drawtext`, verify, `sendDocument`) | Working; positions, sizes and opacity measured from the rendered pixels |
| Batch Mode (album-aware collection, three tools, sequential processing) | Working; verified with real ExifTool/FFmpeg/Pillow batches |
| Metadata: clean (ExifTool strip, keeps orientation/ICC, verifies output) | Implemented; **not yet run against real ExifTool** |
| Metadata: change wizard (file → device → location → time of day → confirm) | FSM working; write path implemented, **not yet run against real ExifTool** |
| Sticker Finder (categories, distinct-pack batches, More/Categories/Menu) | Logic working; **needs real `file_id`s seeded** |
| Analytics, jobs, error UX, temp cleanup | Working |
| Docker Compose, migrations | Migration DDL verified offline; **compose not run** (no Docker on the dev machine) |

**Placeholders / known gaps**

- `scripts/fixtures/stickers.yaml` contains `REPLACE_ME_*` file ids. Until real
  ids are seeded, Sticker Finder shows the category menu and then the empty-state
  message, because Telegram rejects each send.
- The ExifTool paths (clean and change) have unit-tested argument construction
  and gated end-to-end tests, but ExifTool was not installed on the machine where
  this was built, so those two tests have never actually executed.
- Timezones are approximated as `round(longitude / 15)` hours
  (`LongitudeOffsetTimezoneResolver`). The `TimezoneResolver` protocol exists so
  a `timezonefinder`-backed implementation can drop in without touching handlers.

---

## Production notes

- **File size.** The cloud Bot API caps downloads at 20 MB and uploads at
  50 MB; the effective limits follow whichever API is configured. See
  *Local Bot API server* above to lift them.
- **FSM storage.** `MemoryStorage` is in-process, so wizard state is lost on
  restart and the bot cannot be scaled horizontally as-is. Switch to Redis
  storage before running more than one instance.
- **Concurrency.** aiogram runs each update as its own task, so a long job
  never blocks other users. A job commits its row as soon as it starts, so a
  long encode does not hold a database connection. The job gate bounds heavy
  work and answers "busy" instead of queueing.
- **Disk.** Workspaces are removed in `finally`, stale ones are swept at
  startup, and every job reserves disk before it starts, keeping 512 MB free.
- **Secrets.** `.env` only. Nothing is logged that could identify media content.
