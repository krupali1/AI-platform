"""
Google sign-in, via authlib's OpenID Connect support - handles PKCE,
state, and ID token verification correctly rather than hand-rolling
the OAuth exchange.

Requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (see README for how
to create these in Google Cloud Console) and, for a Workspace account
like themirailabs.com, setting the OAuth consent screen's user type to
"Internal" so only your org's accounts can sign in - no Google review
required for that mode, unlike a public "External" app.
"""
import os
from authlib.integrations.starlette_client import OAuth

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# Optional extra guard, independent of the "Internal" consent screen
# setting above - reject sign-ins outside this domain even if the
# OAuth app is misconfigured as "External". Leave unset to allow any
# Google account.
ALLOWED_EMAIL_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN")


def is_email_allowed(email: str) -> bool:
    if not ALLOWED_EMAIL_DOMAIN:
        return True
    return email.lower().endswith("@" + ALLOWED_EMAIL_DOMAIN.lower())


# Comma-separated list of emails that should always hold the admin role.
# Checked on every sign-in, not just first sign-in, so adding someone
# to this list promotes them the next time they log in - no manual DB
# edit needed to create the first admin.
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin_email(email: str) -> bool:
    return email.lower() in ADMIN_EMAILS
