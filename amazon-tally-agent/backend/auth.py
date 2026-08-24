"""
Shared-login auth - one or a few username/password pairs configured
via env vars, no OAuth, no per-user roles. Enough to keep this off the
open internet for a single company's own team; not a general-purpose
user system.

Configure either:
  AUTH_USERS="alice:s3cret,bob:hunter2"     (multiple named users)
or, simpler, a single pair:
  APP_USERNAME="team"
  APP_PASSWORD="s3cret"
If neither is set, login is disabled (open access) - useful for local
development only; main.py logs a warning in that case.
"""
import os
from fastapi import Request, HTTPException, Depends


def _configured_users():
    users = {}
    raw = os.getenv("AUTH_USERS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        username, password = pair.split(":", 1)
        users[username.strip()] = password.strip()
    single_user = os.getenv("APP_USERNAME")
    single_pass = os.getenv("APP_PASSWORD")
    if single_user and single_pass:
        users[single_user] = single_pass
    return users


def auth_enabled():
    return bool(_configured_users())


def check_credentials(username, password):
    users = _configured_users()
    return username in users and users[username] == password


def require_login(request: Request):
    if not auth_enabled():
        return "dev"
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def current_user_optional(request: Request):
    if not auth_enabled():
        return "dev"
    return request.session.get("user")
