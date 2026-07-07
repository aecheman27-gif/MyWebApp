"""Status-change and comment notification emails.

Each function builds a small, readable HTML+text email and dispatches it
through the existing Resend client. If RESEND_API_KEY is empty the call
becomes a console log, matching the rest of the email module.
"""

from __future__ import annotations

import httpx
import structlog

from app.config import Settings
from app.email.resend_client import RESEND_API_URL

log = structlog.get_logger(__name__)


def _wrap_html(
    title: str, body_html: str, cta_url: str | None, cta_text: str = "Open in Print Queue"
) -> str:
    """Reusable card layout, same look as the magic-link email."""
    BG = "#f4f5f7"
    CARD = "#ffffff"
    TEXT = "#1a1d24"
    MUTED = "#6b7280"
    BORDER = "#e5e7eb"
    PRIMARY = "#4f8cff"
    PRIMARY_TEXT = "#ffffff"

    cta_block = ""
    if cta_url:
        cta_block = f"""\
        <tr>
          <td align="center" style="padding:8px 32px 24px 32px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td bgcolor="{PRIMARY}" style="background:{PRIMARY};border-radius:6px;">
                  <a href="{cta_url}" target="_blank" rel="noopener" style="display:inline-block;padding:12px 24px;color:{PRIMARY_TEXT};font-size:14px;font-weight:600;text-decoration:none;border-radius:6px;line-height:1;">
                    {cta_text}
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
"""
    return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:{BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:{BG};padding:32px 16px;">
  <tr><td align="center">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:520px;background:{CARD};border:1px solid {BORDER};border-radius:10px;overflow:hidden;">
      <tr><td style="padding:28px 32px 16px 32px;">
        <h1 style="margin:0 0 8px 0;font-size:18px;font-weight:600;color:{TEXT};line-height:1.3;">{title}</h1>
        <div style="color:{TEXT};font-size:14px;line-height:1.55;">{body_html}</div>
      </td></tr>
      {cta_block}
      <tr><td style="padding:16px 32px 24px 32px;border-top:1px solid {BORDER};">
        <p style="margin:0;color:{MUTED};font-size:11px;line-height:1.4;">
          You're getting this because email notifications are on for your account.
          Reply to this email won't reach anyone — manage notifications in your profile.
        </p>
      </td></tr>
    </table>
  </td></tr>
</table></body></html>"""


async def _send(settings: Settings, to_email: str, subject: str, html: str, text: str) -> None:
    if not settings.resend_api_key:
        log.warning(
            "email.notify.no_api_key.console_fallback",
            to=to_email,
            subject=subject,
        )
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.email_from,
                "to": [to_email],
                "subject": subject,
                "text": text,
                "html": html,
            },
        )
    if resp.status_code >= 300:
        log.error(
            "email.notify.send_failed",
            status=resp.status_code,
            body=resp.text[:200],
            to=to_email,
        )
        return
    log.info("email.notify.sent", to=to_email, subject=subject)


async def send_status_change_email(
    settings: Settings,
    *,
    to_email: str,
    part_name: str,
    new_status: str,
    site_url: str,
    submission_url_path: str,
    actor_note: str | None = None,
) -> None:
    full_url = f"{site_url.rstrip('/')}{submission_url_path}"
    pretty_status = new_status.replace("_", " ").title()
    subject = f"[{pretty_status}] {part_name}"

    body_lines = [
        f"<p>Your submission <strong>{part_name}</strong> is now "
        f"<strong>{pretty_status}</strong>.</p>",
    ]
    if actor_note:
        body_lines.append(f"<p>Note: {actor_note}</p>")

    html = _wrap_html(
        f"{part_name}: {pretty_status}",
        "\n".join(body_lines),
        cta_url=full_url,
        cta_text="Open submission",
    )
    text = f"Your submission '{part_name}' is now {pretty_status}.\n\n" f"{full_url}\n"
    if actor_note:
        text = (
            f"Your submission '{part_name}' is now {pretty_status}.\n\n"
            f"Note: {actor_note}\n\n{full_url}\n"
        )
    await _send(settings, to_email, subject, html, text)


async def send_comment_email(
    settings: Settings,
    *,
    to_email: str,
    part_name: str,
    author_email: str,
    body: str,
    site_url: str,
    submission_url_path: str,
) -> None:
    full_url = f"{site_url.rstrip('/')}{submission_url_path}"
    subject = f"[Comment] {part_name}"

    # Escape minimal HTML and convert newlines.
    safe_body = (
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    )

    body_html = (
        f"<p><strong>{author_email}</strong> commented on "
        f"<strong>{part_name}</strong>:</p>"
        f'<blockquote style="margin:12px 0;padding:8px 12px;border-left:3px solid #4f8cff;color:#1a1d24;background:#f4f5f7;border-radius:4px;font-size:13px;">{safe_body}</blockquote>'
    )
    html = _wrap_html(
        f"New comment on {part_name}",
        body_html,
        cta_url=full_url,
        cta_text="Open submission",
    )
    text = f"{author_email} commented on '{part_name}':\n\n" f"{body}\n\n" f"{full_url}\n"
    await _send(settings, to_email, subject, html, text)
