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
from models import TallyRun, TallyGeneration, TallyUploadedFile, TallyFieldMapping, TallyLedgerConfig, TallyRule, TallyPlatform, TallyOutputRow, TallyReviewItem, Event
import auth
import tally_parsing
import tally_rules
import tally_pipeline
import tally_output
import emailer

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
ALLOWED_FILE_ROLES = {"sales", "master", "sample_tally", "other"}

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

app = FastAPI(title="Amazon -> Tally Automation Agent")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-secret-change-me"),
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)


def _ensure_builtin_platforms():
    session = SessionLocal()
    existing = {p.slug for p in session.query(TallyPlatform).all()}
    for slug, display_name in BUILTIN_PLATFORMS:
        if slug not in existing:
            session.add(TallyPlatform(slug=slug, display_name=display_name, is_builtin=True))
    session.commit()
    session.close()


@app.on_event("startup")
def _startup():
    init_db()
    _ensure_builtin_platforms()
    if not auth.auth_enabled():
        print("WARNING: no AUTH_USERS or APP_USERNAME/APP_PASSWORD configured - "
              "running with auth DISABLED (open access). Fine for local dev only.")


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
    """Configure Logic - field mapping, rules, ledger config, platform
    management. Deliberately not part of the Online Sales flow at '/' -
    reachable only via the header's settings link, for whoever
    administers the deterministic pipeline rather than the day-to-day
    user building a sales register."""
    if auth.auth_enabled() and not request.session.get("user"):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))


# ---------- Auth ----------

