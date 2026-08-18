"""
Email, in both directions:

- run_gmail: ingestion. Reuses the exact same Google OAuth connection
  already built for Drive - "Connect Google Account" now requests
  gmail.readonly alongside drive.readonly in one consent, so there's
  no second connect flow. Gmail's search syntax genuinely supports
  domain matching natively (from:@domain.com, to:@domain.com) unlike
  Drive or Fireflies, so this is a single efficient query rather than
  the bounded scan-and-filter those needed.

- create_meeting_draft: per-meeting, on demand (not a scheduled
  module). Writes the meeting's minutes into a new Gmail draft in the
  connected account's own Drafts folder via gmail.compose - review and
  send happen in Gmail itself, so there's no approval step to build
  here. Deliberately draft-only, not gmail.send: creating a draft an
  admin still has to open and click Send is a meaningfully smaller
  grant than being able to send mail outright.

- run_digest: notification. A separate, much simpler piece - a Resend
  API key per project, and an email address to send to. Reads recent
  events and the latest brief (if one exists) and sends a plain digest.
  Deliberately not reusing Gmail send scope for this either: sending
  automated mail from a person's own inbox is a different, more
  sensitive thing than reading it, and a dedicated transactional email
  provider is the more normal shape for this regardless.

Connections made before gmail.readonly or gmail.compose existed only
granted whatever scopes came before them - calling either API with an
old token fails with a clear permission error caught below, not a
crash.
"""
import os
import base64
import datetime
import requests

from models import Document, Event
from crypto import decrypt

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def _normalize_domain(value):
    if not value:
        return None
    v = value.strip().lower()
    if v.startswith("@"):
        v = v[1:]
    return v.replace("http://", "").replace("https://", "").rstrip("/") or None


def _parse_dt(value):
    """Always naive UTC - fromisoformat would otherwise return a
    timezone-aware datetime for a "Z"-suffixed value, but a value read
    back from the database is always naive (SQLite/SQLAlchemy's plain
    DateTime column drops tzinfo on the round-trip), so comparing the two
    later raises "can't compare offset-naive and offset-aware datetimes" -
    stripped here so every caller gets a consistently comparable value."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


# ---------- Gmail ingestion ----------

def run_gmail(session, client):
    from connectors import _drive_oauth_credentials  # same connected account, same token

    domain = _normalize_domain(client.domain)
    project_name = client.name

    try:
        creds = _drive_oauth_credentials(session, client)
    except Exception as e:
        _log(session, client, "gmail-connector", "error", f"Google credentials invalid or expired: {e}")
        raise

    if not creds:
        count = 0
        for s in _demo_emails(client):
            if session.query(Document).filter_by(client_id=client.id, external_id=s["external_id"]).first():
                continue
            session.add(Document(client_id=client.id, source="gmail (demo)", **s))
            count += 1
        session.commit()
        _log(session, client, "gmail-connector", "success",
             f"Demo mode - generated {count} sample email(s) (no Google account connected for this team).")
        return {"synced": count, "demo": True}

    try:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds)

        # Gmail's search syntax gives special meaning to ", (, and ) - a
        # project name containing any of those (e.g. "SBAMI (Sri Balaji
        # Trust)") would otherwise break out of the quoted phrase or open
        # an unbalanced group and get the whole query rejected as invalid.
        safe_name = project_name.replace('"', "").replace("(", "").replace(")", "")
        clauses = [f'subject:"{safe_name}"']
        if domain:
            clauses.append(f"from:@{domain}")
            clauses.append(f"to:@{domain}")
        query = " OR ".join(clauses)

        results = service.users().messages().list(userId="me", q=query, maxResults=30).execute()
        message_refs = results.get("messages", [])

        count = 0
        for ref in message_refs:
            if session.query(Document).filter_by(client_id=client.id, external_id=ref["id"]).first():
                continue
            msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            subject, sender, date_str = _headers(msg)
            body = _extract_body(msg.get("payload", {}))
            session.add(Document(
                client_id=client.id,
                source="gmail",
                external_id=ref["id"],
                source_url=f"https://mail.google.com/mail/u/0/#all/{ref['id']}",
                title=subject or "(no subject)",
                content=f"From: {sender}\n\n{body}",
                modified_at=_parse_dt(date_str) or datetime.datetime.utcnow(),
            ))
            count += 1
        session.commit()

        criteria = [f'subject contains "{project_name}"']
        if domain:
            criteria.append(f"to/from @{domain}")
        else:
            criteria.append("no client domain set - domain matching skipped")
        _log(session, client, "gmail-connector", "success",
             f"Synced {count} new email(s) - {'; '.join(criteria)}")
        return {"synced": count, "demo": False}
    except Exception as e:
        _log(session, client, "gmail-connector", "error", f"Gmail sync failed: {e}")
        raise


def _headers(msg):
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers.get("subject"), headers.get("from"), headers.get("date")


def _extract_body(payload):
    """Gmail bodies are base64url-encoded, and can be nested inside
    multipart/alternative parts - this walks the parts looking for the
    first plain-text one, falling back to the message snippet."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    return ""


