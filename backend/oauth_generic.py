"""
Tier B: a generic OAuth2 provider - Slack, or any other OAuth2 SaaS,
added as an OAuthProvider config row (authorize URL, token URL, client
ID/secret, scopes) instead of a new Python route per provider. One
connect route, one callback route, one refresh function, all driven by
whichever provider's row is in play.

This deliberately does NOT touch how Drive/Gmail/Calendar auth works -
that's a separate, already-working path (main.py's /drive/connect
routes, using authlib's registered Google client). This module is for
providers added after this system exists, where standard
authorization-code OAuth2 (POST code + client_id + client_secret +
redirect_uri, get back an access/refresh token) covers the flow -
which is most OAuth2 SaaS APIs, including Slack's.

What this doesn't generalize: PKCE-only flows, providers with a
non-standard token exchange shape, or anything needing more than a
bearer token in an Authorization header to call afterward.
"""
import datetime
import urllib.parse
import requests

from crypto import encrypt, decrypt


def build_authorize_url(provider, redirect_uri, state):
    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.scopes or "",
        "response_type": "code",
        "state": state,
    }
    return f"{provider.authorize_url}?{urllib.parse.urlencode(params)}"


def exchange_code(provider, code, redirect_uri):
    """Returns (access_token, refresh_token_or_None, expires_in_seconds_or_None).
    Standard authorization_code grant, form-encoded - what Slack and
    most OAuth2 APIs expect."""
    client_secret = decrypt(provider.encrypted_client_secret)
    resp = requests.post(
        provider.token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": provider.client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    # Slack returns ok:false with a 200 status on failure, rather than
    # a non-2xx code - worth checking explicitly rather than trusting
    # raise_for_status() alone to catch every provider's failure shape.
    if body.get("ok") is False:
        raise RuntimeError(body.get("error", "OAuth token exchange failed"))
    access_token = body.get("access_token") or (body.get("authed_user") or {}).get("access_token")
    if not access_token:
        raise RuntimeError("No access_token in the provider's response")
    return access_token, body.get("refresh_token"), body.get("expires_in")


def get_valid_access_token(session, connection, provider):
    """Refreshes if expired and there's a refresh token, saving the new
    token back so the next call doesn't need to refresh again. Returns
    the (possibly refreshed) plain access token."""
    access_token = decrypt(connection.encrypted_access_token)
    expired = connection.token_expiry and connection.token_expiry <= datetime.datetime.utcnow()
    refresh_token = decrypt(connection.encrypted_refresh_token) if connection.encrypted_refresh_token else None

    if expired and refresh_token:
        client_secret = decrypt(provider.encrypted_client_secret)
        resp = requests.post(
            provider.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": provider.client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("ok") is False:
            raise RuntimeError(body.get("error", "OAuth token refresh failed"))
        access_token = body.get("access_token")
        connection.encrypted_access_token = encrypt(access_token)
        if body.get("expires_in"):
            connection.token_expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=body["expires_in"])
        if body.get("refresh_token"):
            connection.encrypted_refresh_token = encrypt(body["refresh_token"])
        session.commit()

    return access_token
