# First-Deploy Verification

Step-by-step verification for the first `docker compose up` on the laptop.
Don't skip ahead — each step depends on the previous ones working.

If anything fails, see `RUNBOOK.md` for troubleshooting.

---

## Pre-flight (before starting Docker)

- [ ] **Windows host configured**
  - Sleep set to Never (Settings → Power & battery)
  - Lid-close action set to "Do nothing" when plugged in
  - Active hours for Windows Update set to 6 AM – 1 AM

- [ ] **WSL2 Ubuntu installed and Docker engine inside**
  ```bash
  docker --version    # should print "Docker version 27.x or newer"
  docker compose version
  ```

- [ ] **Project unzipped to `~/print-queue` inside WSL2**
  ```bash
  cd ~/print-queue
  ls -la             # should see .env.example, docker-compose.yml, app/, bridge/
  ```

- [ ] **`.env` filled in completely** — generate the three secrets, paste the six saved credentials, fill in the printer blocks
  ```bash
  cp .env.example .env
  python3 -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(48))"
  python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))"
  python3 -c "import secrets; print('BRIDGE_SHARED_TOKEN=' + secrets.token_urlsafe(32))"
  nano .env
  ```
  Make sure every `change-me-*` placeholder is replaced. The `ALLOWED_EMAILS` and `OPERATOR_EMAILS` lists at minimum need your own work email.

- [ ] **`.env` permissions tightened** (it has all your secrets)
  ```bash
  chmod 600 .env
  ```

---

## Phase 1: Start the stack

```bash
docker compose up -d --build
```

First build pulls images and compiles the web container — this takes 2-5 minutes. Subsequent restarts take ~10 seconds.

- [ ] **All six services are `Up`**
  ```bash
  docker compose ps
  ```
  Expected: `db`, `web`, `caddy`, `bridge`, `cloudflared`, `backup` all in `Up` state. `backup` runs cron in the foreground, which counts as Up.

- [ ] **`web` container completed its migrations**
  ```bash
  docker compose logs web | grep -i alembic
  ```
  Should see `Running upgrade -> 0003` (or whatever the latest revision is) and no errors.

- [ ] **`/healthz` returns OK**
  ```bash
  docker compose exec web curl -sS http://localhost:8000/healthz
  ```
  Expected: `{"status":"ok","db":"ok"}`

---

## Phase 2: Verify the public route

- [ ] **Tunnel is registered** — Cloudflare dashboard → Zero Trust → Networks → Tunnels → `print-queue` shows status **Healthy** with at least one active connector.

- [ ] **Domain resolves to Cloudflare**
  ```bash
  dig +short 3dprinterqueue.com
  ```
  Should return Cloudflare IPs (104.x or similar), not a SpaceX or local IP.

- [ ] **HTTPS site responds**
  ```bash
  curl -sS https://3dprinterqueue.com/healthz
  ```
  Expected: `{"status":"ok","db":"ok"}` — same as the internal check, but coming through Cloudflare.

- [ ] **Browser shows the login page** — open `https://3dprinterqueue.com` in any browser on any network. You should be redirected to `/login`.

---

## Phase 3: Auth round-trip

This is the moment of truth for Resend deliverability — finding it doesn't work on day 1 is much better than discovering it during a real outage.

- [ ] **Request a magic link from your own work email**
  - On `/login`, type `anthony.echeman@spacex.com`, submit.
  - You should see a confirmation page reading something like "Check your email."

- [ ] **App logs the send**
  ```bash
  docker compose logs --tail=20 web | grep email.resend
  ```
  Should see `email.resend.sent` with your email and a `message_id`. If it shows `email.resend.no_api_key.console_fallback`, you forgot to fill in `RESEND_API_KEY`.

- [ ] **Resend dashboard shows the email as Delivered** — `resend.com → Emails`, top of the list.
  - **Delivered** = good, but corp filters may still spam-bin it.
  - **Bounced** = corp blocked it; raise an IT ticket to allowlist `noreply@3dprinterqueue.com`.

- [ ] **Email arrives in your @spacex.com inbox** — check inbox, then spam/junk, then quarantine. If it never arrives, this is the blocker — the rest of the system works but no one can sign in until it's fixed. See `RUNBOOK.md` → "Magic-link not arriving".

- [ ] **Click the link, get signed in** — you should land on the queue page with your name in the top-right.

---

## Phase 4: Submission round-trip

