#!/usr/bin/env bash
# Installs the cloud-telemetry bridge into ./bridge/
# Run from your project root:  bash apply_cloud_bridge.sh
set -e
if [ ! -d bridge ]; then echo "ERROR: run this from ~/print-queue (no bridge/ dir here)"; exit 1; fi
echo "Writing updated bridge files..."
cat > bridge/cloud_auth.py << '__PQFILE_0__'
"""Bambu Cloud authentication.

Handles the email+password (+ 2FA email-code) login flow against Bambu's
cloud API, persists the resulting tokens to disk, refreshes them, and
derives the MQTT username from the access token.

The bridge runs headless using a saved token. The interactive 2FA step is
only needed once (via ``python -m bridge.login``), or rarely if both the
access token and refresh token fully expire.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)

_API_HOSTS = {"us": "api.bambulab.com", "global": "api.bambulab.com", "china": "api.bambulab.cn"}
_MQTT_HOSTS = {
    "us": "us.mqtt.bambulab.com",
    "global": "us.mqtt.bambulab.com",
    "china": "cn.mqtt.bambulab.com",
}

_DEFAULT_TTL = 7776000  # ~3 months, Bambu's documented token lifetime


def api_host(region: str) -> str:
    return _API_HOSTS.get(region.lower(), "api.bambulab.com")


def mqtt_host(region: str) -> str:
    return _MQTT_HOSTS.get(region.lower(), "us.mqtt.bambulab.com")


class AuthError(Exception):
    pass


class TwoFactorRequired(AuthError):
    """Raised when login needs an email code but no callback was supplied."""


@dataclass
class CloudToken:
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds
    region: str

    @property
    def is_expired(self) -> bool:
        # Refresh proactively a day early.
        return time.time() >= (self.expires_at - 86400)

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "region": self.region,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CloudToken:
        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token", ""),
            expires_at=float(d.get("expires_at", 0)),
            region=d.get("region", "us"),
        )


def _uid_from_jwt(access_token: str) -> str:
    """Fallback: derive u_<uid> by decoding a JWT access token, if it is one."""
    try:
        payload_b64 = access_token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except Exception as e:
        raise AuthError(f"access token is not a decodable JWT: {e}") from e
    username = payload.get("username")
    if isinstance(username, str) and username.startswith("u_"):
        return username
    uid = payload.get("uid") or payload.get("sub")
    if uid:
        return f"u_{uid}"
    raise AuthError("access token did not contain a username/uid claim")


def _fetch_uid(token: CloudToken) -> str:
    """Ask Bambu's API for the account uid, returning the u_<uid> MQTT username."""
    candidates = [
        f"https://{api_host(token.region)}/v1/design-user-service/my/preference",
        "https://makerworld.com/api/v1/design-user-service/my/preference",
    ]
    last_err = "no endpoint responded"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url in candidates:
            try:
                r = client.get(url, headers=headers)
            except httpx.HTTPError as e:
                last_err = f"{url}: {e}"
                continue
            if r.status_code >= 300:
                last_err = f"{url}: HTTP {r.status_code} {r.text[:120]}"
                continue
            uid = r.json().get("uid")
            if uid is not None:
                return f"u_{uid}"
            last_err = f"{url}: response had no uid"
    raise AuthError(f"could not fetch account uid: {last_err}")


def mqtt_username(token: CloudToken) -> str:
    """Resolve the MQTT username (u_<uid>) for this account.

    Primary path queries Bambu's API (works regardless of token format);
    falls back to decoding the token as a JWT.
    """
    try:
        return _fetch_uid(token)
    except AuthError as api_err:
        log.warning("cloud.uid_api_failed", error=str(api_err))
        try:
            return _uid_from_jwt(token.access_token)
        except AuthError:
            raise api_err from None


def _token_from_response(data: dict, region: str) -> CloudToken:
    access = data.get("accessToken") or ""
    if not access:
        raise AuthError(f"response contained no access token: {str(data)[:200]}")
    refresh_tok = data.get("refreshToken") or access
    expires_in = int(data.get("expiresIn") or 0) or _DEFAULT_TTL
    return CloudToken(
        access_token=access,
        refresh_token=refresh_tok,
        expires_at=time.time() + expires_in,
        region=region,
    )


