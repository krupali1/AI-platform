"""
Standalone Amazon -> Tally automation agent. Deliberately independent
of the Client Memory Console (separate app, separate DB, separate
simple shared-login auth) so it can be lifted into the Console later
without carrying that coupling now. Endpoint shape mirrors the
Console's RestConnector CRUD convention on purpose: inline pydantic
payloads, manual SessionLocal()/close() per request, raw dict
responses - so this codebase reads familiarly if it ever does move in.
"""
import os
import re
import json
import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import Response, FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from database import init_db, SessionLocal
from models import (TallyRun, TallyGeneration, TallyUploadedFile, TallyFieldMapping, TallyLedgerConfig, TallyRule,
                     TallyPlatform, TallyOutputRow, TallyReviewItem, Event, TallyUser, TallyNotification, TallySkillVersion,
                     TallySampleFormat, TallyAgentNotes)
import auth
import tally_parsing
import tally_rules
import tally_pipeline
import tally_output
import emailer
import deterministic_import
import qa_checks

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
ALLOWED_FILE_ROLES = {"sales", "master", "sample_tally", "column_mapping", "rules_sheet", "batch_summary", "other"}

# A generous cap for a spreadsheet/document upload (real PIL exports
# seen this session run a few MB) that still stops one oversized or
# malicious upload from exhausting request memory / SQLite storage -
# every upload endpoint in this file reads through this helper rather
# than a bare `await file.read()` so the cap can't be missed on a new one.
MAX_UPLOAD_BYTES = 40 * 1024 * 1024