- [ ] **Submit a test STEP file**
  - Click "+ New submission"
  - Part name: `test-day-1`
  - Upload any small STEP file (the project has none — find one in your downloads or just rename a small text file to `.step` for this test; the upload doesn't validate STEP content yet).
  - Submit.

- [ ] **Submission appears in the queue** with status **Queued**, you as submitter.

- [ ] **Suggested filename is shown on the detail page** in the format `sub-<8 hex>-test-day-1.3mf`.

- [ ] **STEP download works** — click the download link on the detail page, confirm the file you uploaded comes back unchanged.

- [ ] **Delete the test submission** — operator delete action.

---

## Phase 5: Printer telemetry

This is the most likely place to find real-hardware surprises.

- [ ] **Bridge container started and didn't crash**
  ```bash
  docker compose logs --tail=30 bridge | grep -E "bridge.start|printer.connected|printer.connect_refused"
  ```
  Healthy: `bridge.start` followed by `printer.connected` for each printer slug. If you see `connect_refused`, the printer IP / access code / serial is wrong, or the printer is off, or it's on a different network.

- [ ] **Printer cards render on the queue page** — you should see one card per configured printer at the top of the queue. Initially they may show OFFLINE; on first telemetry that flips to whatever the printer's real state is (most likely IDLE).

- [ ] **Telemetry endpoint rejects unauthenticated POSTs**
  ```bash
  docker compose exec web curl -sS -X POST http://localhost:8000/internal/telemetry \
    -H "Content-Type: application/json" \
    -d '{"printer_slug":"x","printer_serial":"y","ts":"2026-01-01T00:00:00Z","status":"IDLE"}'
  ```
  Expected: 401 Unauthorized. (The bridge has the right token internally; this curl from inside `web` deliberately doesn't.)

- [ ] **Telemetry endpoint accepts the bridge's token** — verified implicitly by the printer cards updating in real time. Open dev tools → Network → look for `/printer/stream` (type `eventsource`). Watching messages tick in confirms SSE works end to end.

- [ ] **Stale detection works** — temporarily turn off one of the printers (or unplug its Ethernet/disable WiFi). Within ~30 seconds the card should flip to OFFLINE. Turn the printer back on; within a minute it should flip back to IDLE or whatever its real state is.

---

## Phase 6: Auto-bind (optional, requires a real print)

- [ ] **Submit a real STEP**, download it, slice it in Bambu Studio.
- [ ] **Save the `.3mf` with the suggested filename** — copy exactly from the submission detail page.
- [ ] **Send to printer** via Bambu Studio's normal flow.
- [ ] **Within ~5 seconds, the submission card flips to Printing** — the autobind worked.
- [ ] **On clean finish, the card flips to Done** automatically. Check the submission's audit log to see the autobind events.

If autobind didn't fire: see `RUNBOOK.md` → "Autobind not firing".

---

## Phase 7: Backups (verify the next morning)

The backup container runs cron and dumps to R2 at 03:00. Verify this works on day 2:

- [ ] **The backup container is still running** the next morning.
  ```bash
  docker compose ps backup
  ```

- [ ] **A new object exists in R2** — Cloudflare dashboard → R2 → `print-queue-backups`. There should be a file named like `print-queue-YYYY-MM-DD.sql.gz`.

- [ ] **The backup script log shows a successful run**
  ```bash
  docker compose exec backup cat /var/log/backup.log | tail -20
  ```

If the backup didn't run, see `RUNBOOK.md` → "Backup didn't run".

---

## Phase 8: Push to GitHub (optional but recommended)

Source code in GitHub makes recovery easier if the laptop gets wiped, and the CI workflow runs your test suite on every push.

```bash
sudo apt install -y git gh
gh auth login   # choose: GitHub.com → HTTPS → Yes → Login with browser
git config --global user.email "your-personal-email@example.com"
git config --global user.name "Anthony Echeman"

cd ~/print-queue

# Sanity check: confirm .env is gitignored
git check-ignore -v .env    # should print ".gitignore:.... .env"

git init
git branch -M main
git add .
git status | grep -i '\.env'   # MUST be empty. If your .env appears, STOP.
git commit -m "Initial commit: M1 through M6"
git remote add origin https://github.com/recheman0621-hub/print-queue.git
git push -u origin main
```

- [ ] **CI ran and is green** — GitHub repo → Actions tab. First workflow run should pass on all checks.

---

## You're done

Everything above passing means the system is production-ready. From here on it's normal operation — submitters submit, you slice and print, telemetry flows.

**Final action**: bookmark these in your browser for fast access:
- `https://3dprinterqueue.com` — the app
- `https://sentry.io` — errors
- `https://dash.cloudflare.com` — tunnel + R2
- `https://resend.com/emails` — email delivery logs
- `https://github.com/recheman0621-hub/print-queue` — source

And put `RUNBOOK.md` in a tab you don't close.
