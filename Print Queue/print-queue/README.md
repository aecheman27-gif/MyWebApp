# Print Queue

A small web app for managing the 3D-printer queue for a non-ITAR shop tool.
Submitters upload STEP files; operators slice them and load them on a Bambu
H2D; the app auto-tracks live print state via the printer's MQTT telemetry.

## Architecture

```
                                    Public internet
                                          │
                            ┌─────────────┴──────────────┐
                            │                            │
                       Cloudflare                Browsers (corp net)
                          edge
                            │
                       outbound-only
                       tunnel (no inbound ports)
                            │
   ┌─────────────────── Bridge laptop on guest WiFi ──────────────────┐
   │                                                                  │
   │   Docker compose stack:                                          │
   │                                                                  │
   │   ┌─────────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐  │
   │   │ cloudflared │──▶│  caddy   │──▶│   web     │──▶│    db    │  │
   │   └─────────────┘   └──────────┘   │ (FastAPI) │   │ (Postgres)│ │
   │                                    └─────┬─────┘   └──────────┘  │
   │                                          ▲                        │
   │                                          │ /internal/telemetry    │
   │                                    ┌─────┴────┐                   │
   │                                    │  bridge  │                   │
   │                                    │ (paho-   │                   │
   │                                    │  mqtt)   │                   │
   │                                    └─────┬────┘                   │
   │                                          │                        │
   │   ┌──────────┐                           │ MQTT TLS :8883         │
   │   │  backup  │──▶ Cloudflare R2          │ (local LAN)            │
   │   └──────────┘                           ▼                        │
   │                                    Bambu H2D #1, #2 (on guest WiFi)
   └──────────────────────────────────────────────────────────────────┘
```

### Critical isolation rules

- **The bridge laptop is on guest WiFi.** The guest WiFi and SpaceX corporate
  network are never directly connected. All public access flows through
  Cloudflare's edge.
- **The laptop must never be signed into a Tailscale account joined to a
  corporate tailnet.** Doing so would route corp traffic through the tunnel.
- **No inbound ports are opened on guest WiFi.** All connectivity is outbound:
  the cloudflared container dials Cloudflare, the bridge container dials each
  printer's local MQTT broker.
- **Nothing on this system is for ITAR data.** This tool is for non-ITAR utility
  prints only (cable organizers, jigs, fixtures).

## Milestones implemented

- **M1** — magic-link auth, allowlist, sessions, structured logging, Sentry, R2 backup, GitHub Actions CI
- **M2** — submission CRUD with permissions, queue ordering, status workflow, search, audit log, 90-day retention
- **M3** — bridge service: per-printer MQTT connections, normalized telemetry, offline buffer, POST to app
- **M4** — live printer widget on the queue page, Server-Sent Events feed, stale-printer detection
- **M5** — filename auto-bind: `sub-<id>-...` in `subtask_name` flips the matching submission Queued/Slicing → Printing → Done/Failed automatically
- **M6** — multi-printer support: `printers` table, multi-card UI, optional per-submission printer pinning

## Team features (post-M6)