def login(
    email: str,
    password: str,
    code_callback: Callable[[], str] | None = None,
    region: str = "us",
) -> CloudToken:
    """Log in to Bambu cloud, handling the email verification-code step.

    ``code_callback`` is invoked when Bambu requires a 2FA email code; it
    should return the code the user received.
    """
    base = f"https://{api_host(region)}"
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            f"{base}/v1/user-service/user/login",
            json={"account": email, "password": password},
        )
        if r.status_code >= 300:
            raise AuthError(f"login failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        login_type = (data.get("loginType") or "").strip()

        # Direct success: token present and no further challenge.
        if data.get("accessToken") and not login_type:
            return _token_from_response(data, region)

        # A verification code is required (emailed to the user).
        if code_callback is None:
            raise TwoFactorRequired(
                "Bambu requires an email verification code. "
                "Run `python -m bridge.login` interactively."
            )
        code = code_callback().strip()
        r2 = client.post(
            f"{base}/v1/user-service/user/login",
            json={"account": email, "code": code},
        )
        if r2.status_code >= 300:
            raise AuthError(f"code verification failed: HTTP {r2.status_code} {r2.text[:200]}")
        return _token_from_response(r2.json(), region)


def refresh(token: CloudToken) -> CloudToken:
    if not token.refresh_token:
        raise AuthError("no refresh token available")
    base = f"https://{api_host(token.region)}"
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            f"{base}/v1/user-service/user/refreshtoken",
            json={"refreshToken": token.refresh_token},
        )
        if r.status_code >= 300:
            raise AuthError(f"token refresh failed: HTTP {r.status_code} {r.text[:200]}")
        return _token_from_response(r.json(), token.region)


def save_token(path: str, token: CloudToken) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(token.to_dict(), f)
    os.replace(tmp, path)


def load_token(path: str) -> CloudToken | None:
    try:
        with open(path) as f:
            return CloudToken.from_dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return None
