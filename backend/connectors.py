"""
Ingestion connectors, driven by per-project identity rather than
freeform keywords - a project's own name and its client email domain,
both set from the UI, not .env.

Matching rules, same shape for both sources:
1. The record's title/filename contains the project name.
2. Someone on the project's client domain is a participant (Fireflies)
   or has the file shared with them (Drive).

Neither Fireflies' nor Drive's API supports filtering server-side by
"anyone at domain X" - both only match exact email addresses in their
query language. Rule 2 is implemented by pulling a bounded, recent set
of candidates and checking each one's actual participants/permissions
in code. That's a real constraint, not a design choice: it means rule
2 is a scan over the most recent N meetings/files, not an exhaustive
guarantee across everything that was ever shared with that domain.

Demo mode: if a project has no Fireflies key / no Drive credentials
set, each connector generates a couple of realistic sample records
instead of failing, titled using the project's own name.
"""
import os
import datetime
import requests

from models import Meeting, Document, Event
from crypto import encrypt, decrypt

FIREFLIES_ENDPOINT = "https://api.fireflies.ai/graphql"

# How many "recent" candidates to scan for the domain-matching rule,
# since neither API can filter by domain directly - see module docstring.
# Fireflies' transcripts query caps `limit` at 50 - asking for more fails
# the whole call with a generic "Invalid argument(s) were provided", not
# a clamped result, so this can't just be turned up for wider scans.
DOMAIN_SCAN_LIMIT = 50


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def _normalize_domain(value):
    if not value:
        return None
    v = value.strip().lower()
    if v.startswith("@"):
        v = v[1:]
    v = v.replace("http://", "").replace("https://", "").rstrip("/")
    return v or None