class LoginPayload(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(payload: LoginPayload, request: Request):
    if not auth.auth_enabled():
        request.session["user"] = "dev"
        return {"ok": True, "user": "dev"}
    if not auth.check_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    request.session["user"] = payload.username
    return {"ok": True, "user": payload.username}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    if not auth.auth_enabled():
        return {"user": "dev", "auth_enabled": False}
    return {"user": request.session.get("user"), "auth_enabled": True}


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
def create_platform(payload: PlatformPayload, user: str = Depends(auth.require_login)):
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
def delete_platform(platform_id: int, user: str = Depends(auth.require_login)):
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

    blob = await file.read()
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
    session.commit()
    session.refresh(record)
    out = _file_out(record)
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


def _validate_rule_payload(p: RulePayload):
    if p.rule_group not in tally_rules.RULE_GROUPS:
        raise HTTPException(400, f"Unknown rule_group '{p.rule_group}'. Valid: {', '.join(tally_rules.RULE_GROUPS)}")
    if p.condition_operator not in tally_rules.CONDITION_OPERATORS:
        raise HTTPException(400, f"Unknown condition_operator '{p.condition_operator}'")
    if p.action_type not in tally_rules.ACTION_TYPES:
        raise HTTPException(400, f"Unknown action_type '{p.action_type}'")
    if p.action_field.strip().lower() in tally_rules.FORBIDDEN_ACTION_FIELDS:
        raise HTTPException(400, f"'{p.action_field}' can never be set by a rule - it always comes straight from the platform's own column, unedited.")


@app.get("/api/tally-rules")
def list_rules(user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rows = session.query(TallyRule).order_by(TallyRule.rule_group, TallyRule.order_index).all()
    out = [_rule_out(r) for r in rows]
    session.close()
    return out


@app.post("/api/tally-rules")
def create_rule(payload: RulePayload, user: str = Depends(auth.require_login)):
    _validate_rule_payload(payload)
    session = SessionLocal()
    rule = TallyRule(**payload.model_dump(), created_by=user)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    out = _rule_out(rule)
    session.close()
    return out


@app.patch("/api/tally-rules/{rule_id}")
def update_rule(rule_id: int, payload: RulePayload, user: str = Depends(auth.require_login)):
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
    session.close()
    return out


@app.delete("/api/tally-rules/{rule_id}")
def delete_rule(rule_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    rule = session.query(TallyRule).filter_by(id=rule_id).first()
    if not rule:
        session.close()
        raise HTTPException(404, "Rule not found")
    session.delete(rule)
    session.commit()
    session.close()
    return {"ok": True}


class ReorderPayload(BaseModel):
    rule_group: str
    ordered_ids: list[int]


@app.post("/api/tally-rules/reorder")
def reorder_rules(payload: ReorderPayload, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    for idx, rule_id in enumerate(payload.ordered_ids):
        rule = session.query(TallyRule).filter_by(id=rule_id, rule_group=payload.rule_group).first()
        if rule:
            rule.order_index = idx
    session.commit()
    session.close()
    return {"ok": True}


class SuggestPayload(BaseModel):
    context: str


@app.post("/api/tally-rules/suggest")
def suggest_rule(payload: SuggestPayload, user: str = Depends(auth.require_login)):
    """AI-assist only - proposes a rule shape, never saves one. The
    human must review the result and POST /api/tally-rules themselves."""
    field_keys = [f["key"] for f in tally_pipeline.CANONICAL_FIELDS]
    try:
        suggestion = tally_rules.suggest_rule(payload.context, canonical_fields=field_keys)
    except Exception as e:
        raise HTTPException(500, f"AI suggestion failed: {e}")
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
    blob = await file.read()
    try:
        text = tally_parsing.extract_document_text(blob, file.content_type, file.filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read '{file.filename}': {e}")
    if not text.strip():
        raise HTTPException(400, f"No readable text found in '{file.filename}'.")

    field_keys = [f["key"] for f in tally_pipeline.CANONICAL_FIELDS]
    try:
        suggestions = tally_rules.suggest_rules_from_document(text, canonical_fields=field_keys)
    except Exception as e:
        raise HTTPException(500, f"AI extraction failed: {e}")
    return suggestions


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
def upsert_mapping(payload: MappingPayload, user: str = Depends(auth.require_login)):
    """Upsert on (target_field, source_file_role, platform_slug,
    order_type) - the mapping UI posts on every dropdown change, so
    this avoids the frontend needing to track whether a mapping row
    already exists. platform_slug and order_type are both part of the
    key: Amazon's and Flipkart's column for the same canonical field
    are two independent rows, and so are a platform's own B2B and B2C
    reports when their column names differ (Bill-to vs Ship-to)."""
    if not payload.source_column_name and not payload.constant_value:
        raise HTTPException(400, "Provide either a source column or a constant value")
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
    session.close()
    return out


@app.delete("/api/tally-field-mappings/{mapping_id}")
def delete_mapping(mapping_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    m = session.query(TallyFieldMapping).filter_by(id=mapping_id).first()
    if not m:
        session.close()
        raise HTTPException(404, "Mapping not found")
    session.delete(m)
    session.commit()
    session.close()
    return {"ok": True}


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
def put_ledger_config(payload: LedgerConfigPayload, user: str = Depends(auth.require_login)):
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
    try:
        tally_pipeline.generate_output(session, run, generation)
    except Exception:
        pass
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
def list_generation_review_items(generation_id: int, status: str | None = None, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    q = session.query(TallyReviewItem).filter_by(generation_id=generation_id, stage="final")
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(TallyReviewItem.created_at).all()
    out = [_review_item_out(i) for i in rows]
    session.close()
    return out


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
    tally_pipeline.apply_review_fix(session, run, item, payload.values)
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
    tally_pipeline.approve_review_suggestion(session, run, item)
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
    tally_pipeline.reject_review_suggestion(session, item)
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

    blob = await file.read()
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
    item.answered_at = datetime.datetime.utcnow()
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

def _build_excel_bytes(session, generation):
    sample_file = session.query(TallyUploadedFile).filter_by(run_id=generation.run_id, file_role="sample_tally").order_by(TallyUploadedFile.uploaded_at.desc()).first()
    if not sample_file:
        raise HTTPException(400, "Upload a sample Tally sheet before downloading")
    parsed = tally_parsing.parse_excel_file(sample_file.file_blob, sample_file.content_type, sample_file.original_filename)
    names = tally_parsing.sheet_names_of(parsed)
    sample_sheet = names[0] if names else None
    sample_columns = tally_parsing.column_names_of(parsed, sample_sheet) if sample_sheet else []
    mappings = session.query(TallyFieldMapping).filter_by(source_file_role="sample_tally").all()
    column_to_field = {m.source_column_name: m.target_field for m in mappings}
    rows = session.query(TallyOutputRow).filter_by(generation_id=generation.id).all()
    row_dicts = [{"data": json.loads(r.data), "status": r.status} for r in rows]
    return tally_output.build_excel_output(sample_columns, column_to_field, row_dicts)


@app.get("/api/tally-generations/{generation_id}/download/excel")
def download_excel(generation_id: int, user: str = Depends(auth.require_login)):
    session = SessionLocal()
    generation = session.query(TallyGeneration).filter_by(id=generation_id).first()
    if not generation:
        session.close()
        raise HTTPException(404, "Generation not found")
    xlsx_bytes = _build_excel_bytes(session, generation)
    session.close()
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="tally_output_{generation_id}.xlsx"'},
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
        xlsx_bytes = _build_excel_bytes(session, generation)
    except HTTPException:
        session.close()
        raise
    try:
        emailer.send_generation_email(
            to=payload.to, cc=payload.cc, subject=payload.subject, note=payload.note,
            attachment_bytes=xlsx_bytes, attachment_filename=f"tally_output_{generation_id}.xlsx",
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
