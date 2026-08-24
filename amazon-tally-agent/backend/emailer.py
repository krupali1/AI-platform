"""
Outbound email for the "Email the sheet" action - Resend's HTTP API,
the same provider the Client Memory Console's own digest notifier
uses (see that app's README), not a new integration invented for this
one. RESEND_API_KEY and RESEND_FROM_EMAIL come from the environment;
missing either one fails clearly and catchably (EmailNotConfigured)
rather than silently doing nothing, so the caller can surface a real
message instead of a mysteriously dead button.
"""
import os
import base64
import httpx

RESEND_API_URL = "https://api.resend.com/emails"


class EmailNotConfigured(Exception):
    pass


def send_generation_email(to, subject, note, attachment_bytes, attachment_filename, cc=""):
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("RESEND_FROM_EMAIL")
    if not api_key or not from_email:
        raise EmailNotConfigured(
            "Email isn't configured yet - set RESEND_API_KEY and RESEND_FROM_EMAIL in .env to enable sending."
        )
    if not to.strip():
        raise ValueError("Recipient email is required")

    payload = {
        "from": from_email,
        "to": [to.strip()],
        "subject": subject,
        "text": note or "",
        "attachments": [{
            "filename": attachment_filename,
            "content": base64.b64encode(attachment_bytes).decode("ascii"),
        }],
    }
    if cc.strip():
        payload["cc"] = [c.strip() for c in cc.split(",") if c.strip()]

    resp = httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()
