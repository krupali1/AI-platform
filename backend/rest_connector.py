"""
The generic runner behind every config-driven REST connector - Tier A
of "make this a platform where agents can be made" applied to
connectors rather than engines. There is no per-connector Python file:
a RestConnector row defines a URL template, how to authenticate, and
which JSON fields map to a title/content/url/date, and this module
does the actual fetch-and-store for all of them.

Two authentication styles, deliberately kept separate rather than
generalized into one, because they're genuinely different problems:

- "header": a static credential (a Slack bot token, most API keys) -
  entered per project via RestConnectorCredential, sent as one header.
- "google_oauth": no separate credential at all - reuses the project's
  already-connected Google account (the same token Drive and Gmail
  use). This is what makes something like Google Calendar addable as
  pure configuration: the OAuth plumbing already exists, only the
  scope needs adding to the connect flow, and the connector itself is
  just a URL template and a field mapping, same as any other.

What this does NOT generalize, on purpose: OAuth2 for services other
than Google (that's the bigger "Tier B" project - a generic OAuth
provider table - not this), and GraphQL-shaped APIs like Fireflies,
which don't fit a REST field-mapping model at all.
"""
import os
import datetime
import urllib.parse
import requests

from models import Document, Meeting, RestConnectorCredential, Event
from crypto import decrypt


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def _get_path(obj, path):
    """Dotted-path getter - "start.dateTime" pulls obj["start"]["dateTime"].
    Returns None at any missing step rather than raising, since a
    field mapping author will often get a path slightly wrong and this
    should degrade to an empty field, not crash the whole run."""
    if not path:
        return None
    current = obj
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _build_url(template, project_name, domain):
    query = urllib.parse.quote(project_name or "")
    domain_q = urllib.parse.quote(domain or "")
    return template.replace("{query}", query).replace("{domain}", domain_q)


def run_rest_connector(session, client, connector):
    if connector.auth_style == "google_oauth":
        from connectors import _drive_oauth_credentials
        try:
            creds = _drive_oauth_credentials(session, client)
        except Exception as e:
            _log(session, client, connector.module_id, "error", f"Google credentials invalid or expired: {e}")
            raise
        if not creds:
            return _demo(session, client, connector, "no Google account connected for this team")
        auth_header = {"Authorization": f"Bearer {creds.token}"}
    elif connector.auth_style == "oauth_provider":
        from models import OAuthProvider, OAuthConnection
        import oauth_generic
        provider = session.query(OAuthProvider).filter_by(id=connector.oauth_provider_id).first()
        connection = session.query(OAuthConnection).filter_by(
            provider_id=connector.oauth_provider_id, client_id=client.id
        ).first() if provider else None
        if not provider or not connection:
            return _demo(session, client, connector, f"not connected to {provider.name if provider else 'its OAuth provider'} for this project")
        try:
            token = oauth_generic.get_valid_access_token(session, connection, provider)
        except Exception as e:
            _log(session, client, connector.module_id, "error", f"{provider.name} credentials invalid or expired: {e}")
            raise
        auth_header = {"Authorization": f"Bearer {token}"}
    else:
        cred = session.query(RestConnectorCredential).filter_by(
            connector_id=connector.id, client_id=client.id
        ).first()
        value = decrypt(cred.encrypted_value) if cred else None
        if not value:
            return _demo(session, client, connector, "no credential set for this connector on this project")
        auth_header = {connector.auth_header_name: f"{connector.auth_value_prefix}{value}"}

    try:
        url = _build_url(connector.search_url_template, client.name, client.domain)
        resp = requests.get(url, headers=auth_header, timeout=20)
        resp.raise_for_status()
        body = resp.json()
        results = _get_path(body, connector.results_path)
        if not isinstance(results, list):
            raise RuntimeError(f"results_path '{connector.results_path}' did not point at a list in the response")

        # "meeting" content_type routes into the Meeting table (shows up
        # under Repository's Meetings tab, reads via transcript-or-summary
        # same as Fireflies) instead of Document - lets a connector like
        # Fathom, which pulls meeting recaps rather than files, land in
        # the tab that actually matches what it is.
        is_meeting = connector.content_type == "meeting"
        model = Meeting if is_meeting else Document
        content_field = "summary" if is_meeting else "content"
        date_field = "occurred_at" if is_meeting else "modified_at"

        count = 0
        backfilled = 0
        for item in results:
            external_id = str(_get_path(item, connector.field_id) or hash(str(item)))
            content = _get_path(item, connector.field_content) or ""

            existing = session.query(model).filter_by(client_id=client.id, external_id=external_id).first()
            if existing:
                # Same fix as the Fireflies/Drive connectors: a record
                # synced before the source finished generating its content
                # (Fathom's summary is async, same as Fireflies'
                # transcription) got permanently stuck empty, since the
                # dedup check above skipped it on every later sync even
                # once the content became available. This applies that
                # backfill generically, to every config-driven connector
                # at once, rather than each one needing its own copy of
                # this fix. Deliberately narrow: only touches a record
                # that's still empty, never overwrites one that already
                # has real content - a field genuinely, permanently empty
                # by design (e.g. a calendar event with no description)
                # just gets harmlessly re-checked next sync, not rewritten.
                if not getattr(existing, content_field) and content:
                    setattr(existing, content_field, content)
                    existing.synthesized_summary = None
                    backfilled += 1
                continue

            date_raw = _get_path(item, connector.field_date)
            session.add(model(
                client_id=client.id,
                source=connector.display_name.lower(),
                external_id=external_id,
                source_url=_get_path(item, connector.field_url),
                title=_get_path(item, connector.field_title) or "(untitled)",
                **{content_field: content, date_field: _parse_dt(date_raw)},
            ))
            count += 1
        session.commit()

        backfill_note = f", backfilled content for {backfilled} previously-empty record(s)" if backfilled else ""
        _log(session, client, connector.module_id, "success",
             f"Synced {count} new record(s){backfill_note} via {connector.display_name}")
        # Counted together so a backfill-only run (nothing new, but an old
        # record's content just became available) still triggers
        # extraction/brief regeneration downstream, same as Drive's and
        # Fireflies' equivalent backfill counts already do.
        return {"synced": count + backfilled, "demo": False}
    except Exception as e:
        _log(session, client, connector.module_id, "error", f"{connector.display_name} sync failed: {e}")
        raise


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


def _demo(session, client, connector, reason):
    is_meeting = connector.content_type == "meeting"
    model = Meeting if is_meeting else Document
    content_field = "summary" if is_meeting else "content"
    date_field = "occurred_at" if is_meeting else "modified_at"
    external_id = f"demo-{connector.module_id}-{client.id}-1"
    if not session.query(model).filter_by(client_id=client.id, external_id=external_id).first():
        session.add(model(
            client_id=client.id,
            source=f"{connector.display_name.lower()} (demo)",
            external_id=external_id,
            source_url=None,
            title=f"{client.name} - sample record via {connector.display_name}",
            **{
                content_field: f"This is a placeholder record showing what {connector.display_name} would pull in for {client.name}.",
                date_field: datetime.datetime.utcnow(),
            },
        ))
        session.commit()
    _log(session, client, connector.module_id, "success", f"Demo mode - generated 1 sample record ({reason}).")
    return {"synced": 1, "demo": True}