- **Email notifications** — submitters get an email when their submission moves to Printing, Done, Failed, or Cancelled. Per-user opt-out via the `email_notifications` flag. Fires on both manual operator transitions and auto-bind.
- **Failure webhooks** — print failures push to Slack and/or Discord (set `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL`; either, both, or neither).
- **User management** — operator-only `/admin/users` page to add/remove users and toggle roles, no SSH needed. The `.env` allowlist now seeds the database at startup (so you can't lock yourself out) but the database is the runtime source of truth.
- **Comments** — a comment thread on each submission. Operator comments notify the submitter; submitter comments notify all operators.
- **Stats + CSV** — operator-only `/admin/stats`: submission counts, failure rate, total/average/longest print time (derived from status-transition timestamps), and breakdowns by status, material, submitter, and printer. Date-range filter and CSV export.

## Project layout

```
app/                   # FastAPI server
  admin/               # operator-only: user management, stats, CSV export
  auth/                # magic-link auth + bootstrap (seeds .env users to DB)
  comments/            # submission comment threads
  email/               # Resend client
  models/              # SQLAlchemy ORM models
  notifications/       # status-change emails + Slack/Discord failure webhooks
  printers/            # M3-M6 server side: routes, service, schemas, SSE pubsub
  routes/              # health
  static/              # css, js (including SSE updater)
  storage/             # file storage abstraction (local + R2 stub)
  submissions/         # M2 CRUD
  templates/           # Jinja2 templates
  config.py, database.py, logging.py, main.py
bridge/                # M3 bridge service (runs in its own container)
  config.py            # env var loader, multi-printer config
  parser.py            # Bambu MQTT report → normalized telemetry
  buffer.py            # SQLite buffer for offline tolerance
  app_client.py        # httpx POST to /internal/telemetry
  printer.py           # paho-mqtt connection per printer
  main.py              # entrypoint
  Dockerfile
alembic/               # migrations (0001 baseline, 0002 submissions, 0003 printers, 0004 users+comments)
scripts/               # backup.sh
tests/                 # 90 tests, all passing
Caddyfile              # reverse proxy in tunnel mode
docker-compose.yml     # full stack: db, web, caddy, bridge, cloudflared, backup
Dockerfile             # web container
.env.example           # all required env vars with annotations
```

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check app/ bridge/ tests/
black app/ bridge/ tests/
pytest
```

Tests run against an in-memory SQLite — no Docker needed.

## Production deployment (bridge laptop)

### Phase 1 — Windows host preparation

1. **Power settings**: `Settings → Power & battery → Screen and sleep → Never` on both AC and battery.
2. **Lid behavior**: `Control Panel → Hardware and Sound → Power Options → Choose what closing the lid does → When plugged in: Do nothing`.
3. **Active hours**: `Settings → Windows Update → Advanced options → Active hours → 6 AM to 1 AM` so reboots happen overnight.
4. **Sign-in**: Set the laptop to auto-sign-in after reboot, or keep it always logged in.

### Phase 2 — WSL2 + Docker

```powershell
# In PowerShell as administrator:
wsl --install -d Ubuntu-24.04
# Reboot. Set up Ubuntu user. Then inside Ubuntu:
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### Phase 3 — Deploy the app

```bash
cd ~
unzip print-queue-final.zip
cd print-queue
cp .env.example .env
nano .env   # fill in all credentials (see below)
docker compose up -d --build
docker compose logs -f web
```

Open https://3dprinterqueue.com/login, request a magic link to your email
(must be on `ALLOWED_EMAILS`), click it, and you're in.

### .env values

Generate the secrets locally:

```bash
python3 -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(48))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('BRIDGE_SHARED_TOKEN=' + secrets.token_urlsafe(32))"
```

Paste from your saved notes for: `RESEND_API_KEY`, `CLOUDFLARE_TUNNEL_TOKEN`,
`SENTRY_DSN`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.

Per printer, find on the H2D screen under **Settings → Network info**:
- IP address (`PRINTER_N_HOST`)
- Serial number (`PRINTER_N_SERIAL`)
- Access code (`PRINTER_N_ACCESS_CODE`)

## Operator workflow

1. **Submitter** uploads a STEP file via the web form.
2. **Operator** downloads the STEP, slices it in Bambu Studio.
3. When saving the `.3mf`, **rename it using the suggested filename** shown on
   the submission detail page: `sub-<8 hex chars>-<part name>.3mf`.
4. Send to the printer via Bambu Studio's normal "Send to printer" flow.
5. The bridge sees the new print's `subtask_name`, the app auto-flips the
   submission to **Printing**, and the live widget tracks progress.
6. On clean finish: submission → **Done**. On failure or error code: → **Failed**.
7. The operator can override status manually at any time.

## Backups

- Nightly `pg_dump` of the Postgres database → Cloudflare R2 bucket
  `print-queue-backups`, retained 30 days, encrypted at rest by R2.
- Uploaded STEP files are stored locally on the laptop. For now they are
  not backed up off-machine; if this becomes important, switch to the R2
  storage backend by setting `STORAGE_BACKEND=r2` in `.env`.

## Known operational risks

- **Bridge laptop is a single point of failure** for both telemetry intake
  and the public website. If it goes down, the printers keep printing but
  the app is unavailable. Planned mitigation: migrate the stack to a
  Raspberry Pi 5 with a UPS once the system is stable.
- **Resend deliverability to @spacex.com** is not guaranteed; corporate mail
  filters may quarantine login emails. If a teammate reports never receiving
  a magic link, check Resend's delivery logs and (if needed) raise an IT
  ticket to allowlist `noreply@3dprinterqueue.com`.
- **Bambu MQTT schema may differ on the H2D** from older X1/P1 documentation.
  If a field name comes through unrecognized, it will simply be ignored; check
  `printer_state.raw` in the DB to see the full payload and update `parser.py`.

## CI

GitHub Actions runs on every push to `main`:
- `ruff check`
- `black --check`
- `pytest`

A green build is required before treating any commit as production-ready.
