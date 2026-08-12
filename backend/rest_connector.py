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

from models import Document, RestConnectorCredential, Event
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

        count = 0
        for item in results:
            external_id = str(_get_path(item, connector.field_id) or hash(str(item)))
            if session.query(Document).filter_by(client_id=client.id, external_id=external_id).first():
                continue
            date_raw = _get_path(item, connector.field_date)
            session.add(Document(
                client_id=client.id,
                source=connector.display_name.lower(),
                external_id=external_id,
                source_url=_get_path(item, connector.field_url),
                title=_get_path(item, connector.field_title) or "(untitled)",
                content=_get_path(item, connector.field_content) or "",
                modified_at=_parse_dt(date_raw),
            ))
            count += 1
        session.commit()
        _log(session, client, connector.module_id, "success", f"Synced {count} new record(s) via {connector.display_name}")
        return {"synced": count, "demo": False}
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
    external_id = f"demo-{connector.module_id}-{client.id}-1"
    if not session.query(Document).filter_by(client_id=client.id, external_id=external_id).first():
        session.add(Document(
            client_id=client.id,
            source=f"{connector.display_name.lower()} (demo)",
            external_id=external_id,
            source_url=None,
            title=f"{client.name} - sample record via {connector.display_name}",
            content=f"This is a placeholder record showing what {connector.display_name} would pull in for {client.name}.",
            modified_at=datetime.datetime.utcnow(),
        ))
        session.commit()
    _log(session, client, connector.module_id, "success", f"Demo mode - generated 1 sample record ({reason}).")
    return {"synced": 1, "demo": True}