__PQFILE_0__
echo "  wrote bridge/cloud_auth.py"
cat > bridge/cloud_client.py << '__PQFILE_1__'
"""One MQTT connection to Bambu's cloud broker, shared across all printers.

The local bridge opens one connection per printer (to each printer's own
broker). The cloud broker is shared, so we open a single authenticated
connection and subscribe to every printer's ``device/<serial>/report``
topic, routing each message to the right per-printer accumulator.

If the broker rejects our credentials (expired token), we refresh the
token once and reconnect; if that fails, we log a clear instruction to
re-run the interactive login.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import certifi
import paho.mqtt.client as mqtt
import structlog

from bridge.cloud_auth import CloudToken, mqtt_host, mqtt_username, refresh, save_token
from bridge.config import PrinterConfig
from bridge.parser import PrinterAccumulator

log = structlog.get_logger(__name__)

OnSnapshot = Callable[[dict[str, Any]], Awaitable[None]]

_RELOGIN_HINT = "Re-run: docker compose run --rm bridge python -m bridge.login"


class CloudConnection:
    def __init__(
        self,
        printers: tuple[PrinterConfig, ...],
        token: CloudToken,
        token_path: str,
        on_snapshot: OnSnapshot,
        loop: asyncio.AbstractEventLoop,
    ):
        self.printers = printers
        self.token = token
        self.token_path = token_path
        self.on_snapshot = on_snapshot
        self.loop = loop
        self.accumulators = {
            p.serial: PrinterAccumulator(slug=p.slug, serial=p.serial) for p in printers
        }
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="printq-bridge-cloud",
        )
        self._username = mqtt_username(token)
        self._apply_credentials()
        ctx = ssl.create_default_context(cafile=certifi.where())
        self._client.tls_set_context(ctx)
        self._client.reconnect_delay_set(min_delay=2, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._connected = asyncio.Event()
        self._closed = False
        self._refresh_attempted = False
        self._refresh_task: asyncio.Task | None = None

    def _apply_credentials(self) -> None:
        self._client.username_pw_set(self._username, self.token.access_token)
        log.info("cloud.credentials_set", username=self._username)

    def start(self) -> None:
        host = mqtt_host(self.token.region)
        log.info("cloud.connecting", host=host, printer_count=len(self.printers))
        try:
            self._client.connect_async(host, 8883, keepalive=60)
        except OSError as e:
            log.warning("cloud.initial_connect_failed", error=str(e))
        self._client.loop_start()

    def request_pushall(self) -> None:
        if not self._connected.is_set():
            return
        for p in self.printers:
            try:
                self._client.publish(
                    f"device/{p.serial}/request",
                    json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
                    qos=0,
                )
            except Exception as e:
                log.warning("cloud.pushall_failed", slug=p.slug, error=str(e))

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        if rc == 0:
            log.info("cloud.connected")
            self._connected.set()
            self._refresh_attempted = False
            for p in self.printers:
                client.subscribe(f"device/{p.serial}/report", qos=0)
                log.info("cloud.subscribed", slug=p.slug, serial=p.serial)
            self.request_pushall()
        elif rc in (4, 5, 134, 135):  # bad credentials / not authorized
            log.warning("cloud.auth_rejected", reason=str(reason_code))
            self.loop.call_soon_threadsafe(self._schedule_refresh)
        else:
            log.warning("cloud.connect_failed", reason=str(reason_code))

    def _schedule_refresh(self) -> None:
        if self._refresh_attempted:
            log.error("cloud.auth_failed_permanently", hint=_RELOGIN_HINT)
            return
        self._refresh_attempted = True
        self._refresh_task = asyncio.create_task(self._do_refresh())

    async def _do_refresh(self) -> None:
        try:
            new_token = await asyncio.to_thread(refresh, self.token)
        except Exception as e:
            log.error("cloud.refresh_failed", error=str(e), hint=_RELOGIN_HINT)
            return
        self.token = new_token
        save_token(self.token_path, new_token)
        self._apply_credentials()
        log.info("cloud.token_refreshed")
        try:
            self._client.reconnect()
        except Exception as e:
            log.warning("cloud.reconnect_failed", error=str(e))

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None):
        log.info("cloud.disconnected", reason=str(reason_code))
        self._connected.clear()

    def _on_message(self, _client, _userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) < 2:
            return
        serial = parts[1]
        acc = self.accumulators.get(serial)
        if acc is None:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        print_section = payload.get("print") if isinstance(payload, dict) else None
        if not isinstance(print_section, dict):
            return
        acc.apply(print_section)
        snapshot = acc.snapshot()
        asyncio.run_coroutine_threadsafe(self.on_snapshot(snapshot), self.loop)

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
__PQFILE_1__
echo "  wrote bridge/cloud_client.py"
cat > bridge/login.py << '__PQFILE_2__'
"""Interactive one-time login for Bambu cloud (handles the 2FA email code).

Run once:

    docker compose run --rm bridge python -m bridge.login

Reads BAMBU_EMAIL / BAMBU_PASSWORD from the environment (falling back to
prompts), performs the login including the emailed verification code, and
writes the resulting token to BAMBU_TOKEN_PATH so the bridge runs headless
afterwards.
"""

from __future__ import annotations

import getpass
import os
import sys

from bridge import cloud_auth


def main() -> int:
    email = os.environ.get("BAMBU_EMAIL") or input("Bambu account email: ").strip()
    password = os.environ.get("BAMBU_PASSWORD") or getpass.getpass("Bambu password: ")
    region = os.environ.get("BAMBU_REGION", "us")
    token_path = os.environ.get("BAMBU_TOKEN_PATH", "/data/bambu_token.json")

    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        return 1

    def code_callback() -> str:
        print(
            f"\nBambu just emailed a verification code to {email}."
            "\nCheck your inbox (and spam folder).",
            flush=True,
        )
        return input("Enter the verification code: ").strip()

    try:
        token = cloud_auth.login(email, password, code_callback, region=region)
        username = cloud_auth.mqtt_username(token)
    except cloud_auth.AuthError as e:
        print(f"\nLogin failed: {e}", file=sys.stderr)
        return 1

    cloud_auth.save_token(token_path, token)
    print(f"\n\u2713 Success. Token saved to {token_path}")
    print(f"  Account MQTT id: {username}")
    print("  Now start the bridge:  docker compose up -d bridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
__PQFILE_2__
echo "  wrote bridge/login.py"
cat > bridge/config.py << '__PQFILE_3__'
"""Bridge configuration.