def _decode(data):
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _demo_emails(client):
    now = datetime.datetime.utcnow()
    label = client.name
    return [
        {
            "external_id": f"demo-gmail-{client.id}-1",
            "source_url": "https://mail.google.com/mail/u/0/#all/demo-1",
            "title": f"Re: {label} - next steps",
            "content": f"From: client-contact@example.com\n\nFollowing up on our call - confirming we're aligned on scope for {label} and will send the signed SOW by Friday.",
            "modified_at": now - datetime.timedelta(days=4),
        },
    ]


# ---------- Meeting minutes drafting ----------

def create_meeting_draft(session, client, meeting):
    """Creates a Gmail draft with this meeting's minutes, sitting in
    the connected Google account's own Drafts folder - reviewing and
    sending it happens in Gmail itself, not in this app, so there's no
    separate approval UI to build here. Uses gmail.compose, the
    narrowest scope that can create a draft (it cannot read or send
    existing mail on its own - that's still gmail.readonly's job).
    Raises on any failure (no connection, missing scope, Gmail API
    error) - the caller turns that into a clean error response."""
    from connectors import _drive_oauth_credentials
    from email.mime.text import MIMEText

    creds = _drive_oauth_credentials(session, client)
    if not creds:
        raise RuntimeError("Connect a Google account for this project first (Team & Keys)")

    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=creds)

    body_text = meeting.synthesized_summary or meeting.summary or "(no summary available yet)"
    if meeting.source_url:
        body_text += f"\n\nOriginal recording/notes: {meeting.source_url}"

    mime_msg = MIMEText(body_text)
    mime_msg["subject"] = f"Minutes of Meeting: {meeting.title or 'Untitled meeting'}"
    if meeting.participants:
        # Reconnecting after gmail.compose was added re-consents to
        # this same scope, same as when gmail.readonly was added - an
        # old token without it fails here with Gmail's own permission
        # error, not a crash.
        mime_msg["to"] = meeting.participants

    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
    draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return draft.get("id")


# ---------- Digest notification ----------

def run_digest(session, client, api_key_override=None):
    from models import Brief

    resend_key = decrypt(client.encrypted_resend_key)
    if not resend_key or not client.notify_email:
        _log(session, client, "digest-notifier", "warning",
             "Resend key or notify email not set for this project - nothing sent.")
        return {"sent": False}

    events = session.query(Event).filter_by(client_id=client.id).order_by(Event.created_at.desc()).limit(15).all()
    latest_brief = session.query(Brief).filter_by(client_id=client.id).order_by(Brief.created_at.desc()).first()

    html = f"<h2>{client.name} - digest</h2>"
    if latest_brief:
        html += f"<h3>Latest status brief</h3><p>{latest_brief.content.replace(chr(10), '<br>')}</p>"
    html += "<h3>Recent activity</h3><ul>"
    for e in events:
        html += f"<li><b>{e.module_id}</b> ({e.status}): {e.message}</li>"
    html += "</ul>"

    try:
        resp = requests.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={
                "from": "Client Memory Console <onboarding@resend.dev>",
                "to": [client.notify_email],
                "subject": f"{client.name} - digest",
                "html": html,
            },
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        _log(session, client, "digest-notifier", "error", f"Digest send failed: {e}")
        raise

    _log(session, client, "digest-notifier", "success", f"Sent digest to {client.notify_email}")
    return {"sent": True}