async def _read_upload_capped(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    blob = await file.read()
    if len(blob) > max_bytes:
        raise HTTPException(413, f"'{file.filename}' is {len(blob) / 1024 / 1024:.1f} MB, over the {max_bytes / 1024 / 1024:.0f} MB upload limit.")
    return blob

# Seeded once on first startup (see _ensure_builtin_platforms) - a starting
# point, not a special case. A user can rename nothing about these, but can
# freely add their own alongside them via POST /api/tally-platforms, and the
# pipeline treats every platform identically regardless of is_builtin.
BUILTIN_PLATFORMS = [
    ("amazon", "Amazon"),
    ("flipkart", "Flipkart"),
    ("meesho", "Meesho"),
    ("nykaa", "Nykaa"),
    ("myntra", "Myntra"),
    ("jiomart", "JioMart"),
]

_SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not _SESSION_SECRET:
    print("WARNING: SESSION_SECRET is not set - using a fixed development value. Session "
          "cookies would be forgeable by anyone who reads this source, so this MUST be set "
          "to a random secret (see .env.example) before this app holds real data on a "
          "network anyone else can reach.")
    _SESSION_SECRET = "dev-secret-change-me"

app = FastAPI(title="Amazon -> Tally Automation Agent")
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    same_site="lax",
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Baseline hardening for an app that handles real financial/PII
    data end to end: stop the browser guessing content types (blocks a
    class of upload-triggered XSS), stop this app being framed by
    another site (clickjacking), and never let a browser or
    intermediate proxy cache an /api/* response - every one of them can
    carry order-level financial data, and a shared/public machine
    caching that to disk is exactly the kind of leak this app exists to
    prevent, not cause."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _ensure_builtin_platforms():
    session = SessionLocal()
    existing = {p.slug for p in session.query(TallyPlatform).all()}
    for slug, display_name in BUILTIN_PLATFORMS:
        if slug not in existing:
            session.add(TallyPlatform(slug=slug, display_name=display_name, is_builtin=True))
    session.commit()
    session.close()


def _ensure_seed_users():
    """One-time migration from the old env-var shared-login list to
    real TallyUser accounts - only runs while the table is still
    empty, so it never overwrites accounts an Admin has since created
    or edited from the People page."""
    session = SessionLocal()
    if session.query(TallyUser).count() > 0:
        session.close()
        return
    for username, password, role in auth.seed_pairs_from_env():
        session.add(TallyUser(username=username, password_hash=auth.hash_password(password),
                               display_name=username, role=role, created_by="env seed"))
    session.commit()
    session.close()


@app.on_event("startup")
def _startup():
    init_db()
    _ensure_builtin_platforms()
    _ensure_seed_users()
    if not auth.auth_enabled():
        print("WARNING: no AUTH_USERS or APP_USERNAME/APP_PASSWORD configured, and no "
              "accounts exist yet - running with auth DISABLED (open access). Fine for local dev only.")


# ---------- Pages ----------

@app.get("/login")
def login_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


@app.get("/")
def index_page(request: Request):
    if auth.auth_enabled() and not request.session.get("user"):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/admin")
def admin_page(request: Request):
    """Configure Logic (field mapping, rules, ledger config, platform
    management) now lives inside the chat app's own Skills page at '/'
    rather than a separate dashboard - this route just forwards
    existing bookmarks/links there."""
    if auth.auth_enabled() and not request.session.get("user"):
        return RedirectResponse("/login")
    return RedirectResponse("/")


# ---------- Auth ----------

class LoginPayload(BaseModel):
    username: str
    password: str


def _user_out(u):
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "email": u.email,
            "role": u.role, "is_active": bool(u.is_active), "created_at": u.created_at.isoformat() if u.created_at else None}


def _me_payload(username):
    if not auth.auth_enabled():
        return {"username": "dev", "display_name": "Dev", "role": "admin"}
    session = SessionLocal()
    u = session.query(TallyUser).filter_by(username=username).first()
    session.close()
    if not u:
        return {"username": username, "display_name": username, "role": "creator"}
    return {"username": u.username, "display_name": u.display_name, "role": u.role}


@app.post("/api/login")
def login(payload: LoginPayload, request: Request):
    if not auth.auth_enabled():
        request.session["user"] = "dev"
        return {"ok": True, **_me_payload("dev")}
    client_ip = request.client.host if request.client else "unknown"
    auth.check_login_rate_limit(client_ip, payload.username)
    if not auth.check_credentials(payload.username, payload.password):
        auth.record_failed_login(client_ip, payload.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    auth.clear_login_attempts(client_ip, payload.username)
    request.session["user"] = payload.username
    return {"ok": True, **_me_payload(payload.username)}


@app.get("/api/me")
def me(user: str = Depends(auth.require_login)):
    return _me_payload(user)


# ---------- People (Admin only) ----------

class UserPayload(BaseModel):
    username: str
    password: str = ""     # blank on update = leave password unchanged
    display_name: str = ""
    email: str = ""
    role: str = "creator"


@app.get("/api/users")
def list_users(user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    rows = session.query(TallyUser).order_by(TallyUser.created_at).all()
    out = [_user_out(u) for u in rows]
    session.close()
    return out


@app.post("/api/users")
def create_user(payload: UserPayload, user: str = Depends(auth.require_role("admin"))):
    if payload.role not in auth.ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(auth.ROLES)}")
    if not payload.password:
        raise HTTPException(400, "A password is required for a new account.")
    session = SessionLocal()
    if session.query(TallyUser).filter_by(username=payload.username).first():
        session.close()
        raise HTTPException(409, f"'{payload.username}' already has an account.")
    new_user = TallyUser(username=payload.username, password_hash=auth.hash_password(payload.password),
                          display_name=payload.display_name or payload.username, email=payload.email or None,
                          role=payload.role, created_by=user)
    session.add(new_user)
    session.commit()
    out = _user_out(new_user)
    session.close()
    return out


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, payload: UserPayload, user: str = Depends(auth.require_role("admin"))):
    if payload.role not in auth.ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(auth.ROLES)}")
    session = SessionLocal()
    target = session.query(TallyUser).filter_by(id=user_id).first()
    if not target:
        session.close()
        raise HTTPException(404, "Not found")
    target.display_name = payload.display_name or target.display_name
    target.email = payload.email or target.email
    target.role = payload.role
    if payload.password:
        target.password_hash = auth.hash_password(payload.password)
    session.commit()
    out = _user_out(target)
    session.close()
    return out


@app.delete("/api/users/{user_id}")
def deactivate_user(user_id: int, user: str = Depends(auth.require_role("admin"))):
    """Deactivates rather than deletes - keeps every record this
    person ever touched (created_by/answered_by/uploaded_by etc.
    across the app) attributable, same reasoning as everywhere else in
    this app that stores a plain username instead of a hard FK."""
    session = SessionLocal()
    target = session.query(TallyUser).filter_by(id=user_id).first()
    if not target:
        session.close()
        raise HTTPException(404, "Not found")
    target.is_active = False
    session.commit()
    session.close()
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ---------- Serializers ----------

def _run_out(r):
    return {
        "id": r.id, "platform_slug": r.platform_slug, "period_label": r.period_label, "status": r.status,
        "error_message": r.error_message, "review_pending_count": r.review_pending_count,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "validated_at": r.validated_at.isoformat() if r.validated_at else None,
    }


def _generation_out(g):
    return {
        "id": g.id, "run_id": g.run_id, "order_type": g.order_type, "location": g.location,
        "status": g.status, "error_message": g.error_message,
        "total_rows": g.total_rows, "flagged_rows": g.flagged_rows, "review_pending_count": g.review_pending_count,
        "created_by": g.created_by,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "processed_at": g.processed_at.isoformat() if g.processed_at else None,
        "review_status": g.review_status,
        "submitted_by": g.submitted_by,
        "submitted_at": g.submitted_at.isoformat() if g.submitted_at else None,
        "approved_by": g.approved_by,
        "approved_at": g.approved_at.isoformat() if g.approved_at else None,
    }


def _file_out(f):
    return {
        "id": f.id, "run_id": f.run_id, "file_role": f.file_role,
        "platform_slug": f.platform_slug, "order_type": f.order_type, "label": f.label,
        "original_filename": f.original_filename, "size_bytes": f.size_bytes,
        "sheet_names": [s for s in (f.sheet_names or "").split(",") if s],
        "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None,
    }


def _platform_out(p):
    return {"id": p.id, "slug": p.slug, "display_name": p.display_name, "is_builtin": bool(p.is_builtin)}


def _rule_out(r):
    return {
        "id": r.id, "rule_group": r.rule_group, "order_index": r.order_index,
        "condition_field": r.condition_field, "condition_operator": r.condition_operator,
        "condition_value": r.condition_value, "action_type": r.action_type,
        "action_field": r.action_field, "action_value": r.action_value,
        "is_active": r.is_active, "description": r.description,
    }


def _mapping_out(m):
    return {
        "id": m.id, "target_field": m.target_field, "source_file_role": m.source_file_role,
        "platform_slug": m.platform_slug, "order_type": m.order_type,
        "source_sheet_name": m.source_sheet_name, "source_column_name": m.source_column_name,
        "constant_value": m.constant_value,
    }


def _output_row_out(r):
    return {
        "id": r.id, "run_id": r.run_id, "generation_id": r.generation_id, "source_file_role": r.source_file_role,
        "source_row_ref": r.source_row_ref, "order_id": r.order_id, "sku": r.sku,
        "data": json.loads(r.data) if r.data else {}, "status": r.status,
        "applied_rule_ids": r.applied_rule_ids, "is_manual_override": bool(r.is_manual_override),
        "override_note": r.override_note,
    }


def _review_item_out(i):
    return {
        "id": i.id, "run_id": i.run_id, "generation_id": i.generation_id,
        "stage": i.stage, "severity": i.severity, "affected_row_ids": i.affected_row_ids,
        "source_label": i.source_label, "location_label": i.location_label,
        "title": i.title, "body": i.body,
        "detail": json.loads(i.detail) if i.detail else {},
        "rule_id": i.rule_id, "rule_name": i.rule_name, "trigger_reason": i.trigger_reason,
        "status": i.status, "resolution_mode": i.resolution_mode,
        "answer_value": i.answer_value,
        "answered_by": i.answered_by,
        "answered_at": i.answered_at.isoformat() if i.answered_at else None,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


def _event_out(e):
    return {
        "id": e.id, "generation_id": e.generation_id, "module": e.module, "status": e.status, "message": e.message,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


# ---------- Runs ----------

class RunPayload(BaseModel):
    platform_slug: str
    period_label: str = ""


@app.post("/api/tally-runs")
def create_run(payload: RunPayload, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    if not session.query(TallyPlatform).filter_by(slug=payload.platform_slug).first():
        session.close()
        raise HTTPException(400, f"Unknown platform '{payload.platform_slug}'")
    run = TallyRun(platform_slug=payload.platform_slug, period_label=payload.period_label, created_by=user)
    session.add(run)
    session.commit()
    session.refresh(run)
    out = _run_out(run)
    session.close()
    return out


@app.get("/api/tally-runs")
def list_runs(platform_slug: str | None = None, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    q = session.query(TallyRun)
    if platform_slug:
        q = q.filter_by(platform_slug=platform_slug)
    rows = q.order_by(TallyRun.created_at.desc()).all()
    out = [_run_out(r) for r in rows]
    session.close()
    return out


@app.get("/api/tally-runs/{run_id}")
def get_run(run_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    run = session.query(TallyRun).filter_by(id=run_id).first()
    if not run:
        session.close()
        raise HTTPException(404, "Run not found")
    out = _run_out(run)
    session.close()
    return out


@app.delete("/api/tally-runs/{run_id}")
def delete_run(run_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    run = session.query(TallyRun).filter_by(id=run_id).first()
    if not run:
        session.close()
        raise HTTPException(404, "Run not found")
    generation_ids = [g.id for g in session.query(TallyGeneration).filter_by(run_id=run_id).all()]
    for gid in generation_ids:
        session.query(TallyOutputRow).filter_by(generation_id=gid).delete()
        session.query(TallyReviewItem).filter_by(generation_id=gid).delete()
        session.query(Event).filter_by(generation_id=gid).delete()
    session.query(TallyGeneration).filter_by(run_id=run_id).delete()
    session.query(TallyUploadedFile).filter_by(run_id=run_id).delete()
    session.query(TallyReviewItem).filter_by(run_id=run_id, stage="input").delete()
    session.delete(run)
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Platforms (sales channels: Amazon, Flipkart, the company's
# own website, or anything a user adds) ----------

def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "platform"


@app.get("/api/tally-platforms")
def list_platforms(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallyPlatform).order_by(TallyPlatform.id).all()
    out = [_platform_out(p) for p in rows]
    session.close()
    return out


class PlatformPayload(BaseModel):
    display_name: str


@app.post("/api/tally-platforms")
def create_platform(payload: PlatformPayload, user: str = Depends(auth.require_role("admin"))):
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(400, "display_name is required")
    session = SessionLocal()
    base_slug = _slugify(display_name)
    slug = base_slug
    n = 2
    while session.query(TallyPlatform).filter_by(slug=slug).first():
        slug = f"{base_slug}_{n}"
        n += 1
    platform = TallyPlatform(slug=slug, display_name=display_name, is_builtin=False)
    session.add(platform)
    session.commit()
    session.refresh(platform)
    out = _platform_out(platform)
    session.close()
    return out


@app.delete("/api/tally-platforms/{platform_id}")
def delete_platform(platform_id: int, user: str = Depends(auth.require_role("admin"))):
    """Deliberately conservative: a built-in platform can never be
    removed (nothing about the data model needs that, and it keeps the
    sidebar from being able to end up with zero platforms), and a
    custom one can't be removed while it still has runs against it -
    delete those first, same "don't silently orphan data" stance as
    everywhere else in this app."""
    session = SessionLocal()
    platform = session.query(TallyPlatform).filter_by(id=platform_id).first()
    if not platform:
        session.close()
        raise HTTPException(404, "Platform not found")
    if platform.is_builtin:
        session.close()
        raise HTTPException(400, "Built-in platforms can't be deleted")
    in_use = session.query(TallyRun).filter_by(platform_slug=platform.slug).first()
    if in_use:
        session.close()
        raise HTTPException(400, "This platform still has runs against it - remove those first")
    session.query(TallyFieldMapping).filter_by(platform_slug=platform.slug).delete()
    session.delete(platform)
    session.commit()
    session.close()
    return {"ok": True}


# ---------- File uploads ----------

def _upsert_field_mapping(session, target_field, source_file_role, platform_slug, order_type, source_column_name, constant_value):
    """Same upsert-by-(target_field, source_file_role, platform_slug,
    order_type) key as POST /api/tally-field-mappings below, used by the
    deterministic column-mapping import so re-uploading a corrected
    sheet updates the same rows instead of accumulating duplicates.
    Doesn't set source_sheet_name (the spec sheet's own rows don't
    reference one) - the manual endpoint keeps that separately."""
    platform_slug = platform_slug or None
    order_type = order_type or None
    existing = session.query(TallyFieldMapping).filter_by(
        target_field=target_field, source_file_role=source_file_role,
        platform_slug=platform_slug, order_type=order_type,
    ).first()
    if existing:
        existing.source_column_name = source_column_name or None
        existing.constant_value = constant_value or None
    else:
        session.add(TallyFieldMapping(
            target_field=target_field, source_file_role=source_file_role,
            platform_slug=platform_slug, order_type=order_type,
            source_column_name=source_column_name or None, constant_value=constant_value or None,
        ))


def _upsert_rule(session, user, rule_group, condition_field, condition_operator, condition_value, action_type, action_field, action_value, description):
    """Dedups on the condition+action shape (everything but order_index/
    is_active/description) so re-uploading a corrected rules sheet
    updates the matching rule in place rather than piling up duplicates
    every time."""
    existing = session.query(TallyRule).filter_by(
        rule_group=rule_group, condition_field=condition_field, condition_operator=condition_operator,
        condition_value=condition_value or "", action_type=action_type, action_field=action_field or "",
    ).first()
    if existing:
        existing.action_value = action_value or ""
        if description:
            existing.description = description
        existing.updated_at = datetime.datetime.utcnow()
    else:
        order_index = session.query(TallyRule).filter_by(rule_group=rule_group).count()
        session.add(TallyRule(
            rule_group=rule_group, order_index=order_index, condition_field=condition_field,
            condition_operator=condition_operator, condition_value=condition_value or "",
            action_type=action_type, action_field=action_field or "", action_value=action_value or "",
            is_active=True, description=description, created_by=user,
        ))


@app.post("/api/tally-runs/{run_id}/files")
async def upload_file(run_id: int, file: UploadFile = File(...), file_role: str = Form(...),
                       order_type: str = Form(""), label: str = Form(""), user: str = Depends(auth.require_login)):
    if file_role not in ALLOWED_FILE_ROLES:
        raise HTTPException(400, f"Unknown file_role '{file_role}'")
    session = SessionLocal()
    run = session.query(TallyRun).filter_by(id=run_id).first()
    if not run:
        session.close()
        raise HTTPException(404, "Run not found")

    # A "sales" file always belongs to its run's own platform - no
    # separate platform_slug to pass in, since the run itself is
    # already scoped to one platform (picked in the sidebar before the
    # run was created).
    platform_slug = run.platform_slug if file_role == "sales" else None
    order_type = (order_type or None) if file_role == "sales" else None

    blob = await _read_upload_capped(file)
    # "other" (Additional file) is explicitly for reference material
    # that isn't a spreadsheet at all - a PDF, an image, a plain note -
    # so it's the one role that skips the spreadsheet-parse requirement
    # every other role needs (to read its columns, or - for
    # column_mapping/rules_sheet below - its rows).
    parsed, sheet_names = None, ""
    if file_role != "other":
        try:
            parsed = tally_parsing.parse_excel_file(blob, file.content_type, file.filename)
            sheet_names = ",".join(tally_parsing.sheet_names_of(parsed))
        except Exception as e:
            session.close()
            raise HTTPException(400, f"Could not read '{file.filename}' as a spreadsheet: {e}")

    record = TallyUploadedFile(
        run_id=run_id, file_role=file_role, platform_slug=platform_slug,
        order_type=order_type, label=label or None,
        original_filename=file.filename, content_type=file.content_type,
        file_blob=blob, size_bytes=len(blob), sheet_names=sheet_names, uploaded_by=user,
    )
    session.add(record)

    # Column Mapping Sheet and Rules Sheet are applied deterministically
    # the moment they're uploaded - no AI, no review step, no demo mode,
    # matching what a real re-uploaded sheet is for: your own ground
    # truth, applied as-is. Anything the parser doesn't recognize is
    # reported back in "import_summary" rather than silently dropped.
    import_summary = None
    if file_role == "column_mapping":
        result = deterministic_import.parse_column_mapping_sheet(parsed)
        for m in result["mappings"]:
            _upsert_field_mapping(session, m["target_field"], m["source_file_role"], platform_slug=run.platform_slug, order_type=m["order_type"], source_column_name=m["source_column_name"], constant_value=m["constant_value"])
        import_summary = {"applied": len(result["mappings"]), "flagged": result["flagged"]}
    elif file_role == "rules_sheet":
        result = deterministic_import.parse_rules_sheet(parsed)
        applied = 0
        flagged = list(result["flagged"])
        for r in result["rules"]:
            # Same field-existence check as the manual Rules editor
            # (_validate_rule_payload) - a typo'd column/condition field
            # in the sheet would otherwise save silently as a rule that
            # can never match anything, with nobody the wiser.
            bad_field = next((f for f in (r["condition_field"], r["action_field"]) if f and f.strip().lower() not in _KNOWN_RULE_FIELD_KEYS), None)
            if bad_field:
                flagged.append({"label": r.get("description") or r["condition_field"], "reason": f'"{bad_field}" isn\'t a known field - this row was not applied. Check GET /api/tally-canonical-fields for valid keys.'})
                continue
            _upsert_rule(session, user, r["rule_group"], r["condition_field"], r["condition_operator"], r["condition_value"], r["action_type"], r["action_field"], r["action_value"], r["description"])
            applied += 1
        import_summary = {"applied": applied, "flagged": flagged}

    session.commit()
    session.refresh(record)
    out = _file_out(record)
    out["import_summary"] = import_summary
    session.close()
    return out


@app.get("/api/tally-runs/{run_id}/files")
def list_files(run_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallyUploadedFile).filter_by(run_id=run_id).order_by(TallyUploadedFile.uploaded_at).all()
    out = [_file_out(f) for f in rows]
    session.close()
    return out


@app.get("/api/tally-runs/{run_id}/files/{file_id}/columns")
def file_columns(run_id: int, file_id: int, sheet: str | None = None, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    f = session.query(TallyUploadedFile).filter_by(id=file_id, run_id=run_id).first()
    if not f:
        session.close()
        raise HTTPException(404, "File not found")
    parsed = tally_parsing.parse_excel_file(f.file_blob, f.content_type, f.original_filename)
    names = tally_parsing.sheet_names_of(parsed)
    sheet_name = sheet if sheet in names else (names[0] if names else None)
    columns = tally_parsing.column_names_of(parsed, sheet_name) if sheet_name else []
    session.close()
    return {"sheets": names, "sheet": sheet_name, "columns": columns}


@app.delete("/api/tally-runs/{run_id}/files/{file_id}")
def delete_file(run_id: int, file_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    f = session.query(TallyUploadedFile).filter_by(id=file_id, run_id=run_id).first()
    if not f:
        session.close()
        raise HTTPException(404, "File not found")
    session.delete(f)
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Canonical fields ----------

@app.get("/api/tally-canonical-fields")
def canonical_fields(user: str = Depends(auth.require_login)):
    return tally_pipeline.CANONICAL_FIELDS


# ---------- Rules ----------

class RulePayload(BaseModel):
    rule_group: str
    order_index: int = 0
    condition_field: str
    condition_operator: str
    condition_value: str = ""
    action_type: str
    action_field: str = ""
    action_value: str = ""
    is_active: bool = True
    description: str = ""


# Every field key a rule could sensibly reference - the full canonical
# vocabulary plus "batch_allocation_status", the one internal-only flag
# the pipeline sets that isn't in CANONICAL_FIELDS (verified against
# every `fields["..."] = ` write site in tally_pipeline.py). Used to
# reject a condition_field/action_field that doesn't exist at all -
# the single biggest source of a silently-inert or wrongly-targeted
# rule, whether a person mistyped it or an AI suggestion hallucinated
# a plausible-looking field name that was never real (see suggest_rule
# in tally_rules.py, which proposes but never saves - this is the
# backstop that catches it even if a reviewer doesn't).
_CANONICAL_FIELD_KEYS = {f["key"] for f in tally_pipeline.CANONICAL_FIELDS}
_KNOWN_RULE_FIELD_KEYS = _CANONICAL_FIELD_KEYS | {"batch_allocation_status"}


def _validate_rule_payload(p: RulePayload):
    if p.rule_group not in tally_rules.RULE_GROUPS:
        raise HTTPException(400, f"Unknown rule_group '{p.rule_group}'. Valid: {', '.join(tally_rules.RULE_GROUPS)}")
    if p.condition_operator not in tally_rules.CONDITION_OPERATORS:
        raise HTTPException(400, f"Unknown condition_operator '{p.condition_operator}'")
    if p.action_type not in tally_rules.ACTION_TYPES:
        raise HTTPException(400, f"Unknown action_type '{p.action_type}'")
    if p.action_field.strip().lower() in tally_rules.FORBIDDEN_ACTION_FIELDS:
        raise HTTPException(400, f"'{p.action_field}' can never be set by a rule - it always comes straight from the platform's own column, unedited.")
    if p.condition_field.strip().lower() not in _KNOWN_RULE_FIELD_KEYS:
        raise HTTPException(400, f"'{p.condition_field}' isn't a known field - it can never match any row, so this rule would silently do nothing. Check GET /api/tally-canonical-fields for valid keys.")
    if p.action_field.strip() and p.action_field.strip().lower() not in _KNOWN_RULE_FIELD_KEYS:
        raise HTTPException(400, f"'{p.action_field}' isn't a known field - this rule would set a value nothing else ever reads. Check GET /api/tally-canonical-fields for valid keys.")


def _next_skill_version(session, skill_key):
    last = session.query(TallySkillVersion).filter_by(skill_key=skill_key).order_by(TallySkillVersion.version_number.desc()).first()
    return (last.version_number + 1) if last else 1


def _snapshot_skill(session, skill_key, snapshot_obj, user, summary):
    """Writes one TallySkillVersion row - a history log the Skills page
    reads for "N versions" / "Restore", never the pipeline's own
    source of truth (that stays the live TallyRule/TallyFieldMapping
    rows). Called after every successful Admin edit to a Skill."""
    v = _next_skill_version(session, skill_key)
    session.add(TallySkillVersion(skill_key=skill_key, version_number=v, snapshot=json.dumps(snapshot_obj),
                                   change_summary=summary, created_by=user))
    session.commit()
    return v


def _rules_snapshot_obj(session):
    return [_rule_out(r) for r in session.query(TallyRule).order_by(TallyRule.rule_group, TallyRule.order_index).all()]


@app.get("/api/tally-rules")
def list_rules(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallyRule).order_by(TallyRule.rule_group, TallyRule.order_index).all()
    out = [_rule_out(r) for r in rows]
    session.close()
    return out


@app.post("/api/tally-rules")
def create_rule(payload: RulePayload, user: str = Depends(auth.require_role("admin"))):
    _validate_rule_payload(payload)
    session = SessionLocal()
    rule = TallyRule(**payload.model_dump(), created_by=user)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    out = _rule_out(rule)
    _snapshot_skill(session, "rules", _rules_snapshot_obj(session), user, f"Added rule: {rule.description or rule.rule_group}")
    session.close()
    return out


@app.patch("/api/tally-rules/{rule_id}")
def update_rule(rule_id: int, payload: RulePayload, user: str = Depends(auth.require_role("admin"))):
    _validate_rule_payload(payload)
    session = SessionLocal()
    rule = session.query(TallyRule).filter_by(id=rule_id).first()
    if not rule:
        session.close()
        raise HTTPException(404, "Rule not found")
    for k, v in payload.model_dump().items():
        setattr(rule, k, v)
    rule.updated_at = datetime.datetime.utcnow()
    session.commit()
    out = _rule_out(rule)
    _snapshot_skill(session, "rules", _rules_snapshot_obj(session), user, f"Edited rule: {rule.description or rule.rule_group}")
    session.close()
    return out


@app.delete("/api/tally-rules/{rule_id}")
def delete_rule(rule_id: int, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    rule = session.query(TallyRule).filter_by(id=rule_id).first()
    if not rule:
        session.close()
        raise HTTPException(404, "Rule not found")
    summary = f"Deleted rule: {rule.description or rule.rule_group}"
    session.delete(rule)
    session.commit()
    _snapshot_skill(session, "rules", _rules_snapshot_obj(session), user, summary)
    session.close()
    return {"ok": True}


class ReorderPayload(BaseModel):
    rule_group: str
    ordered_ids: list[int]


@app.post("/api/tally-rules/reorder")
def reorder_rules(payload: ReorderPayload, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    for idx, rule_id in enumerate(payload.ordered_ids):
        rule = session.query(TallyRule).filter_by(id=rule_id, rule_group=payload.rule_group).first()
        if rule:
            rule.order_index = idx
    session.commit()
    session.close()
    return {"ok": True}


@app.get("/api/tally-rules/versions")
def list_rule_versions(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallySkillVersion).filter_by(skill_key="rules").order_by(TallySkillVersion.version_number.desc()).all()
    out = [{"id": v.id, "version_number": v.version_number, "change_summary": v.change_summary,
            "created_by": v.created_by, "created_at": v.created_at.isoformat() if v.created_at else None} for v in rows]
    session.close()
    return out


@app.post("/api/tally-rules/versions/{version_id}/restore")
def restore_rule_version(version_id: int, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    v = session.query(TallySkillVersion).filter_by(id=version_id, skill_key="rules").first()
    if not v:
        session.close()
        raise HTTPException(404, "Version not found")
    snapshot = json.loads(v.snapshot)
    session.query(TallyRule).delete()
    for r in snapshot:
        session.add(TallyRule(rule_group=r["rule_group"], order_index=r["order_index"], condition_field=r["condition_field"],
                               condition_operator=r["condition_operator"], condition_value=r["condition_value"],
                               action_type=r["action_type"], action_field=r["action_field"], action_value=r["action_value"],
                               is_active=r["is_active"], description=r["description"], created_by=user))
    session.commit()
    _snapshot_skill(session, "rules", _rules_snapshot_obj(session), user, f"Restored to v{v.version_number}")
    session.close()
    return {"ok": True}


def _rule_suggestion_warnings(s):
    """Runs the exact same checks _validate_rule_payload enforces at
    save time, but against a not-yet-saved AI suggestion - so a
    hallucinated rule_group/operator/field shows up as a visible
    warning right where the human is reviewing it, instead of a bare
    400 only surfacing later when they click Save (or, worse, them not
    noticing at all because the field name merely looks plausible)."""
    warnings = []
    if s.get("rule_group") not in tally_rules.RULE_GROUPS:
        warnings.append(f"Unknown rule_group '{s.get('rule_group')}' - not one of: {', '.join(tally_rules.RULE_GROUPS)}")
    if s.get("condition_operator") not in tally_rules.CONDITION_OPERATORS:
        warnings.append(f"Unknown condition_operator '{s.get('condition_operator')}'")
    if s.get("action_type") not in tally_rules.ACTION_TYPES:
        warnings.append(f"Unknown action_type '{s.get('action_type')}'")
    cf = (s.get("condition_field") or "").strip().lower()
    if cf and cf not in _KNOWN_RULE_FIELD_KEYS:
        warnings.append(f"'{s.get('condition_field')}' isn't a real field - this rule would never match any row")
    af = (s.get("action_field") or "").strip().lower()
    if af and af in tally_rules.FORBIDDEN_ACTION_FIELDS:
        warnings.append(f"'{s.get('action_field')}' can never be set by a rule")
    elif af and af not in _KNOWN_RULE_FIELD_KEYS:
        warnings.append(f"'{s.get('action_field')}' isn't a real field - this rule would set a value nothing reads")
    return warnings


class SuggestPayload(BaseModel):
    context: str


@app.post("/api/tally-rules/suggest")
def suggest_rule(payload: SuggestPayload, user: str = Depends(auth.require_login)):
    """AI-assist only - proposes a rule shape, never saves one. The
    human must review the result and POST /api/tally-rules themselves."""
    field_keys = [f["key"] for f in tally_pipeline.CANONICAL_FIELDS]
    session = SessionLocal()
    context = _with_agent_notes(session, payload.context)
    session.close()
    try:
        suggestion = tally_rules.suggest_rule(context, canonical_fields=field_keys)
    except Exception as e:
        raise HTTPException(500, f"AI suggestion failed: {e}")
    suggestion["warnings"] = _rule_suggestion_warnings(suggestion)
    return suggestion


@app.post("/api/tally-rules/suggest-from-document")
async def suggest_rules_from_document(file: UploadFile = File(...), user: str = Depends(auth.require_login)):
    """AI-assist only, document-scale - extracts every rule/condition
    it can find in an uploaded document (.docx/.pdf/.txt/.md, or a
    spreadsheet of edge cases) into a list of proposed rules. Same
    contract as /suggest: nothing here is saved until a human reviews
    each one and calls POST /api/tally-rules themselves. Not tied to a
    run - rules are global config, like the document upload itself is
    a one-time input, not a stored artifact."""
    name = (file.filename or "").lower()
    if not name.endswith(tally_parsing.DOCUMENT_EXTENSIONS):
        raise HTTPException(400, f"Unsupported file type for a rules document. Use one of: {', '.join(tally_parsing.DOCUMENT_EXTENSIONS)}")
    blob = await _read_upload_capped(file)
    try:
        text = tally_parsing.extract_document_text(blob, file.content_type, file.filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read '{file.filename}': {e}")
    if not text.strip():
        raise HTTPException(400, f"No readable text found in '{file.filename}'.")

    field_keys = [f["key"] for f in tally_pipeline.CANONICAL_FIELDS]
    session = SessionLocal()
    text = _with_agent_notes(session, text)
    session.close()
    try:
        suggestions = tally_rules.suggest_rules_from_document(text, canonical_fields=field_keys)
    except Exception as e:
        raise HTTPException(500, f"AI extraction failed: {e}")
    for s in suggestions:
        s["warnings"] = _rule_suggestion_warnings(s)
    return suggestions


class SuggestFromTextPayload(BaseModel):
    text: str


@app.post("/api/tally-rules/suggest-from-text")
def suggest_rules_from_text(payload: SuggestFromTextPayload, user: str = Depends(auth.require_login)):
    """Same contract as /suggest-from-document, for someone who wants to
    type or paste exceptions/rules directly instead of uploading a file -
    reuses the same document-scale extraction since pasted text needs no
    file parsing."""
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "No text provided.")
    field_keys = [f["key"] for f in tally_pipeline.CANONICAL_FIELDS]
    session = SessionLocal()
    text = _with_agent_notes(session, text)
    session.close()
    try:
        suggestions = tally_rules.suggest_rules_from_document(text, canonical_fields=field_keys)
    except Exception as e:
        raise HTTPException(500, f"AI extraction failed: {e}")
    for s in suggestions:
        s["warnings"] = _rule_suggestion_warnings(s)
    return suggestions


@app.post("/api/tally-field-mappings/suggest-from-document")
async def suggest_field_mappings_from_document(file: UploadFile = File(...), platform_slug: str = Form(""), user: str = Depends(auth.require_login)):
    """AI-assist only - reads a column-mapping spec (e.g. a PRD-style
    'Tally column | Source sheet | Source column' table exported to
    .docx/.pdf/.xlsx/.csv/.txt/.md) and proposes TallyFieldMapping rows,
    both input (file column -> canonical field) and output (canonical
    field -> sample sheet column). Same contract as the rules-document
    upload: nothing here is saved until a human reviews each one and
    calls POST /api/tally-field-mappings themselves."""
    name = (file.filename or "").lower()
    if not name.endswith(tally_parsing.DOCUMENT_EXTENSIONS):
        raise HTTPException(400, f"Unsupported file type for a mapping document. Use one of: {', '.join(tally_parsing.DOCUMENT_EXTENSIONS)}")
    blob = await _read_upload_capped(file)
    try:
        text = tally_parsing.extract_document_text(blob, file.content_type, file.filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read '{file.filename}': {e}")
    if not text.strip():
        raise HTTPException(400, f"No readable text found in '{file.filename}'.")
    session = SessionLocal()
    text = _with_agent_notes(session, text)
    session.close()
    try:
        result = tally_rules.suggest_field_mappings_from_document(text, tally_pipeline.CANONICAL_FIELDS, platform_slug or None)
    except Exception as e:
        raise HTTPException(500, f"AI extraction failed: {e}")
    _annotate_mapping_suggestion_warnings(result)
    return result


def _annotate_mapping_suggestion_warnings(result):
    """Same defense-in-depth as _rule_suggestion_warnings: the prompt
    already tells the model to only use real canonical fields, but this
    catches it server-side if one slips through anyway, rather than
    trusting the model's own compliance."""
    for m in result.get("mappings", []):
        warnings = []
        if (m.get("target_field") or "").strip().lower() not in _CANONICAL_FIELD_KEYS:
            warnings.append(f"'{m.get('target_field')}' isn't a real canonical field")
        m["warnings"] = warnings


class SuggestMappingsFromTextPayload(BaseModel):
    text: str
    platform_slug: str = ""


@app.post("/api/tally-field-mappings/suggest-from-text")
def suggest_field_mappings_from_text(payload: SuggestMappingsFromTextPayload, user: str = Depends(auth.require_login)):
    """Same contract as /suggest-from-document, for pasted/typed text
    instead of an uploaded file."""
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "No text provided.")
    session = SessionLocal()
    text = _with_agent_notes(session, text)
    session.close()
    try:
        result = tally_rules.suggest_field_mappings_from_document(text, tally_pipeline.CANONICAL_FIELDS, payload.platform_slug or None)
    except Exception as e:
        raise HTTPException(500, f"AI extraction failed: {e}")
    _annotate_mapping_suggestion_warnings(result)
    return result


# ---------- Field mappings ----------

class MappingPayload(BaseModel):
    target_field: str
    source_file_role: str
    platform_slug: str = ""    # only meaningful when source_file_role == "sales"
    order_type: str = ""       # only meaningful when source_file_role == "sales"
    source_sheet_name: str = ""
    source_column_name: str = ""
    constant_value: str = ""   # mutually exclusive with source_column_name - a literal applied to every row


@app.get("/api/tally-field-mappings")
def list_mappings(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallyFieldMapping).all()
    out = [_mapping_out(m) for m in rows]
    session.close()
    return out


@app.post("/api/tally-field-mappings")
def upsert_mapping(payload: MappingPayload, user: str = Depends(auth.require_role("admin"))):
    """Upsert on (target_field, source_file_role, platform_slug,
    order_type) - the mapping UI posts on every dropdown change, so
    this avoids the frontend needing to track whether a mapping row
    already exists. platform_slug and order_type are both part of the
    key: Amazon's and Flipkart's column for the same canonical field
    are two independent rows, and so are a platform's own B2B and B2C
    reports when their column names differ (Bill-to vs Ship-to)."""
    if not payload.source_column_name and not payload.constant_value:
        raise HTTPException(400, "Provide either a source column or a constant value")
    if payload.target_field.strip().lower() not in _CANONICAL_FIELD_KEYS:
        raise HTTPException(400, f"'{payload.target_field}' isn't a known canonical field - nothing in the pipeline would ever read it. Check GET /api/tally-canonical-fields for valid keys.")
    is_sales = payload.source_file_role == "sales"
    platform_slug = (payload.platform_slug or None) if is_sales else None
    order_type = (payload.order_type or None) if is_sales else None
    session = SessionLocal()
    existing = session.query(TallyFieldMapping).filter_by(
        target_field=payload.target_field, source_file_role=payload.source_file_role,
        platform_slug=platform_slug, order_type=order_type,
    ).first()
    if existing:
        existing.source_sheet_name = payload.source_sheet_name or None
        existing.source_column_name = payload.source_column_name or None
        existing.constant_value = payload.constant_value or None
        session.commit()
        out = _mapping_out(existing)
    else:
        m = TallyFieldMapping(
            target_field=payload.target_field, source_file_role=payload.source_file_role,
            platform_slug=platform_slug, order_type=order_type,
            source_sheet_name=payload.source_sheet_name or None, source_column_name=payload.source_column_name or None,
            constant_value=payload.constant_value or None,
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        out = _mapping_out(m)
    _snapshot_skill(session, "field_mappings", [_mapping_out(r) for r in session.query(TallyFieldMapping).all()],
                     user, f"Set mapping: {payload.target_field} ({payload.source_file_role})")
    session.close()
    return out


@app.delete("/api/tally-field-mappings/{mapping_id}")
def delete_mapping(mapping_id: int, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    m = session.query(TallyFieldMapping).filter_by(id=mapping_id).first()
    if not m:
        session.close()
        raise HTTPException(404, "Mapping not found")
    summary = f"Deleted mapping: {m.target_field} ({m.source_file_role})"
    session.delete(m)
    session.commit()
    _snapshot_skill(session, "field_mappings", [_mapping_out(r) for r in session.query(TallyFieldMapping).all()],
                     user, summary)
    session.close()
    return {"ok": True}


@app.get("/api/tally-field-mappings/versions")
def list_mapping_versions(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallySkillVersion).filter_by(skill_key="field_mappings").order_by(TallySkillVersion.version_number.desc()).all()
    out = [{"id": v.id, "version_number": v.version_number, "change_summary": v.change_summary,
            "created_by": v.created_by, "created_at": v.created_at.isoformat() if v.created_at else None} for v in rows]
    session.close()
    return out


# ---------- Ledger config ----------

@app.get("/api/tally-ledger-config-keys")
def ledger_config_keys(user: str = Depends(auth.require_login)):
    return [{"key": k, "label": label} for k, label in tally_pipeline.LEDGER_CONFIG_KEYS]


@app.get("/api/tally-ledger-config")
def get_ledger_config(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    stored = {r.config_key: r.config_value for r in session.query(TallyLedgerConfig).all()}
    session.close()
    return {key: stored.get(key, "") for key, _ in tally_pipeline.LEDGER_CONFIG_KEYS}


class LedgerConfigPayload(BaseModel):
    values: dict[str, str]


@app.put("/api/tally-ledger-config")
def put_ledger_config(payload: LedgerConfigPayload, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    for key, value in payload.values.items():
        row = session.query(TallyLedgerConfig).filter_by(config_key=key).first()
        if row:
            row.config_value = value
            row.updated_at = datetime.datetime.utcnow()
        else:
            session.add(TallyLedgerConfig(config_key=key, config_value=value))
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Sample Tally Format (a Skill - set once, not re-uploaded per run) ----------

def _sample_format_out(row):
    if not row:
        return {"columns": [], "updated_by": None, "updated_at": None}
    return {"columns": json.loads(row.columns_json), "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@app.get("/api/tally-sample-format")
def get_sample_format(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    row = session.query(TallySampleFormat).order_by(TallySampleFormat.id.desc()).first()
    out = _sample_format_out(row)
    session.close()
    return out


def _save_sample_format(session, columns, user, summary):
    columns = [c.strip() for c in columns if c and c.strip()]
    if not columns:
        raise HTTPException(400, "Provide at least one column name")
    row = session.query(TallySampleFormat).order_by(TallySampleFormat.id.desc()).first()
    if not row:
        row = TallySampleFormat(columns_json="[]")
        session.add(row)
    row.columns_json = json.dumps(columns)
    row.updated_by = user
    row.updated_at = datetime.datetime.utcnow()
    session.commit()
    _snapshot_skill(session, "sample_format", {"columns": columns}, user, summary)
    return row


class SampleFormatPayload(BaseModel):
    columns: list[str]


@app.put("/api/tally-sample-format")
def put_sample_format(payload: SampleFormatPayload, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    row = _save_sample_format(session, payload.columns, user, f"Set to {len(payload.columns)} column(s) (typed)")
    out = _sample_format_out(row)
    session.close()
    return out


@app.post("/api/tally-sample-format/from-file")
async def set_sample_format_from_file(file: UploadFile = File(...), user: str = Depends(auth.require_role("admin"))):
    """Parses an uploaded sample sheet's header row exactly once and
    saves the resulting column list as the standing Skill - the file
    itself isn't kept, only the column names/order it had, which is
    all the Excel export ever actually needed from it."""
    blob = await _read_upload_capped(file)
    try:
        parsed = tally_parsing.parse_excel_file(blob, file.content_type, file.filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read '{file.filename}' as a spreadsheet: {e}")
    names = tally_parsing.sheet_names_of(parsed)
    if not names:
        raise HTTPException(400, "No sheets found in this file")
    columns = tally_parsing.column_names_of(parsed, names[0])
    session = SessionLocal()
    row = _save_sample_format(session, columns, user, f"Set to {len(columns)} column(s) from '{file.filename}'")
    out = _sample_format_out(row)
    session.close()
    return out


@app.get("/api/tally-sample-format/versions")
def list_sample_format_versions(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallySkillVersion).filter_by(skill_key="sample_format").order_by(TallySkillVersion.version_number.desc()).all()
    out = [{"id": v.id, "version_number": v.version_number, "change_summary": v.change_summary,
            "created_by": v.created_by, "created_at": v.created_at.isoformat() if v.created_at else None} for v in rows]
    session.close()
    return out


# ---------- Agent Notes (freeform markdown Skill, typed in chat -
# background/conventions/edge cases that don't fit the structured
# Rules/Field Mapping/Master tables; fed as context into the AI-assist
# calls in tally_rules.py, never applied by the pipeline directly) ----------

def _agent_notes_out(row):
    if not row:
        return {"body_md": "", "updated_by": None, "updated_at": None}
    return {"body_md": row.body_md or "", "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def current_agent_notes(session):
    """What tally_rules.py's AI-assist calls prepend as context - the
    empty string when no notes have been written yet."""
    row = session.query(TallyAgentNotes).order_by(TallyAgentNotes.id.desc()).first()
    return (row.body_md or "").strip() if row else ""


def _with_agent_notes(session, text):
    """Prepends the Agent Notes Skill (if any) to a prompt input, so
    every AI-assist call in this file picks it up automatically - the
    Admin writes it once, not per rule/mapping suggestion."""
    notes = current_agent_notes(session)
    if not notes:
        return text
    return (f"Standing background notes an Admin wrote for you to keep in mind (the \"Agent Notes\" Skill):\n"
            f"\"\"\"{notes}\"\"\"\n\n{text}")


@app.get("/api/tally-agent-notes")
def get_agent_notes(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    row = session.query(TallyAgentNotes).order_by(TallyAgentNotes.id.desc()).first()
    out = _agent_notes_out(row)
    session.close()
    return out


class AgentNotesPayload(BaseModel):
    body_md: str


@app.put("/api/tally-agent-notes")
def put_agent_notes(payload: AgentNotesPayload, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    body = payload.body_md.strip()
    row = session.query(TallyAgentNotes).order_by(TallyAgentNotes.id.desc()).first()
    if not row:
        row = TallyAgentNotes(body_md="")
        session.add(row)
    row.body_md = body
    row.updated_by = user
    row.updated_at = datetime.datetime.utcnow()
    session.commit()
    words = len(body.split())
    _snapshot_skill(session, "agent_notes", {"body_md": body}, user, f"Updated notes ({words} word{'s' if words != 1 else ''})")
    out = _agent_notes_out(row)
    session.close()
    return out


@app.get("/api/tally-agent-notes/versions")
def list_agent_notes_versions(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallySkillVersion).filter_by(skill_key="agent_notes").order_by(TallySkillVersion.version_number.desc()).all()
    out = [{"id": v.id, "version_number": v.version_number, "change_summary": v.change_summary,
            "created_by": v.created_by, "created_at": v.created_at.isoformat() if v.created_at else None} for v in rows]
    session.close()
    return out


@app.post("/api/tally-agent-notes/versions/{version_id}/restore")
def restore_agent_notes_version(version_id: int, user: str = Depends(auth.require_role("admin"))):
    session = SessionLocal()
    v = session.query(TallySkillVersion).filter_by(id=version_id, skill_key="agent_notes").first()
    if not v:
        session.close()
        raise HTTPException(404, "Version not found")
    snapshot = json.loads(v.snapshot)
    row = session.query(TallyAgentNotes).order_by(TallyAgentNotes.id.desc()).first()
    if not row:
        row = TallyAgentNotes(body_md="")
        session.add(row)
    row.body_md = snapshot.get("body_md", "")
    row.updated_by = user
    row.updated_at = datetime.datetime.utcnow()
    session.commit()
    _snapshot_skill(session, "agent_notes", snapshot, user, f"Restored to v{v.version_number}")
    out = _agent_notes_out(row)
    session.close()
    return out


# ---------- Stage 1: validate the run's uploaded input files ----------

@app.post("/api/tally-runs/{run_id}/validate")
def validate_run(run_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    run = session.query(TallyRun).filter_by(id=run_id).first()
    if not run:
        session.close()
        raise HTTPException(404, "Run not found")
    try:
        tally_pipeline.validate_inputs(session, run)
    except Exception:
        pass   # run.status/error_message already reflect the failure
    out = _run_out(run)
    session.close()
    return out


@app.get("/api/tally-runs/{run_id}/slices")
def get_slices(run_id: int, user: str = Depends(auth.require_login)):
    """Distinct order types and locations (Godowns) found in the run's
    mapped sales rows - what the "Build the Tally sheet" step lets you
    pick from."""
    session = SessionLocal()
    run = session.query(TallyRun).filter_by(id=run_id).first()
    if not run:
        session.close()
        raise HTTPException(404, "Run not found")
    order_types, locations = tally_pipeline.available_slices(session, run)
    session.close()
    return {"order_types": order_types, "locations": locations}


@app.get("/api/tally-runs/{run_id}/review-items")
def list_run_review_items(run_id: int, status: str | None = None, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    q = session.query(TallyReviewItem).filter_by(run_id=run_id, stage="input")
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(TallyReviewItem.created_at).all()
    out = [_review_item_out(i) for i in rows]
    session.close()
    return out


# ---------- Stage 2: build a Tally sheet for one (order type, location) slice ----------

class GenerationPayload(BaseModel):
    run_id: int
    order_type: str = ""
    location: str = ""


@app.post("/api/tally-generations")
def create_generation(payload: GenerationPayload, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    run = session.query(TallyRun).filter_by(id=payload.run_id).first()
    if not run:
        session.close()
        raise HTTPException(404, "Run not found")
    if run.review_pending_count:
        session.close()
        raise HTTPException(409, "Resolve the open input issues before building the Tally sheet")
    generation = TallyGeneration(run_id=run.id, order_type=payload.order_type or None, location=payload.location or None, created_by=user)
    session.add(generation)
    session.commit()
    session.refresh(generation)
    try:
        tally_pipeline.generate_output(session, run, generation)
    except Exception:
        pass
    out = _generation_out(generation)
    session.close()
    return out


@app.post("/api/tally-generations/{generation_id}/regenerate")
def regenerate(generation_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    run = session.query(TallyRun).filter_by(id=generation.run_id).first()
    was_locked = generation.review_status in ("submitted", "approved", "returned")
    try:
        tally_pipeline.generate_output(session, run, generation)
    except Exception:
        pass
    if was_locked:
        # The underlying inputs changed (new/edited file), so a
        # previously submitted/approved/returned sheet no longer
        # reflects what was reviewed - it must go through approval
        # again rather than staying "approved" against stale data.
        generation.review_status = "draft"
        generation.submitted_by = None
        generation.submitted_at = None
        generation.approved_by = None
        generation.approved_at = None
        session.commit()
    out = _generation_out(generation)
    session.close()
    return out


@app.get("/api/tally-runs/{run_id}/generations")
def list_generations(run_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallyGeneration).filter_by(run_id=run_id).order_by(TallyGeneration.created_at.desc()).all()
    out = [_generation_out(g) for g in rows]
    session.close()
    return out


@app.get("/api/tally-generations/{generation_id}")
def get_generation(generation_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    g = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not g:
        session.close()
        raise HTTPException(404, "Generation not found")
    out = _generation_out(g)
    session.close()
    return out


@app.get("/api/tally-generations/{generation_id}/events")
def list_generation_events(generation_id: int, user: str = Depends(auth.require_login)):
    """Powers the animated "agent run" log - one Event per named
    pipeline step, in the order they actually executed."""
    session = SessionLocal()
    rows = session.query(Event).filter_by(generation_id=generation_id).order_by(Event.created_at).all()
    out = [_event_out(e) for e in rows]
    session.close()
    return out


@app.get("/api/tally-generations/{generation_id}/review-items")
def list_generation_review_items(generation_id: int, status: str | None = None, stage: str = "final", user: str = Depends(auth.require_login)):
    """stage defaults to "final" (the pipeline's own build-time
    exceptions); pass stage="review" for the Approver's per-cell
    comments on this same generation instead."""
    session = SessionLocal()
    q = session.query(TallyReviewItem).filter_by(generation_id=generation_id, stage=stage)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(TallyReviewItem.created_at).all()
    out = [_review_item_out(i) for i in rows]
    session.close()
    return out


@app.get("/api/tally-generations/{generation_id}/qa-report")
def get_qa_report(generation_id: int, user: str = Depends(auth.require_login)):
    """Runs qa_checks.py's deterministic integrity checks against this
    generation on demand (not stored - always fresh against the
    generation's current data, cheap enough to recompute per view for
    the row counts this app deals with). Informational: does not gate
    submit-for-approval or download, which stay controlled by the
    review-item queue and the approval workflow respectively."""
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    run = session.query(TallyRun).filter_by(id=generation.run_id).first()
    if generation.status == "processing":
        session.close()
        return {"checks": [], "status": "processing"}
    checks = qa_checks.run_qa_checks(session, run, generation)
    session.close()
    overall = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    return {"checks": checks, "status": overall}


# ---------- Approval workflow (Creator submits, Approver approves or sends back) ----------

def _notify(session, username, kind, generation_id, title, message=""):
    session.add(TallyNotification(username=username, kind=kind, generation_id=generation_id, title=title, message=message))
    session.commit()


def _generation_label(g):
    return f"{g.order_type or 'Sheet'} - {g.location or ''}".strip(" -")


@app.post("/api/tally-generations/{generation_id}/submit-for-approval")
def submit_for_approval(generation_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    if generation.status != "ready":
        session.close()
        raise HTTPException(409, "Resolve every flagged row before sending this sheet for approval")
    generation.review_status = "submitted"
    generation.submitted_by = user
    generation.submitted_at = datetime.datetime.utcnow()
    generation.approved_by = None
    generation.approved_at = None
    session.commit()
    approvers = [u.username for u in session.query(TallyUser).filter_by(role="approver", is_active=True).all()]
    for approver in approvers:
        _notify(session, approver, "submitted_for_review", generation.id,
                "A Tally sheet is waiting for your review", f"{user} sent {_generation_label(generation)} for approval.")
    out = _generation_out(generation)
    session.close()
    return out


@app.post("/api/tally-generations/{generation_id}/approve")
def approve_generation(generation_id: int, user: str = Depends(auth.require_role("approver", "admin"))):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    if generation.review_status != "submitted":
        session.close()
        raise HTTPException(409, "This sheet isn't waiting for approval")
    generation.review_status = "approved"
    generation.approved_by = user
    generation.approved_at = datetime.datetime.utcnow()
    session.commit()
    creator = generation.submitted_by or generation.created_by
    if creator:
        _notify(session, creator, "approved", generation.id, "Your Tally sheet was approved",
                f"{user} approved {_generation_label(generation)}.")
    out = _generation_out(generation)
    session.close()
    return out


class SendBackPayload(BaseModel):
    comments: list[dict]   # [{"row_id": int|None, "field": str, "message": str}, ...]


@app.post("/api/tally-generations/{generation_id}/send-back")
def send_back_generation(generation_id: int, payload: SendBackPayload, user: str = Depends(auth.require_role("approver", "admin"))):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    if generation.review_status != "submitted":
        session.close()
        raise HTTPException(409, "This sheet isn't waiting for approval")
    added = 0
    for c in payload.comments:
        message = (c.get("message") or "").strip()
        if not message:
            continue
        row_id = c.get("row_id")
        field = (c.get("field") or "").strip()
        session.add(TallyReviewItem(
            run_id=generation.run_id, generation_id=generation.id, stage="review", severity="warning",
            affected_row_ids=str(row_id) if row_id else None,
            source_label="Approver comment", location_label=field or None,
            title=f"Approver comment{' on ' + field if field else ''}", body=message,
            detail=json.dumps({"available_modes": ["fix"], "default_mode": "fix",
                                "fix": {"note": message, "fields": ([{"key": field, "label": field, "value": "", "type": "text"}] if field else [])}}),
            trigger_reason="approver_comment", status="pending",
        ))
        added += 1
    if not added:
        session.close()
        raise HTTPException(400, "Add at least one comment so the Creator knows what to fix")
    generation.review_status = "returned"
    generation.approved_by = None
    generation.approved_at = None
    session.commit()
    tally_pipeline.finalize_generation(session, generation)
    creator = generation.submitted_by or generation.created_by
    if creator:
        _notify(session, creator, "returned", generation.id, "Your Tally sheet was sent back",
                f"{user} left {added} comment{'s' if added != 1 else ''} on {_generation_label(generation)}.")
    out = _generation_out(generation)
    session.close()
    return out


# ---------- Notifications ----------

def _notification_out(n):
    return {"id": n.id, "kind": n.kind, "generation_id": n.generation_id, "title": n.title, "message": n.message,
            "is_read": bool(n.is_read), "created_at": n.created_at.isoformat() if n.created_at else None}


@app.get("/api/notifications")
def list_notifications(unread_only: bool = False, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    q = session.query(TallyNotification).filter_by(username=user)
    if unread_only:
        q = q.filter_by(is_read=False)
    rows = q.order_by(TallyNotification.created_at.desc()).limit(50).all()
    out = [_notification_out(n) for n in rows]
    session.close()
    return out


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    n = session.query(TallyNotification).filter_by(id=notification_id, username=user).first()
    if not n:
        session.close()
        raise HTTPException(404, "Not found")
    n.is_read = True
    session.commit()
    session.close()
    return {"ok": True}


@app.post("/api/notifications/read-all")
def mark_all_notifications_read(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    session.query(TallyNotification).filter_by(username=user, is_read=False).update({"is_read": True})
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Review items (human-in-the-loop, 3 resolution modes) ----------

class FixPayload(BaseModel):
    values: dict[str, str]


@app.post("/api/tally-review-items/{item_id}/apply-fix")
def apply_fix(item_id: int, payload: FixPayload, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    item = session.query(TallyReviewItem).filter_by(id=item_id).first()
    if not item:
        session.close()
        raise HTTPException(404, "Not found")
    if item.status != "pending":
        session.close()
        raise HTTPException(409, "This item has already been resolved")
    run = session.query(TallyRun).filter_by(id=item.run_id).first()
    tally_pipeline.apply_review_fix(session, run, item, payload.values, user=user)
    out = _review_item_out(item)
    session.close()
    return out


@app.post("/api/tally-review-items/{item_id}/approve-suggestion")
def approve_suggestion(item_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    item = session.query(TallyReviewItem).filter_by(id=item_id).first()
    if not item:
        session.close()
        raise HTTPException(404, "Not found")
    if item.status != "pending":
        session.close()
        raise HTTPException(409, "This item has already been resolved")
    run = session.query(TallyRun).filter_by(id=item.run_id).first()
    tally_pipeline.approve_review_suggestion(session, run, item, user=user)
    out = _review_item_out(item)
    session.close()
    return out


@app.post("/api/tally-review-items/{item_id}/reject-suggestion")
def reject_suggestion(item_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    item = session.query(TallyReviewItem).filter_by(id=item_id).first()
    if not item:
        session.close()
        raise HTTPException(404, "Not found")
    tally_pipeline.reject_review_suggestion(session, item, user=user)
    out = _review_item_out(item)
    session.close()
    return out


@app.post("/api/tally-review-items/{item_id}/defer")
def defer_review_item(item_id: int, user: str = Depends(auth.require_login)):
    """"Decide later" - acknowledges the exception without resolving
    it, so it stops blocking progress but the underlying gap (a blank
    column, an unmapped SKU) stays exactly as unresolved as before."""
    session = SessionLocal()
    item = session.query(TallyReviewItem).filter_by(id=item_id).first()
    if not item:
        session.close()
        raise HTTPException(404, "Not found")
    if item.status != "pending":
        session.close()
        raise HTTPException(409, "This item has already been resolved")
    run = session.query(TallyRun).filter_by(id=item.run_id).first()
    tally_pipeline.defer_review_item(session, run, item, user=user)
    out = _review_item_out(item)
    session.close()
    return out


@app.post("/api/tally-review-items/{item_id}/reupload")
async def reupload_for_review_item(item_id: int, file: UploadFile = File(...), user: str = Depends(auth.require_login)):
    session = SessionLocal()
    item = session.query(TallyReviewItem).filter_by(id=item_id).first()
    if not item:
        session.close()
        raise HTTPException(404, "Not found")
    if item.status != "pending":
        session.close()
        raise HTTPException(409, "This item has already been resolved")
    detail = json.loads(item.detail or "{}")
    target_role = (detail.get("reupload") or {}).get("target_file_role")
    if not target_role:
        session.close()
        raise HTTPException(400, "This item has no re-upload target")
    run = session.query(TallyRun).filter_by(id=item.run_id).first()
    if not run:
        session.close()
        raise HTTPException(404, "Run not found")

    blob = await _read_upload_capped(file)
    try:
        parsed = tally_parsing.parse_excel_file(blob, file.content_type, file.filename)
        sheet_names = ",".join(tally_parsing.sheet_names_of(parsed))
    except Exception as e:
        session.close()
        raise HTTPException(400, f"Could not read '{file.filename}': {e}")

    existing_file = session.query(TallyUploadedFile).filter_by(run_id=run.id, file_role=target_role).order_by(TallyUploadedFile.uploaded_at.desc()).first()
    if existing_file:
        existing_file.original_filename = file.filename
        existing_file.content_type = file.content_type
        existing_file.file_blob = blob
        existing_file.size_bytes = len(blob)
        existing_file.sheet_names = sheet_names
        existing_file.uploaded_by = user
        existing_file.uploaded_at = datetime.datetime.utcnow()
    else:
        session.add(TallyUploadedFile(
            run_id=run.id, file_role=target_role, original_filename=file.filename, content_type=file.content_type,
            file_blob=blob, size_bytes=len(blob), sheet_names=sheet_names, uploaded_by=user,
        ))

    item.status = "answered"
    item.resolution_mode = "reupload"
    item.answer_value = json.dumps({"note": f"Re-uploaded '{file.filename}'"})
    item.answered_at = datetime.datetime.utcnow()
    item.answered_by = user
    session.commit()

    try:
        if item.stage == "input":
            tally_pipeline.validate_inputs(session, run)
        elif item.generation_id:
            generation = session.query(TallyGeneration).filter_by(id=item.generation_id).first()
            if generation:
                tally_pipeline.generate_output(session, run, generation)
    except Exception:
        pass   # status/error already reflect the failure
    session.close()
    return {"ok": True}


# ---------- Output rows ----------

@app.get("/api/tally-generations/{generation_id}/output-rows")
def list_output_rows(generation_id: int, status: str | None = None, search: str | None = None,
                      page: int = 1, page_size: int = 50, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    q = session.query(TallyOutputRow).filter_by(generation_id=generation_id)
    if status:
        q = q.filter_by(status=status)
    if search:
        like = f"%{search}%"
        q = q.filter((TallyOutputRow.order_id.like(like)) | (TallyOutputRow.sku.like(like)))
    total = q.count()
    rows = q.order_by(TallyOutputRow.id).offset((page - 1) * page_size).limit(page_size).all()
    out = {"total": total, "page": page, "page_size": page_size, "rows": [_output_row_out(r) for r in rows]}
    session.close()
    return out


class OverridePayload(BaseModel):
    data: dict
    note: str = ""


@app.patch("/api/tally-generations/{generation_id}/output-rows/{row_id}")
def override_output_row(generation_id: int, row_id: int, payload: OverridePayload, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    row = session.query(TallyOutputRow).filter_by(id=row_id, generation_id=generation_id).first()
    if not row:
        session.close()
        raise HTTPException(404, "Row not found")
    fields = json.loads(row.data)
    fields.update(payload.data)
    row.data = json.dumps(fields)
    row.is_manual_override = True
    row.override_note = payload.note or row.override_note
    if row.status == "flagged":
        row.status = "resolved"
    row.updated_at = datetime.datetime.utcnow()
    session.commit()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if generation:
        tally_pipeline.finalize_generation(session, generation)
    out = _output_row_out(row)
    session.close()
    return out


@app.post("/api/tally-generations/{generation_id}/output-rows/{row_id}/exclude")
def exclude_output_row(generation_id: int, row_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    row = session.query(TallyOutputRow).filter_by(id=row_id, generation_id=generation_id).first()
    if not row:
        session.close()
        raise HTTPException(404, "Row not found")
    row.status = "excluded"
    session.commit()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if generation:
        tally_pipeline.finalize_generation(session, generation)
    session.close()
    return {"ok": True}


# ---------- Downloads ----------

def _build_excel_zip_bytes(session, generation):
    # The Sample Tally Format Skill (set once, admin-managed) is the
    # normal path now - a run's own uploaded file_role="sample_tally"
    # file is only consulted as a fallback, so a run from before the
    # Skill existed (or one that deliberately overrides it for a
    # one-off) still works unchanged.
    skill_row = session.query(TallySampleFormat).order_by(TallySampleFormat.id.desc()).first()
    sample_columns = json.loads(skill_row.columns_json) if skill_row else None
    if not sample_columns:
        sample_file = session.query(TallyUploadedFile).filter_by(run_id=generation.run_id, file_role="sample_tally").order_by(TallyUploadedFile.uploaded_at.desc()).first()
        if not sample_file:
            raise HTTPException(400, "Set the Sample Tally Format Skill (Skills page, Admin) or attach a Sample Tally sheet to this run before downloading")
        parsed = tally_parsing.parse_excel_file(sample_file.file_blob, sample_file.content_type, sample_file.original_filename)
        names = tally_parsing.sheet_names_of(parsed)
        sample_sheet = names[0] if names else None
        sample_columns = tally_parsing.column_names_of(parsed, sample_sheet) if sample_sheet else []
    mappings = session.query(TallyFieldMapping).filter_by(source_file_role="sample_tally").all()
    column_to_field = {m.source_column_name: m.target_field for m in mappings}
    rows = session.query(TallyOutputRow).filter_by(generation_id=generation.id).all()
    row_dicts = [{"data": json.loads(r.data), "status": r.status} for r in rows]
    run = session.query(TallyRun).filter_by(id=generation.run_id).first()
    platform = session.query(TallyPlatform).filter_by(slug=run.platform_slug).first() if run else None
    platform_name = platform.display_name if platform else (run.platform_slug if run else "")
    period_label = run.period_label if run else ""
    return tally_output.build_excel_files(sample_columns, column_to_field, row_dicts, platform_name, period_label)


@app.get("/api/tally-generations/{generation_id}/download/excel")
def download_excel(generation_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    zip_bytes = _build_excel_zip_bytes(session, generation)
    session.close()
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="tally_output_{generation_id}.zip"'},
    )


@app.get("/api/tally-generations/{generation_id}/download/tally-xml")
def download_xml(generation_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    ledger_config = {r.config_key: r.config_value for r in session.query(TallyLedgerConfig).all()}
    rows = session.query(TallyOutputRow).filter_by(generation_id=generation_id).all()
    row_dicts = [{"data": json.loads(r.data), "status": r.status} for r in rows]
    xml_bytes = tally_output.build_tally_xml(row_dicts, ledger_config)
    session.close()
    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="tally_import_{generation_id}.xml"'},
    )


class EmailPayload(BaseModel):
    to: str
    cc: str = ""
    subject: str
    note: str = ""


@app.post("/api/tally-generations/{generation_id}/email")
def email_generation(generation_id: int, payload: EmailPayload, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    try:
        zip_bytes = _build_excel_zip_bytes(session, generation)
    except HTTPException:
        session.close()
        raise
    try:
        emailer.send_generation_email(
            to=payload.to, cc=payload.cc, subject=payload.subject, note=payload.note,
            attachment_bytes=zip_bytes, attachment_filename=f"tally_output_{generation_id}.zip",
        )
    except emailer.EmailNotConfigured as e:
        session.close()
        raise HTTPException(400, str(e))
    except Exception as e:
        session.close()
        raise HTTPException(502, f"Email send failed: {e}")
    session.close()
    return {"ok": True}


# ---------- Activity log ----------

@app.get("/api/events")
def list_events(limit: int = 50, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(Event).order_by(Event.created_at.desc()).limit(limit).all()
    out = [_event_out(e) for e in rows]
    session.close()
    return out