Configured via env vars. Multiple printers are declared by repeating
PRINTER_<N>_* groups (1, 2, ...).

Two modes (BRIDGE_MODE):
- "local": connect directly to each printer's LAN MQTT broker (needs
  HOST + ACCESS_CODE, and the printer in LAN/Developer mode).
- "cloud": connect to Bambu's cloud broker with a Bambu account token and
  subscribe to each printer's telemetry (printers stay in normal cloud
  mode; Handy keeps working). Only the SERIAL is needed per printer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PrinterConfig:
    slug: str
    name: str
    host: str
    port: int
    serial: str
    access_code: str  # 8-digit code from the printer's network settings (local mode)


@dataclass(frozen=True)
class BridgeConfig:
    mode: str  # "local" or "cloud"
    app_telemetry_url: str
    bridge_shared_token: str
    buffer_db_path: str
    printers: tuple[PrinterConfig, ...]
    poll_request_interval: int  # seconds between explicit pushall refreshes
    log_level: str
    # Cloud-mode settings
    bambu_email: str
    bambu_password: str
    bambu_region: str
    bambu_token_path: str

    @classmethod
    def from_env(cls) -> BridgeConfig:
        mode = os.environ.get("BRIDGE_MODE", "local").strip().lower()
        is_cloud = mode == "cloud"

        printers = []
        for i in range(1, 11):  # support up to 10 printers
            slug = os.environ.get(f"PRINTER_{i}_SLUG")
            if not slug:
                continue
            serial = os.environ.get(f"PRINTER_{i}_SERIAL")
            host = os.environ.get(f"PRINTER_{i}_HOST")
            access_code = os.environ.get(f"PRINTER_{i}_ACCESS_CODE")
            if not serial:
                raise RuntimeError(f"PRINTER_{i}_SLUG set but PRINTER_{i}_SERIAL missing")
            if not is_cloud and not (host and access_code):
                raise RuntimeError(
                    f"PRINTER_{i} (local mode) needs PRINTER_{i}_HOST and "
                    f"PRINTER_{i}_ACCESS_CODE"
                )
            printers.append(
                PrinterConfig(
                    slug=slug.strip(),
                    name=os.environ.get(f"PRINTER_{i}_NAME", slug).strip(),
                    host=(host or "").strip(),
                    port=int(os.environ.get(f"PRINTER_{i}_PORT", "8883")),
                    serial=serial.strip(),
                    access_code=(access_code or "").strip(),
                )
            )
        if not printers:
            raise RuntimeError("No printers configured (set PRINTER_1_SLUG etc.)")

        if is_cloud and not os.environ.get("BAMBU_EMAIL"):
            raise RuntimeError("BRIDGE_MODE=cloud requires BAMBU_EMAIL")

        return cls(
            mode=mode,
            app_telemetry_url=os.environ.get(
                "APP_TELEMETRY_URL", "http://web:8000/internal/telemetry"
            ),
            bridge_shared_token=os.environ.get("BRIDGE_SHARED_TOKEN", ""),
            buffer_db_path=os.environ.get("BRIDGE_BUFFER_PATH", "/data/buffer.sqlite3"),
            printers=tuple(printers),
            poll_request_interval=int(os.environ.get("BRIDGE_PUSHALL_INTERVAL", "300")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            bambu_email=os.environ.get("BAMBU_EMAIL", "").strip(),
            bambu_password=os.environ.get("BAMBU_PASSWORD", ""),
            bambu_region=os.environ.get("BAMBU_REGION", "us").strip().lower(),
            bambu_token_path=os.environ.get("BAMBU_TOKEN_PATH", "/data/bambu_token.json"),
        )
__PQFILE_3__
echo "  wrote bridge/config.py"
cat > bridge/main.py << '__PQFILE_4__'
"""Bridge service entry point.

Wires up logging, builds the AppClient, opens printer connections
(per-printer in local mode, or a single shared connection in cloud mode),
and runs forever. Loops:

- on_snapshot (fired by each MQTT report): post to app; on failure, buffer.
- drain_loop (every few seconds): try to drain the buffer.
- pushall_loop (every BRIDGE_PUSHALL_INTERVAL seconds): ask each printer
  for a full state refresh, defensive against missed incremental updates.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog

from bridge import cloud_auth
from bridge.app_client import AppClient
from bridge.buffer import TelemetryBuffer
from bridge.config import BridgeConfig
from bridge.printer import PrinterConnection


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


async def _build_cloud_connection(config, log, on_snapshot, loop, stop_event):
    """Load (and refresh) the saved token, then build the cloud connection.

    If no token is present, idle and re-check until the user runs the
    interactive login (which drops the token onto the shared volume), so we
    never crash-loop.
    """
    from bridge.cloud_client import CloudConnection

    token = cloud_auth.load_token(config.bambu_token_path)
    while token is None and not stop_event.is_set():
        log.error(
            "cloud.no_token",
            path=config.bambu_token_path,
            hint="Run: docker compose run --rm bridge python -m bridge.login",
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15.0)
        except TimeoutError:
            pass
        token = cloud_auth.load_token(config.bambu_token_path)
    if token is None:
        return None

    if token.is_expired and token.refresh_token:
        try:
            token = await asyncio.to_thread(cloud_auth.refresh, token)
            cloud_auth.save_token(config.bambu_token_path, token)
            log.info("cloud.token_refreshed_on_start")
        except Exception as e:
            log.warning("cloud.startup_refresh_failed", error=str(e))

    return CloudConnection(
        printers=config.printers,
        token=token,
        token_path=config.bambu_token_path,
        on_snapshot=on_snapshot,
        loop=loop,
    )


async def run(config: BridgeConfig) -> None:
    log = structlog.get_logger("bridge")
    log.info(
        "bridge.start",
        mode=config.mode,
        printer_count=len(config.printers),
        telemetry_url=config.app_telemetry_url,
    )

    buffer = TelemetryBuffer(config.buffer_db_path)
    app_client = AppClient(config.app_telemetry_url, config.bridge_shared_token)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def on_snapshot(payload: dict) -> None:
        ok = await app_client.post(payload)
        if not ok:
            await buffer.push(payload)

    def _signal_handler():
        log.info("bridge.signal_received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Build connector(s). Both modes expose start()/request_pushall()/stop().
    if config.mode == "cloud":
        conn = await _build_cloud_connection(config, log, on_snapshot, loop, stop_event)
        connectors = [conn] if conn is not None else []
    else:
        connectors = [PrinterConnection(p, on_snapshot, loop) for p in config.printers]

    for c in connectors:
        c.start()

    async def drain_loop():
        while not stop_event.is_set():
            try:
                rows = await buffer.drain()
                if rows:
                    drained_ids = []
                    for row_id, payload in rows:
                        if await app_client.post(payload):
                            drained_ids.append(row_id)
                        else:
                            break  # leave the rest for later
                    if drained_ids:
                        await buffer.delete(drained_ids)
                        log.info("buffer.drained", count=len(drained_ids))
            except Exception as e:
                log.warning("drain.error", error=str(e))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def pushall_loop():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.poll_request_interval)
                break  # stop_event fired
            except TimeoutError:
                for c in connectors:
                    c.request_pushall()

    drain_task = asyncio.create_task(drain_loop())
    pushall_task = asyncio.create_task(pushall_loop())
    await stop_event.wait()

    log.info("bridge.shutting_down")
    drain_task.cancel()
    pushall_task.cancel()
    for c in connectors:
        await c.stop()
    await app_client.close()
    log.info("bridge.stopped")


def main() -> None:
    config = BridgeConfig.from_env()
    _configure_logging(config.log_level)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
__PQFILE_4__
echo "  wrote bridge/main.py"
cat > bridge/Dockerfile << '__PQFILE_5__'
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install --upgrade pip && \
    pip install \
        "paho-mqtt>=2.1" \
        "httpx>=0.27" \
        "structlog>=24.4" \
        "certifi>=2024.8.30"

COPY bridge/ /app/bridge/

RUN useradd --create-home --shell /bin/bash bridge \
    && mkdir -p /data \
    && chown -R bridge:bridge /app /data
USER bridge

CMD ["python", "-m", "bridge.main"]
__PQFILE_5__
echo "  wrote bridge/Dockerfile"
echo ""
echo "Done. Next: docker compose up -d --build"
