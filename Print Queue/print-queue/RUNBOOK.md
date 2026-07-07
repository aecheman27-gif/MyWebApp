# Print Queue — Operator Runbook

The "what's broken and how do I fix it" doc. Keep this open in a tab.

For first-time deploy verification, see `DEPLOY_DAY_1.md` first.

---

## Quick reference

| Symptom                                      | Section                                |
| -------------------------------------------- | -------------------------------------- |
| Site is unreachable                          | [Site down](#site-unreachable)         |
| Magic-link email never arrives               | [Auth issues](#magic-link-not-arriving)|
| Printer card stuck on OFFLINE                | [Printer offline](#printer-stuck-offline) |
| Telemetry isn't updating                     | [Telemetry stale](#telemetry-stuck-stale) |
| Auto-bind didn't fire on print start         | [Autobind not firing](#autobind-not-firing) |
| Bridge container won't start                 | [Bridge won't start](#bridge-container-wont-start) |
| Backup didn't run last night                 | [Backup issues](#backup-didnt-run)     |
| Need to add a teammate                       | [Adding a user](#adding-or-removing-a-teammate) |
| Need to add a third printer                  | [Adding a printer](#adding-a-printer)  |
| Submission stuck in wrong state              | [Manual override](#manually-overriding-submission-status) |
| Want to read logs                            | [Reading logs](#reading-logs)          |
| Updating the app to a new version            | [Updates](#updating-the-app)           |

---

## Site unreachable

`https://3dprinterqueue.com` returns no response, a Cloudflare error page, or hangs.

**1. Check the public side.** Try the URL from a phone on cellular data (not corp WiFi). If it works there but not on corp, the issue is corp-side network or DNS — not yours. Report to IT.

**2. Check the laptop is up.** Walk to it. Lid open, screen wakes, no shutdown.

**3. Check the docker stack is running.**

```bash
cd ~/print-queue
docker compose ps
```

Every service should show `Up` or `running`. If any are `Exited` or `Restarting`, check that service's logs (see [Reading logs](#reading-logs)).

**4. Check the tunnel specifically.**

```bash
docker compose logs --tail=50 cloudflared
```

Healthy output includes `Registered tunnel connection` lines and no recent `connection error` storms. If the tunnel is unhappy:

```bash
docker compose restart cloudflared
```

If that doesn't fix it, the tunnel token may be invalid. Get a fresh token from Cloudflare dashboard → Zero Trust → Networks → Tunnels → `print-queue` → Configure → Install connector → copy the new token into `.env` as `CLOUDFLARE_TUNNEL_TOKEN`, then:

```bash
docker compose up -d cloudflared
```

**5. Bypass the tunnel for testing.** Run on the laptop:

```bash
docker compose exec web curl -sS http://localhost:8000/healthz
```

If that returns `{"status":"ok"}`, the app is healthy and the tunnel is the problem. If it doesn't, the app is the problem — check `docker compose logs web`.

---

## Magic-link not arriving

Someone requested a magic link and 5+ minutes later still hasn't received it.

**1. Was the email allowlisted?** Check `.env`:

```bash
grep ALLOWED_EMAILS ~/print-queue/.env
```

The exact email (lowercase, no typos) must appear in the comma-separated list. If it's missing, see [Adding a teammate](#adding-or-removing-a-teammate).

**2. Check Resend's delivery log.** Sign in to `resend.com` → **Emails** tab. Filter by the recipient. You'll see one of:

- **Delivered** — the email reached the recipient's mail server. The problem is on the recipient side: spam folder, Outlook focused-inbox split, corporate quarantine. Have them check spam first. If it's not there, raise an IT ticket asking to allowlist `noreply@3dprinterqueue.com`.
- **Bounced** — corp filters rejected the email. The bounce body usually says why ("550 message blocked" etc.). Same fix: IT ticket.
- **Nothing for that recipient** — we never tried to send. Check the app's logs to see if the magic-link request even reached us:

```bash
docker compose logs --tail=200 web | grep -i "magic_link"
```

A successful request looks like `email.resend.sent`. A misconfiguration looks like `email.resend.no_api_key.console_fallback` (you forgot to set `RESEND_API_KEY`).

**3. Was the link clicked but didn't work?** Magic links expire after 15 minutes. If they sat in someone's inbox too long, ask them to request a new one.

---

## Printer stuck OFFLINE

A printer card shows `OFFLINE` and won't update.

**1. Is the bridge container running?**

```bash
docker compose ps bridge
```

If it's not `Up`, see [Bridge won't start](#bridge-container-wont-start).

**2. Can the bridge see the printer's MQTT broker?**

```bash
docker compose exec bridge python -c "
import socket
s = socket.create_connection(('PRINTER_IP_HERE', 8883), timeout=3)
print('reachable')
s.close()
"
```

If it errors, the printer's IP changed (very common on guest WiFi — DHCP renewals), the printer is off, or the printer is on a different network.

To fix an IP change: walk to the printer, **Settings → Network info → IP address**, update `PRINTER_N_HOST` in `.env`, then:

```bash
docker compose up -d bridge
```

**3. Did the access code change?** Bambu sometimes regenerates the LAN access code (`Settings → Network → Access Code` on the printer). If it doesn't match `PRINTER_N_ACCESS_CODE` in `.env`, the bridge gets an MQTT auth error in its logs:

```bash
docker compose logs --tail=50 bridge | grep -iE "auth|refused|connect"
```

Update `.env` and `docker compose up -d bridge`.

**4. Did the serial number change?** Almost never, but if you swapped which physical printer is in this slot, check `PRINTER_N_SERIAL`.

---

## Telemetry stuck / stale

Card is updating but very slowly, or shows old data.

**1. Verify the SSE stream is healthy from a fresh tab.** Open browser dev tools → Network → reload the page → look for `/printer/stream` (type: `eventsource`). It should be `pending` with bytes ticking up. If it's `closed` or repeatedly reconnecting, the app may be overloaded.

**2. Check if a printer pushed recently.**

```bash
docker compose exec web curl -sS http://localhost:8000/admin/printers
```

The `last_seen_at` field on each printer tells you the freshest report. If it's recent (within a few seconds) but the UI doesn't show it, the issue is browser-side (refresh, then check dev tools).

**3. Check bridge logs.** A healthy bridge logs nothing during a print — that's normal. If it's logging `app_client.http_error` or `app_client.bad_status`, the app server is unreachable from the bridge:

```bash
docker compose logs --tail=200 bridge
```

`docker compose restart web bridge` often fixes a transient hang.

---

## Autobind not firing

Print started but the submission stayed Queued.

**Most common cause: filename didn't match the convention.** The `.3mf` file must start with `sub-<8 hex chars>-`. Walk to the printer, **Print history → tap the active print → File name**, copy that string, compare to what the submission detail page suggested.

Common mistakes:
- Operator forgot to rename and the file is something like `bracket.3mf`.
- Prefix matches the wrong submission (very rare — only if two submissions share the first 8 chars of their UUID).
- Bambu Studio added a suffix or version number that broke the prefix.

**To recover an already-running print:** flip the submission to Printing manually using the operator status control on the submission detail page. The auto-FINISH/FAILED transition still won't work for this submission (no binding), so flip it to Done or Failed by hand when the print completes.

For the next print, just save with the correct filename and the system will pick it up automatically.

---

## Bridge container won't start

`docker compose ps bridge` shows `Exited` or `Restarting`.

```bash
docker compose logs --tail=100 bridge
```

Common causes:

- **`RuntimeError: No printers configured`** — you didn't fill in any `PRINTER_1_*` env vars in `.env`. At least one printer block is required.
- **`RuntimeError: PRINTER_1_SLUG set but missing one of PRINTER_1_HOST / ...`** — partial config; fill in all four required fields per printer.
- **`ConnectionRefusedError` repeatedly** — the bridge couldn't reach the printer at all. See [Printer offline](#printer-stuck-offline) steps 2-3.

After fixing `.env`: `docker compose up -d bridge`.

---

## Backup didn't run

`Cloudflare R2 → print-queue-backups` should get a new file daily around 03:00 laptop-local time.

**1. Check backup container ran.**

```bash
docker compose ps backup
docker compose logs --tail=50 backup
```

If the container isn't running at all: `docker compose up -d backup`.

**2. Check if cron fired.** The container runs `crond` and shells out to `backup.sh`. If the backup script is failing, the log will show why:

```bash
docker compose exec backup cat /var/log/backup.log
```

Most common errors:
- **`InvalidAccessKeyId`** — `R2_ACCESS_KEY_ID` or `R2_SECRET_ACCESS_KEY` is wrong. Regenerate in Cloudflare dashboard.
- **`AccessDenied`** — the R2 API token doesn't have write scope on the bucket. Re-create the token with "Object Read & Write" scoped to `print-queue-backups`.
- **`pg_dump: connection refused`** — the `db` container isn't healthy. Check `docker compose ps db`.

**3. Manual backup right now.**

```bash
docker compose exec backup /backup.sh
```

If this succeeds, you have a current backup and you've identified that cron-or-something was the issue. Worth setting an alarm on your phone to manually check R2 weekly until cron stability is proven.

---

## Adding or removing a teammate

**Preferred: use the admin UI.** Sign in as an operator, go to **Users** in the top nav (`/admin/users`):

- **Add**: enter their `@spacex.com` email, pick a role (submitter or operator), click Add. They can request a magic link immediately — no restart.
- **Change role**: use the role dropdown on their row.
- **Remove access**: click Deactivate. Their submission history is preserved; they just can't sign in anymore. Existing session cookies remain valid until expiry (default 30 days) — if you need to force them out immediately, also run `docker compose restart web`.

You can't deactivate or demote yourself (guard against locking yourself out of admin).

**Fallback: the `.env` allowlist.** `ALLOWED_EMAILS` and `OPERATOR_EMAILS` in `.env` are read once at startup and seeded into the database — every email there is ensured to exist as an active user with the right role. This is a safety net so a fresh deploy or DB restore can't lock you out. It is NOT the runtime source of truth anymore; day-to-day user changes go through the admin UI. If you do edit `.env`, restart with `docker compose up -d web` to re-run the seed.

```bash
nano ~/print-queue/.env   # only needed for the bootstrap safety net
```

---

## Print failure alerts (Slack / Discord)

Print failures push to whichever webhook channels you've configured in `.env`:

- `SLACK_WEBHOOK_URL` — create at api.slack.com/apps → your app → Incoming Webhooks.
- `DISCORD_WEBHOOK_URL` — Discord Server Settings → Integrations → Webhooks → New Webhook → Copy URL.

Set either, both, or neither. After editing `.env`: `docker compose up -d web`. A failed webhook is logged (`webhook.slack.error` / `webhook.discord.error`) but never blocks a status change. To test, manually flip a submission to Failed and confirm the message lands.

---

## Stats and exports

Operator nav → **Stats** (`/admin/stats`): submission counts, failure rate, total/average/longest print time, and breakdowns. Use the date pickers to set a range, then **Download CSV** for a row-per-submission export (good for monthly reporting). Print durations are derived from the gap between a submission's Printing and Done/Failed status events, so they only populate for prints that went through the normal status flow (auto-bind or manual transitions).

---

## Adding a printer

For a third printer (you have config for two by default):

1. **Get the printer ready** on guest WiFi. Note its IP, serial, and 8-digit access code.
2. **Add a printer block** in `.env`:

```env
PRINTER_3_SLUG=P3
PRINTER_3_NAME=Bambu H2D #3
PRINTER_3_HOST=192.168.1.102
PRINTER_3_PORT=8883
PRINTER_3_SERIAL=00M00A000000002
PRINTER_3_ACCESS_CODE=12345678
```

3. **Restart the bridge:**

```bash
docker compose up -d bridge
```

4. **Visit the queue.** A new card appears automatically on first telemetry. The app auto-creates the `Printer` row in the database.

The bridge config supports up to 10 printers (`PRINTER_1` through `PRINTER_10`).

---

## Manually overriding submission status

The operator status control on the submission detail page lets you set any status. Use it when:

- Autobind missed a print (file wasn't named right) — manually mark Printing → Done.
- A print is canceled at the printer — mark Failed with a note.
- A submission is no longer needed — mark Cancelled.

Every change is logged in the submission's audit log (visible below the detail view).

---

## Reading logs

All logs are JSON, one line per event.

```bash
# All services, follow live
docker compose logs -f

# One service, last 100 lines
docker compose logs --tail=100 web
docker compose logs --tail=100 bridge

# Search across services
docker compose logs | grep "autobind"
```

For specific event types, search for the event name (string after the timestamp). Examples worth knowing:
- `email.resend.sent` — magic link successfully dispatched
- `email.resend.send_failed` — dispatch failed (status + body included)
- `autobind.bound` — autobind matched a submission to a starting print
- `autobind.no_match` — print started but filename didn't match any submission
- `printer.connected` — bridge connected to a printer's MQTT
- `printer.disconnected` — bridge lost MQTT connection
- `printers.marked_offline` — stale-checker flipped one or more printers to OFFLINE

Errors with stack traces also flow to Sentry — check `sentry.io` for grouped/aggregated views of repeating issues.

---

## Updating the app

When new code lands on `main` in GitHub:

```bash
cd ~/print-queue
git pull
docker compose up -d --build
```

If a migration is needed, the web container will run it automatically on start (see `alembic upgrade head` in the container entrypoint). Watch the logs to confirm:

```bash
docker compose logs --tail=50 web | grep -i alembic
```

If something looks wrong, **don't panic**. Roll back:

```bash
git log --oneline -10           # find the last known-good commit
git reset --hard <commit-hash>
docker compose up -d --build
```

A migration that's already been applied won't be re-run on rollback, but the schema may be ahead of the code. If you see `ProgrammingError: column "x" does not exist` in the logs after rollback, you need to downgrade the migration manually:

```bash
docker compose exec web alembic downgrade -1
```

(One step at a time. Test between each.)

---

## Things to set up later (not urgent)

- **Phone alarm**: weekly reminder to check R2 has new backups.
- **Sentry Slack/email alerts**: in Sentry, set up an alert rule for any new issue type. Currently you only see errors if you actively look.
- **Migrate to Raspberry Pi**: the laptop is a single point of failure. Pi 5 + UPS would be more reliable. Same docker-compose stack — no code changes.
- **R2 lifecycle rule**: set the bucket to auto-delete files older than 30 days so storage doesn't balloon. Cloudflare dashboard → R2 → bucket → Settings → Object Lifecycle.
