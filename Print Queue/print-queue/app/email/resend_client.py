"""Send transactional emails via Resend (https://resend.com).

Resend's API is a single POST request, so we use httpx directly rather
than pulling in the official SDK. Keeps the dependency surface small.

If RESEND_API_KEY is empty (local dev), we log the email to the console
instead of sending — useful for testing the magic-link flow without a
real provider account.
"""

from __future__ import annotations

import httpx
import structlog

from app.config import Settings

log = structlog.get_logger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def _render_magic_link_html(
    site_name: str,
    verify_url: str,
    ttl_minutes: int,
) -> str:
    """Render the HTML magic-link email.

    Constraints baked into the markup:
    - Inline styles only (most corp/legacy mail clients strip <style>).
    - Table-based layout for Outlook compatibility.
    - System font stack so it renders consistently without webfont fetches.
    - Dark text on light background for the body — most users default to
      light-mode email, and dark-mode clients invert this on their own.
    - Single huge call-to-action button — both as a styled <a> and as a
      plain text fallback URL below it.
    """
    # Color tokens that match the app's primary blue.
    BG = "#f4f5f7"
    CARD = "#ffffff"
    TEXT = "#1a1d24"
    MUTED = "#6b7280"
    BORDER = "#e5e7eb"
    PRIMARY = "#4f8cff"
    PRIMARY_TEXT = "#ffffff"

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="x-apple-disable-message-reformatting">
  <title>Sign in to {site_name}</title>
</head>
<body style="margin:0;padding:0;background:{BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:{BG};padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:520px;background:{CARD};border:1px solid {BORDER};border-radius:10px;overflow:hidden;">
          <tr>
            <td style="padding:32px 32px 16px 32px;">
              <h1 style="margin:0 0 6px 0;font-size:20px;font-weight:600;color:{TEXT};line-height:1.3;">Sign in to {site_name}</h1>
              <p style="margin:0;color:{MUTED};font-size:14px;line-height:1.5;">
                Click the button below to finish signing in. This link works once and expires in {ttl_minutes} minutes.
              </p>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:8px 32px 24px 32px;">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td bgcolor="{PRIMARY}" style="background:{PRIMARY};border-radius:6px;">
                    <a href="{verify_url}" target="_blank" rel="noopener" style="display:inline-block;padding:14px 28px;color:{PRIMARY_TEXT};font-size:15px;font-weight:600;text-decoration:none;border-radius:6px;line-height:1;">
                      Sign in
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 32px 24px 32px;">
              <p style="margin:0;color:{MUTED};font-size:12px;line-height:1.5;">
                If the button doesn't work, paste this link in your browser:
              </p>
              <p style="margin:6px 0 0 0;font-size:12px;line-height:1.4;word-break:break-all;">
                <a href="{verify_url}" style="color:{PRIMARY};text-decoration:underline;">{verify_url}</a>
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 24px 32px;border-top:1px solid {BORDER};">
              <p style="margin:0;color:{MUTED};font-size:12px;line-height:1.5;">
                Didn't request this? You can safely ignore this email. Someone may have typed your address by mistake — no account changes were made.
              </p>
            </td>
          </tr>
        </table>
        <p style="margin:16px 0 0 0;color:{MUTED};font-size:11px;line-height:1.4;">
          {site_name} · This is an automated message; do not reply.
        </p>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _render_magic_link_text(
    site_name: str,
    verify_url: str,
    ttl_minutes: int,
) -> str:
    """Plain text fallback — what users see if HTML is stripped."""
    return (
        f"Sign in to {site_name}\n"
        f"\n"
        f"Click this link to finish signing in:\n"
        f"\n"
        f"{verify_url}\n"
        f"\n"
        f"The link works once and expires in {ttl_minutes} minutes.\n"
        f"\n"
        f"If you didn't request this, you can ignore this email — no account "
        f"changes were made.\n"
    )


async def send_magic_link_email(
    settings: Settings,
    to_email: str,
    verify_url: str,
) -> None:
    subject = f"Sign in to {settings.site_name}"
    text_body = _render_magic_link_text(
        settings.site_name, verify_url, settings.magic_link_ttl_minutes
    )
    html_body = _render_magic_link_html(
        settings.site_name, verify_url, settings.magic_link_ttl_minutes
    )

    if not settings.resend_api_key:
        log.warning(
            "email.resend.no_api_key.console_fallback",
            to=to_email,
            subject=subject,
            verify_url=verify_url,
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
                "text": text_body,
                "html": html_body,
            },
        )
    if resp.status_code >= 300:
        log.error(
            "email.resend.send_failed",
            status=resp.status_code,
            body=resp.text,
            to=to_email,
        )
        # Don't raise — we still tell the user "check your email" so we
        # don't leak whether a send succeeded. Errors go to Sentry via log.
        return

    log.info("email.resend.sent", to=to_email, message_id=resp.json().get("id"))
