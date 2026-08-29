"""
Real per-person accounts with roles, backed by the TallyUser table -
replaces the old env-var shared-login list now that "who can edit the
Skills" and "who approves a sheet" are genuinely different people, not
one shared team password.

On first startup (see main.py's _ensure_seed_users), if TallyUser is
empty, accounts are seeded from the same env vars the old scheme used:
  AUTH_USERS="alice:s3cret:admin,bob:hunter2:approver,carol:pw:creator"
    (role is optional per pair; defaults to "creator")
or, simpler, a single pair (seeded as role "admin", since with only
one account there's no one else to own the Skills):
  APP_USERNAME="team"
  APP_PASSWORD="s3cret"
The env vars are only ever read for that one-time seed - from then on,
an Admin manages accounts from the People page and passwords are
stored hashed, never in plaintext. If neither env var is set AND no
TallyUser rows exist, login is disabled (open access, every request
treated as user "dev" with role "admin") - useful for local
development only; main.py logs a warning in that case.
"""
import os
import time
import hashlib
import hmac
import secrets
from fastapi import Request, HTTPException, Depends

ROLES = ("creator", "approver", "admin")

# ---------- Login rate limiting ----------
# In-memory, per-process - fine for this app's single-uvicorn-worker
# deployment (see README). Keyed by (client IP, username) rather than
# just IP, so one person mistyping their own password repeatedly can't
# lock out everyone else behind the same office NAT/VPN, but a targeted
# credential-guessing attempt against one account from one address is
# still throttled hard.
_LOGIN_ATTEMPT_LIMIT = 8
_LOGIN_ATTEMPT_WINDOW_SECONDS = 600
_failed_login_attempts = {}   # (ip, username) -> [timestamp, ...]


def check_login_rate_limit(client_ip, username):
    key = (client_ip or "unknown", (username or "").lower())
    now = time.time()
    attempts = [t for t in _failed_login_attempts.get(key, []) if now - t < _LOGIN_ATTEMPT_WINDOW_SECONDS]
    _failed_login_attempts[key] = attempts
    if len(attempts) >= _LOGIN_ATTEMPT_LIMIT:
        wait_minutes = max(1, int((_LOGIN_ATTEMPT_WINDOW_SECONDS - (now - attempts[0])) / 60) + 1)
        raise HTTPException(status_code=429, detail=f"Too many failed sign-in attempts. Try again in about {wait_minutes} minute(s).")


def record_failed_login(client_ip, username):
    key = (client_ip or "unknown", (username or "").lower())
    _failed_login_attempts.setdefault(key, []).append(time.time())


def clear_login_attempts(client_ip, username):
    key = (client_ip or "unknown", (username or "").lower())
    _failed_login_attempts.pop(key, None)


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(password, password_hash):
    try:
        salt, digest = password_hash.split("$", 1)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000).hex()
    return hmac.compare_digest(candidate, digest)


def seed_pairs_from_env():
    """[(username, password, role), ...] from AUTH_USERS /
    APP_USERNAME+APP_PASSWORD - used once by main.py's startup seed,
    never at request time. A password here is plaintext (it's an env
    var the operator set), hashed immediately on insert.

    An AUTH_USERS entry with no explicit role defaults to "creator" -
    except when it is the *only* seeded account, where it defaults to
    "admin" instead. Most existing deployments only had one shared
    login before roles existed (e.g. AUTH_USERS=team:changeme), and
    that one account implicitly had full access to everything; keeping
    it "creator"-only on upgrade would silently lock it out of the
    Skills pages and approvals it could always reach before. Once a
    second account is added, unspecified roles go back to "creator" -
    an operator who bothers to list several accounts is expected to
    say who the Admin is."""
    pairs = []
    raw = os.getenv("AUTH_USERS", "")
    for triple in raw.split(","):
        triple = triple.strip()
        if not triple:
            continue
        parts = triple.split(":")
        if len(parts) < 2:
            continue
        username, password = parts[0].strip(), parts[1].strip()
        explicit_role = parts[2].strip() if len(parts) > 2 and parts[2].strip() in ROLES else None
        if username and password:
            pairs.append((username, password, explicit_role))
    single_user = os.getenv("APP_USERNAME")
    single_pass = os.getenv("APP_PASSWORD")
    if single_user and single_pass:
        pairs.append((single_user.strip(), single_pass.strip(), "admin"))
    default_role = "admin" if len(pairs) == 1 else "creator"
    return [(u, p, r or default_role) for u, p, r in pairs]


def auth_enabled():
    from database import SessionLocal
    from models import TallyUser
    session = SessionLocal()
    try:
        return session.query(TallyUser).filter_by(is_active=True).count() > 0
    finally:
        session.close()


def check_credentials(username, password):
    from database import SessionLocal
    from models import TallyUser
    session = SessionLocal()
    try:
        user = session.query(TallyUser).filter_by(username=username, is_active=True).first()
        if not user or not verify_password(password, user.password_hash):
            return False
        return True
    finally:
        session.close()


def get_user_role(username):
    from database import SessionLocal
    from models import TallyUser
    session = SessionLocal()
    try:
        user = session.query(TallyUser).filter_by(username=username, is_active=True).first()
        return user.role if user else None
    finally:
        session.close()


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


def require_role(*allowed_roles):
    """Dependency factory - `Depends(auth.require_role("admin"))`. Runs
    require_login first (401 if not signed in at all), then checks the
    signed-in user's role, 403 with a plain-English reason if it isn't
    one of allowed_roles. Dev mode (no auth configured) always passes
    as role "admin", so local development isn't blocked by a role
    check that has no real accounts to check against."""
    def _check(request: Request, user: str = Depends(require_login)):
        if not auth_enabled():
            return user
        role = get_user_role(user)
        if role not in allowed_roles:
            label = {"admin": "an Admin", "approver": "an Approver", "creator": "a Creator"}
            wanted = " or ".join(label.get(r, r) for r in allowed_roles)
            raise HTTPException(status_code=403, detail=f"This action needs {wanted}. Please contact your administrator.")
        return user
    return _check
