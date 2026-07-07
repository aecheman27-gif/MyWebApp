"""Send notifications to Slack and Discord webhook URLs.

Both providers accept POSTed JSON; the payload shapes differ. We send a
short, useful message to whichever channels are configured. If both are
configured, both fire. If neither is configured, the function is a no-op.

Failures are logged (and reported to Sentry via the structlog → logging
bridge) but never raised — a webhook outage shouldn't crash a status
transition.
"""

from __future__ import annotations

import httpx
import structlog

from app.config import Settings

log = structlog.get_logger(__name__)


async def notify_failure(
    settings: Settings,
    *,
    submission_part_name: str,
    submitter_email: str,
    printer_name: str | None,
    error_code: int | None,
    site_url: str,
    submission_url_path: str,
) -> None:
    """Fan out a print-failure notification to all configured channels."""
    submission_url = f"{site_url.rstrip('/')}{submission_url_path}"
    text = f":x: Print *failed* — `{submission_part_name}` " f"(submitter: {submitter_email})"
    if printer_name:
        text += f" on _{printer_name}_"
    if error_code:
        text += f" — error code {error_code}"

    async with httpx.AsyncClient(timeout=5.0) as client:
        if settings.slack_webhook_url:
            await _post_slack(client, settings.slack_webhook_url, text, submission_url)
        if settings.discord_webhook_url:
            await _post_discord(client, settings.discord_webhook_url, text, submission_url)


async def _post_slack(client: httpx.AsyncClient, url: str, text: str, link: str) -> None:
    """Slack incoming-webhook payload."""
    payload = {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open submission"},
                        "url": link,
                    }
                ],
            },
        ],
    }
    try:
        r = await client.post(url, json=payload)
        if r.status_code >= 300:
            log.warning("webhook.slack.bad_status", status=r.status_code, body=r.text[:200])
    except httpx.HTTPError as e:
        log.warning("webhook.slack.error", error=str(e))


async def _post_discord(client: httpx.AsyncClient, url: str, text: str, link: str) -> None:
    """Discord webhook payload."""
    # Discord doesn't render Slack-style `:emoji:` shortcodes consistently;
    # convert the Slack-friendly markers to plain unicode.
    discord_text = text.replace(":x:", "❌").replace("*", "**")
    payload = {
        "content": f"{discord_text}\n{link}",
        "allowed_mentions": {"parse": []},
    }
    try:
        r = await client.post(url, json=payload)
        if r.status_code >= 300:
            log.warning("webhook.discord.bad_status", status=r.status_code, body=r.text[:200])
    except httpx.HTTPError as e:
        log.warning("webhook.discord.error", error=str(e))
