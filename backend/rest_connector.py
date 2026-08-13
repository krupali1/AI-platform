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
import re
import datetime
import urllib.parse
import requests

from models import Document, Meeting, RestConnectorCredential, Event
from crypto import decrypt

# Used only by detect_fields()'s best-effort field-name guessing - order
# matters within each list (checked top to bottom), and a field already
# claimed by an earlier guess is never reused for a later one, so e.g.
# a single "summary" field doesn't get assigned to both title and
# content just because it matches both patterns.
_ID_PATTERNS = [r"^id$", r"_id$", r"^uuid$"]
_TITLE_PATTERNS = [r"^title$", r"^name$", r"^subject$", r"_title$", r"summary$"]
_CONTENT_PATTERNS = [r"content", r"description", r"body", r"text", r"summary"]
_URL_PATTERNS = [r"url$", r"link$", r"href$"]
_DATE_PATTERNS = [r"date", r"_at$", r"time"]


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


def find_results_array(body, prefix="", depth=0, max_depth=2):
    """Locates the list of records in an arbitrary API response for
    detect_fields() - checks the current level's keys for a list-of-dicts
    before recursing into nested objects, so a top-level "items" or
    "results" array is found before anything buried deeper, matching how
    most REST APIs actually shape a list response (Fathom's top-level
    "items", a hypothetical {"data": {"results": [...]}}, etc.)."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return prefix, body
    if not isinstance(body, dict) or depth > max_depth:
        return None, None
    for key, value in body.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return path, value
    for key, value in body.items():
        if isinstance(value, dict):
            path = f"{prefix}.{key}" if prefix else key
            found_path, found_list = find_results_array(value, path, depth + 1, max_depth)
            if found_path:
                return found_path, found_list
    return None, None


def _flatten_item(obj, prefix="", depth=0, max_depth=2):
    """Dotted-path scalar fields only, up to max_depth nested dicts -
    enough to catch a shape like Fathom's default_summary.markdown_formatted
    without wandering arbitrarily deep into unrelated nested objects."""
    out = {}
    if not isinstance(obj, dict) or depth > max_depth:
        return out
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[path] = value
        elif isinstance(value, dict):
            out.update(_flatten_item(value, path, depth + 1, max_depth))
    return out


def _match_first(patterns, keys_lower_to_orig, used):
    for pattern in patterns:
        for lower, orig in keys_lower_to_orig.items():
            if orig in used:
                continue
            if re.search(pattern, lower):
                return orig
    return None


def suggest_field_mapping(item):
    """Best-effort guess at which field is which, from the field NAMES
    only - there's no way to know a field means "title" without some
    signal, and names are the only one an arbitrary REST API gives us.
    Meant as a starting point to review and correct, not a guarantee -
    the caller also gets the raw sample item back alongside this, so a
    wrong guess is obvious and easy to fix by hand."""
    flat = _flatten_item(item)
    keys_lower = {k.lower(): k for k in flat}
    used = set()
    result = {}
    for field_name, patterns in (
        ("field_id", _ID_PATTERNS), ("field_title", _TITLE_PATTERNS),
        ("field_content", _CONTENT_PATTERNS), ("field_url", _URL_PATTERNS),
        ("field_date", _DATE_PATTERNS),
    ):
        match = _match_first(patterns, keys_lower, used)
        if match:
            used.add(match)
        result[field_name] = match or ""
    return result


def detect_fields(session, client, auth_style, auth_header_name, auth_value_prefix,
                   oauth_provider_id, search_url_template, connector_id=None, test_key=None):
    """Live test-fetch behind the "Test & detect fields" button - resolves
    auth the same way run_rest_connector does, but from raw parameters
    rather than a saved RestConnector row (works before a connector's
    first save, using a one-off key that's never persisted), and returns
    a best-guess field mapping from the real response instead of writing
    any records. Raises ValueError for anything the caller should show
    as-is (missing credential, bad auth_style); a request/HTTP failure
    propagates as whatever requests itself raises."""
    if auth_style == "header":
        value = test_key
        if not value and connector_id:
            cred = session.query(RestConnectorCredential).filter_by(connector_id=connector_id, client_id=client.id).first()
            value = decrypt(cred.encrypted_value) if cred else None
        if not value:
            raise ValueError("Paste an API key to test with, or save one in Team & Keys first")
        auth_header = {auth_header_name or "Authorization": f"{auth_value_prefix}{value}"}
    elif auth_style == "google_oauth":
        from connectors import _drive_oauth_credentials
        creds = _drive_oauth_credentials(session, client)
        if not creds:
            raise ValueError("Connect a Google account for this project first (Team & Keys)")
        auth_header = {"Authorization": f"Bearer {creds.token}"}
    elif auth_style == "oauth_provider":
        from models import OAuthProvider, OAuthConnection
        import oauth_generic
        if not oauth_provider_id:
            raise ValueError("Pick an OAuth provider first")
        provider = session.query(OAuthProvider).filter_by(id=oauth_provider_id).first()
        connection = session.query(OAuthConnection).filter_by(
            provider_id=oauth_provider_id, client_id=client.id
        ).first() if provider else None
        if not provider or not connection:
            raise ValueError(f"Connect {provider.name if provider else 'this provider'} for this project first (Team & Keys)")
        token = oauth_generic.get_valid_access_token(session, connection, provider)
        auth_header = {"Authorization": f"Bearer {token}"}
    else:
        raise ValueError("auth_style must be 'header', 'google_oauth', or 'oauth_provider'")

    url = _build_url(search_url_template, client.name, client.domain)
    resp = requests.get(url, headers=auth_header, timeout=15)
    resp.raise_for_status()
    body = resp.json()

    results_path, items = find_results_array(body)
    if not items:
        return {"results_path": results_path, "sample_item": None, "suggested_fields": None}

    sample = items[0]
    return {"results_path": results_path, "sample_item": sample, "suggested_fields": suggest_field_mapping(sample)}


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
