"""
The generic runner behind every config-driven ActionConnector - the
outbound sibling of rest_connector.py. Instead of pulling records IN
and parsing them into Document/Meeting rows, this POSTs (or PUTs) a
PromptEngine's generated text output OUT to a third-party API. The
response is only ever logged, never stored as a record.

Duplicates rest_connector.py's three-way auth branch ("header" /
"google_oauth" / "oauth_provider") rather than importing a shared
helper - that file already duplicates the same branch between
detect_fields() and run_rest_connector() rather than factoring it out;
this follows that same established convention.
"""
import json
import datetime
import urllib.parse
import requests

from models import ActionConnectorCredential, Event
from crypto import decrypt

_PLACEHOLDER_KEYS = ("content", "project_name", "date", "engine_name")


def _log(session, client, module_id, status, message):
    session.add(Event(client_id=client.id, module_id=module_id, status=status, message=message))
    session.commit()


def _build_url(template, project_name, domain):
    query = urllib.parse.quote(project_name or "")
    domain_q = urllib.parse.quote(domain or "")
    return template.replace("{query}", query).replace("{domain}", domain_q)


def render_body(body_template, values):
    """Substitutes {content}/{project_name}/{date}/{engine_name} into a
    JSON-shaped template string. Each value is JSON-string-escaped
    (json.dumps(value)[1:-1]) before substitution rather than inserted
    raw, so generated content containing a quote or newline still
    produces valid JSON. Raises ValueError if the result doesn't parse
    as JSON - a broken template is caught before ever being sent."""
    rendered = body_template
    for key in _PLACEHOLDER_KEYS:
        token = "{" + key + "}"
        if token in rendered:
            escaped = json.dumps(values.get(key) or "")[1:-1]
            rendered = rendered.replace(token, escaped)
    try:
        parsed = json.loads(rendered)
    except Exception as e:
        raise ValueError(f"body_template did not produce valid JSON: {e}")
    return parsed


def _resolve_auth(session, client, connector):
    """Returns (auth_header, demo_reason). demo_reason set means "no
    real credential/connection" - caller falls back to demo mode, same
    convention as rest_connector.py's _demo(...) callers."""
    if connector.auth_style == "google_oauth":
        from connectors import _drive_oauth_credentials
        creds = _drive_oauth_credentials(session, client)
        if not creds:
            return None, "no Google account connected for this team"
        return {"Authorization": f"Bearer {creds.token}"}, None
    elif connector.auth_style == "oauth_provider":
        from models import OAuthProvider, OAuthConnection
        import oauth_generic
        provider = session.query(OAuthProvider).filter_by(id=connector.oauth_provider_id).first()
        connection = session.query(OAuthConnection).filter_by(
            provider_id=connector.oauth_provider_id, client_id=client.id
        ).first() if provider else None
        if not provider or not connection:
            return None, f"not connected to {provider.name if provider else 'its OAuth provider'} for this project"
        token = oauth_generic.get_valid_access_token(session, connection, provider)
        return {"Authorization": f"Bearer {token}"}, None
    else:
        cred = session.query(ActionConnectorCredential).filter_by(
            connector_id=connector.id, client_id=client.id
        ).first()
        value = decrypt(cred.encrypted_value) if cred else None
        if not value:
            return None, "no credential set for this connector on this project"
        return {connector.auth_header_name: f"{connector.auth_value_prefix}{value}"}, None


def send_action(session, client, connector, content, engine_name=None):
    """Sends one agent's generated output through this connector.
    Returns {"sent": bool, "demo": bool}. Only raises for a real send
    that actually fails - a missing credential falls back to demo mode
    (logged, not raised), same distinction run_rest_connector makes."""
    auth_header, demo_reason = _resolve_auth(session, client, connector)
    who = f"{engine_name}'s" if engine_name else "an agent's"
    if demo_reason:
        _log(session, client, connector.module_id, "success",
             f"Demo mode - would have sent {who} output to {connector.display_name} ({demo_reason}).")
        return {"sent": False, "demo": True}

    try:
        body = render_body(connector.body_template, {
            "content": content, "project_name": client.name,
            "date": datetime.datetime.utcnow().date().isoformat(), "engine_name": engine_name or "",
        })
        url = _build_url(connector.url_template, client.name, client.domain)
        method = (connector.http_method or "POST").upper()
        resp = requests.request(method, url, headers={**auth_header, "Content-Type": "application/json"}, json=body, timeout=20)
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            # Same reasoning as rest_connector.py's equivalent re-raise -
            # the default HTTPError message drops whatever explanation
            # the API actually put in the response body.
            detail = (resp.text or "")[:300]
            raise RuntimeError(f"{resp.status_code} error from {connector.display_name}: {detail}") from None
    except Exception as e:
        _log(session, client, connector.module_id, "error", f"{connector.display_name} send failed: {e}")
        raise

    _log(session, client, connector.module_id, "success", f"Sent {who} output to {connector.display_name}")
    return {"sent": True, "demo": False}


def test_send(session, client, auth_style, auth_header_name, auth_value_prefix, oauth_provider_id,
              url_template, http_method, body_template, connector_id=None, test_key=None,
              sample_content="This is a sample agent output, sent to verify this action connector is configured correctly."):
    """Behind the "Test send" button - resolves auth the same way
    detect_fields() does (raw params, works pre-save, falls back to a
    saved credential when connector_id is given and no test_key), then
    ACTUALLY sends a clearly-labeled sample payload to the real target.
    Unlike detect_fields's plain GET, there's no side-effect-free way to
    verify an outbound webhook/API integration short of really calling
    it - the caller-side UI must warn the user this is a real send
    before calling this. Raises ValueError for setup issues (missing
    key, bad auth_style); a request/HTTP failure propagates as whatever
    requests itself raises."""
    if auth_style == "header":
        value = test_key
        if not value and connector_id:
            cred = session.query(ActionConnectorCredential).filter_by(connector_id=connector_id, client_id=client.id).first()
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
        connection = session.query(OAuthConnection).filter_by(provider_id=oauth_provider_id, client_id=client.id).first() if provider else None
        if not provider or not connection:
            raise ValueError(f"Connect {provider.name if provider else 'this provider'} for this project first (Team & Keys)")
        token = oauth_generic.get_valid_access_token(session, connection, provider)
        auth_header = {"Authorization": f"Bearer {token}"}
    else:
        raise ValueError("auth_style must be 'header', 'google_oauth', or 'oauth_provider'")

    body = render_body(body_template, {
        "content": sample_content, "project_name": client.name,
        "date": datetime.datetime.utcnow().date().isoformat(), "engine_name": "(test)",
    })
    url = _build_url(url_template, client.name, client.domain)
    resp = requests.request((http_method or "POST").upper(), url, headers={**auth_header, "Content-Type": "application/json"}, json=body, timeout=15)
    return {"status_code": resp.status_code, "response_body": (resp.text or "")[:1000], "sent_body": body}
