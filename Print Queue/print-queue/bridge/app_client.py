"""HTTP client that posts telemetry to the app server.

If the post fails (app container restarting, network blip), the caller
falls back to writing into the SQLite buffer; the bridge's main loop
drains the buffer on a timer when conditions improve.
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger(__name__)


class AppClient:
    def __init__(self, telemetry_url: str, shared_token: str, timeout: float = 5.0):
        self.url = telemetry_url
        self.token = shared_token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def post(self, payload: dict) -> bool:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Bridge-Token"] = self.token
        try:
            r = await self._client.post(self.url, json=payload, headers=headers)
            if r.status_code >= 300:
                log.warning("app_client.bad_status", status=r.status_code, body=r.text[:200])
                return False
            return True
        except httpx.HTTPError as e:
            log.info("app_client.http_error", error=str(e))
            return False

    async def close(self) -> None:
        await self._client.aclose()