def _parse_dt(value):
    """Handles both ISO strings (Drive) and epoch-millisecond numbers
    (Fireflies' `date` field)."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
            return datetime.datetime.utcfromtimestamp(float(value) / 1000)
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


# ---------- Fireflies ----------

FIREFLIES_QUERY = """
query GetTranscripts($title: String, $participants: [String!], $limit: Int) {
  transcripts(title: $title, participants: $participants, limit: $limit) {
    id
    title
    date
    transcript_url
    organizer_email
    participants
    summary { overview short_summary }
    sentences { speaker_name text }
  }
}
"""

# Fireflies has no query argument for "any participant at this domain" -
# only exact participant emails or an exact title match. This fetches
# recent meetings unfiltered, so the domain check below can run against
# their real participant lists.
FIREFLIES_RECENT_QUERY = """
query GetRecentTranscripts($limit: Int) {
  transcripts(limit: $limit) {
    id
    title
    date
    transcript_url
    organizer_email
    participants
    summary { overview short_summary }
    sentences { speaker_name text }
  }
}
"""


def run_fireflies(session, client):
    api_key = decrypt(client.encrypted_fireflies_key)
    domain = _normalize_domain(client.domain)
    team_emails = _split_list(client.team_emails)

    if not api_key:
        return _fireflies_demo(session, client, "no Fireflies API key set for this team")

    try:
        seen = {}

        def _run_query(query, variables):
            resp = requests.post(
                FIREFLIES_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "variables": variables},
                timeout=30,
            )
            try:
                body = resp.json()
            except ValueError:
                body = None
            if body and body.get("errors"):
                raise RuntimeError(body["errors"][0].get("message", "Fireflies API error"))
            resp.raise_for_status()
            return (body or {}).get("data", {}).get("transcripts", []) or []

        # Rule 1: title contains the project name - a real, efficient query.
        for t in _run_query(FIREFLIES_QUERY, {"title": client.name, "limit": 50}):
            seen[t["id"]] = t

        # Explicit team members as participants - a real, efficient query.
        if team_emails:
            for t in _run_query(FIREFLIES_QUERY, {"participants": team_emails, "limit": 50}):
                seen[t["id"]] = t

        # Rule 2: anyone at the client domain as a participant - no API
        # filter for this exists, so scan recent meetings and check
        # participant emails in code.
        domain_matched = 0
        if domain:
            for t in _run_query(FIREFLIES_RECENT_QUERY, {"limit": DOMAIN_SCAN_LIMIT}):
                participants = t.get("participants") or []
                if any(p.lower().endswith("@" + domain) for p in participants):
                    if t["id"] not in seen:
                        domain_matched += 1
                    seen[t["id"]] = t

        count = 0
        for t in seen.values():
            if session.query(Meeting).filter_by(client_id=client.id, external_id=t["id"]).first():
                continue
            summary = t.get("summary") or {}
            transcript_text = "\n".join(
                f"{s.get('speaker_name', '')}: {s.get('text', '')}" for s in (t.get("sentences") or [])
            )
            session.add(Meeting(
                client_id=client.id,
                source="fireflies",
                external_id=t["id"],
                source_url=t.get("transcript_url"),
                title=t.get("title"),
                occurred_at=_parse_dt(t.get("date")),
                summary=summary.get("overview") or summary.get("short_summary") or "",
                transcript=transcript_text,
                participants=", ".join(t.get("participants") or []),
            ))
            count += 1
        session.commit()

        criteria = [f"title contains \"{client.name}\""]
        if team_emails:
            criteria.append(f"{len(team_emails)} team member(s) as participant")
        if domain:
            criteria.append(f"@{domain} participant (scanned last {DOMAIN_SCAN_LIMIT} meetings, {domain_matched} matched)")
        else:
            criteria.append("no client domain set - domain matching skipped")
        _log(session, client, "fireflies-connector", "success",
             f"Synced {count} new meeting(s) - {'; '.join(criteria)}")
        return {"synced": count, "demo": False}
    except Exception as e:
        _log(session, client, "fireflies-connector", "error", f"Fireflies sync failed: {e}")
        raise


def _fireflies_demo(session, client, reason):
    count = 0
    for s in _demo_meetings(client):
        if session.query(Meeting).filter_by(client_id=client.id, external_id=s["external_id"]).first():
            continue
        session.add(Meeting(client_id=client.id, source="fireflies (demo)", **s))
        count += 1
    session.commit()
    _log(session, client, "fireflies-connector", "success",
         f"Demo mode - generated {count} sample meeting(s) ({reason}).")
    return {"synced": count, "demo": True}


def _split_list(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _demo_meetings(client):
    now = datetime.datetime.utcnow()
    label = client.name
    return [
        {
            "external_id": f"demo-ff-{client.id}-1",
            "source_url": "https://app.fireflies.ai/view/demo-1",
            "title": f"{label} - kickoff call",
            "occurred_at": now - datetime.timedelta(days=18),
            "summary": f"Aligned on scope and initial requirements for {label}.",
            "transcript": f"Speaker 1: Let's confirm scope for {label}.\nSpeaker 2: Agreed, we'll follow up with a written summary.",
            "participants": "consultant@themirailabs.com, client-contact@example.com",
        },
        {
            "external_id": f"demo-ff-{client.id}-2",
            "source_url": "https://app.fireflies.ai/view/demo-2",
            "title": f"{label} - follow-up review",
            "occurred_at": now - datetime.timedelta(days=9),
            "summary": f"Reviewed open items for {label} and confirmed next steps.",
            "transcript": f"Speaker 1: Where are we on the {label} integration?\nSpeaker 2: On track, confirming timeline by next week.",
            "participants": "consultant@themirailabs.com, client-contact@example.com",
        },
    ]


# ---------- Google Drive ----------

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")


def _drive_oauth_credentials(session, client):
    """Builds credentials from the project's connected Google account,
    refreshing the access token if it's expired and saving the new one
    back - so a run months from now still works without anyone having
    to reconnect, as long as the refresh token is still valid."""
    refresh_token = decrypt(client.encrypted_drive_refresh_token)
    if not refresh_token:
        return None

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    access_token = decrypt(client.encrypted_drive_access_token)
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    expired = not client.drive_token_expiry or client.drive_token_expiry <= datetime.datetime.utcnow()
    if expired or not access_token:
        creds.refresh(Request())
        client.encrypted_drive_access_token = encrypt(creds.token)
        if creds.expiry:
            client.drive_token_expiry = creds.expiry
        session.commit()
    return creds


def _drive_service_account_credentials(client):
    raw = decrypt(client.encrypted_drive_credentials)
    if not raw:
        return None
    import json
    from google.oauth2 import service_account
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )


def run_drive(session, client):
    domain = _normalize_domain(client.domain)
    creds = None
    cred_source = None
    try:
        creds = _drive_oauth_credentials(session, client)
        if creds:
            cred_source = f"connected account ({client.drive_oauth_email})"
        else:
            creds = _drive_service_account_credentials(client)
            if creds:
                cred_source = "service account"
    except Exception as e:
        _log(session, client, "drive-connector", "error", f"Drive credentials invalid or expired: {e}")
        raise

    if not creds:
        count = 0
        for s in _demo_documents(client):
            if session.query(Document).filter_by(client_id=client.id, external_id=s["external_id"]).first():
                continue
            session.add(Document(client_id=client.id, source="google_drive (demo)", **s))
            count += 1
        session.commit()
        _log(session, client, "drive-connector", "success",
             f"Demo mode - generated {count} sample document(s) (no Drive account connected for this team).")
        return {"synced": count, "demo": True}

    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds)
        folder_clause = f"'{client.drive_folder_id}' in parents and " if client.drive_folder_id else ""
        candidates = {}

        # Rule 1: filename contains the project name - a real, efficient query.
        escaped_name = client.name.replace("'", "\\'")
        name_query = f"{folder_clause}name contains '{escaped_name}' and trashed = false"
        name_results = service.files().list(
            q=name_query,
            fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            pageSize=50,
        ).execute()
        for f in name_results.get("files", []):
            candidates[f["id"]] = f

        # Rule 2: shared with anyone at the client domain - no query filter
        # for this exists, so scan recent files with permissions included
        # and check in code. Bounded by DOMAIN_SCAN_LIMIT, not exhaustive.
        domain_matched = 0
        if domain:
            scan_query = f"{folder_clause}trashed = false" if folder_clause else "trashed = false"
            scan_results = service.files().list(
                q=scan_query,
                orderBy="modifiedTime desc",
                fields="files(id, name, mimeType, modifiedTime, webViewLink, permissions(emailAddress, domain))",
                pageSize=DOMAIN_SCAN_LIMIT,
            ).execute()
            for f in scan_results.get("files", []):
                perms = f.get("permissions") or []
                matched = any(
                    (p.get("emailAddress", "").lower().endswith("@" + domain)) or (p.get("domain", "").lower() == domain)
                    for p in perms
                )
                if matched:
                    if f["id"] not in candidates:
                        domain_matched += 1
                    candidates[f["id"]] = f

        count = 0
        for f in candidates.values():
            if session.query(Document).filter_by(client_id=client.id, external_id=f["id"]).first():
                continue
            content = _read_drive_content(service, f)
            session.add(Document(
                client_id=client.id,
                source="google_drive",
                external_id=f["id"],
                source_url=f.get("webViewLink"),
                title=f["name"],
                content=content,
                modified_at=_parse_dt(f.get("modifiedTime")),
            ))
            count += 1
        session.commit()

        criteria = [f"name contains \"{client.name}\""]
        if domain:
            criteria.append(f"shared with @{domain} (scanned last {DOMAIN_SCAN_LIMIT} files, {domain_matched} matched)")
        else:
            criteria.append("no client domain set - domain matching skipped")
        _log(session, client, "drive-connector", "success",
             f"Synced {count} new document(s) via {cred_source} - {'; '.join(criteria)}")
        return {"synced": count, "demo": False}
    except Exception as e:
        _log(session, client, "drive-connector", "error", f"Drive sync failed: {e}")
        raise


def _read_drive_content(service, f):
    try:
        if f["mimeType"] == "application/vnd.google-apps.document":
            data = service.files().export(fileId=f["id"], mimeType="text/plain").execute()
            return data.decode("utf-8") if isinstance(data, bytes) else str(data)
        return f"[binary file: {f['mimeType']}]"
    except Exception:
        return ""


def _demo_documents(client):
    now = datetime.datetime.utcnow()
    label = client.name
    return [
        {
            "external_id": f"demo-drive-{client.id}-1",
            "source_url": "https://drive.google.com/file/d/demo-1/view",
            "title": f"{label} - requirement notes",
            "content": f"Summary of requirements discussed for {label}, covering scope and initial timeline.",
            "modified_at": now - datetime.timedelta(days=17),
        },
        {
            "external_id": f"demo-drive-{client.id}-2",
            "source_url": "https://drive.google.com/file/d/demo-2/view",
            "title": f"{label} - project brief",
            "content": f"Background and objectives for the {label} engagement, plus key stakeholders.",
            "modified_at": now - datetime.timedelta(days=11),
        },
    ]
