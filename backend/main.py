from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
import datetime
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import or_
from typing import List, Optional
from pydantic import BaseModel

from database import init_db, SessionLocal
from models import Client, Meeting, Document, Decision, ActionItem, OpenQuestion, Brief, Contradiction, PromptEngine, PromptEngineProject, EngineOutput, RestConnector, RestConnectorProject, RestConnectorCredential, OAuthProvider, OAuthConnection, Event, User, ProjectMembership
from manifest import MODULES
from auth import oauth, is_email_allowed, is_admin_email
from crypto import encrypt, decrypt
import connectors
import extraction
import brief as brief_module
import contradiction
import email_connector
import custom_engine
import rest_connector
import oauth_generic
import llm_client
import qa

app = FastAPI(title="Client Memory Console")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-insecure-change-me"),
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)

ANTHROPIC_SERVER_KEY = os.getenv("ANTHROPIC_API_KEY")  # optional platform-wide fallback, Anthropic only - see get_user_llm_config

RUNNERS = {
    "fireflies-connector": connectors.run_fireflies,
    "drive-connector": connectors.run_drive,
    "gmail-connector": email_connector.run_gmail,
    "digest-notifier": email_connector.run_digest,
    # extraction-engine, brief-generator, and contradiction-detector are
    # handled separately in api_run - they need the current user's key,
    # which the runners above don't take.
}


def get_user_llm_config(user):
    """Builds the {"provider", "model", "api_key", "endpoint_url"} dict
    every AI-calling module expects, from this user's own preferred
    provider and their key for it. Returns None (triggering demo mode
    in whichever module receives it) if that provider has no key set -
    never falls back to a different provider's key silently, since
    picking a provider is exactly the point of this system."""
    provider = user.preferred_provider or "anthropic"
    model = user.preferred_model or llm_client.SUGGESTED_MODELS.get(provider, "")
    key_column = {
        "anthropic": user.encrypted_anthropic_key,
        "openai": user.encrypted_openai_key,
        "gemini": user.encrypted_gemini_key,
        "custom": user.encrypted_custom_key,
    }.get(provider)
    api_key = decrypt(key_column) if key_column else None
    if not api_key:
        # Only Anthropic has a server-wide fallback - the only provider
        # this platform has ever had a key for by default. No such
        # fallback exists for other providers; a user who hasn't set
        # one of those up gets demo mode, not a silent switch.
        if provider == "anthropic" and ANTHROPIC_SERVER_KEY:
            api_key = ANTHROPIC_SERVER_KEY
        else:
            return None
    if not model:
        return None
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "endpoint_url": user.custom_endpoint_url if provider == "custom" else None,
    }


def get_automation_llm_config(client, session):
    """Same shape as get_user_llm_config, for the background auto-sync loop
    below, which has no signed-in user of its own to draw a preferred
    provider/key from - but a project isn't ownerless just because nobody's
    actively clicking RUN on it. Tries, in order:

    1. Each of this project's admins' own configured key (earliest-added
       admin first - typically whoever created the project), so a project
       whose owner already set up their own provider/key from the header
       gets real automatic runs without needing a second, separate
       server-wide key on top of the one they already configured.
    2. The server-wide Anthropic fallback, if set.

    None (demo mode, same as a manual RUN with no key) only if neither
    produces a usable config."""
    admin_emails = [
        m.email for m in session.query(ProjectMembership)
        .filter_by(client_id=client.id, role="admin")
        .order_by(ProjectMembership.created_at.asc()).all()
    ]
    for email in admin_emails:
        admin_user = session.query(User).filter_by(email=email).first()
        if not admin_user:
            continue
        config = get_user_llm_config(admin_user)
        if config:
            return config

    if not ANTHROPIC_SERVER_KEY:
        return None
    return {
        "provider": "anthropic",
        "model": llm_client.SUGGESTED_MODELS.get("anthropic", ""),
        "api_key": ANTHROPIC_SERVER_KEY,
        "endpoint_url": None,
    }


def get_current_project(request: Request, session):
    """Reads the selected project id out of the session. Returns the
    Client row, or None if no project has been created/selected yet.

    A role="client" user's project is never read from the session,
    even if they somehow got a project_id written there - it's always
    forced to their locked_project_id, so a client account can never
    end up looking at another project's data via session manipulation."""
    user_id = request.session.get("user_id")
    if user_id:
        user = session.query(User).filter_by(id=user_id).first()
        if user and user.role == "client":
            if not user.locked_project_id:
                return None
            return session.query(Client).filter_by(id=user.locked_project_id).first()
    project_id = request.session.get("project_id")
    if not project_id:
        return None
    return session.query(Client).filter_by(id=project_id).first()


@app.on_event("startup")
def startup():
    init_db()
    _seed_builtin_connectors()
    _migrate_scope_to_project_links()
    _migrate_project_admins()
    asyncio.create_task(_auto_sync_loop())


def _migrate_scope_to_project_links():
    """One-time backfill for anyone upgrading from the old single-client_id
    scoping model to the many-to-many PromptEngineProject/
    RestConnectorProject one. Only touches rows that have zero project
    links yet, so it's safe to run on every startup - once a row has
    real links (from this migration or from being created fresh under
    the new model), it's never touched again. An engine/connector whose
    old client_id was a specific project gets linked to just that one;
    one whose client_id was null (the old meaning of "every project")
    gets linked to every project that exists as of this upgrade - a
    one-time snapshot, not an ongoing "auto-add to new projects" rule,
    since that's now an explicit choice offered when a new project is
    created, not something to keep happening silently forever."""
    session = SessionLocal()
    try:
        all_project_ids = [c.id for c in session.query(Client.id).all()]
        for engine in session.query(PromptEngine).all():
            if session.query(PromptEngineProject).filter_by(engine_id=engine.id).first():
                continue
            targets = all_project_ids if engine.client_id is None else [engine.client_id]
            for pid in targets:
                session.add(PromptEngineProject(engine_id=engine.id, client_id=pid))
        for rc in session.query(RestConnector).all():
            if session.query(RestConnectorProject).filter_by(connector_id=rc.id).first():
                continue
            targets = all_project_ids if rc.client_id is None else [rc.client_id]
            for pid in targets:
                session.add(RestConnectorProject(connector_id=rc.id, client_id=pid))
        session.commit()
    finally:
        session.close()


def _migrate_project_admins():
    """One-time backfill for anyone upgrading from before per-project
    roles existed. Team & Keys is now gated on a user's *project-level*
    role being admin, falling back to their global role only when a
    project has zero membership rows at all (see
    get_effective_project_role) - without this, every existing project
    would have zero rows the instant this shipped, and a global "member"
    who previously had Team & Keys access (global admin/member both did)
    would silently lose it. This grants every current global admin and
    member an explicit "admin" membership on every project that doesn't
    have any membership rows yet, preserving today's actual access
    exactly. Only touches projects with zero memberships, so it's safe
    to run on every startup - a project that already has real membership
    rows (from this migration or from being set up fresh) is never
    touched again, and new projects going forward only auto-add their
    creator, not every member org-wide."""
    session = SessionLocal()
    try:
        admin_and_member_emails = [
            u.email for u in session.query(User).filter(User.role.in_(["admin", "member"])).all()
        ]
        if not admin_and_member_emails:
            return
        for client in session.query(Client).all():
            if session.query(ProjectMembership).filter_by(client_id=client.id).first():
                continue
            for email in admin_and_member_emails:
                session.add(ProjectMembership(client_id=client.id, email=email, role="admin"))
        session.commit()
    finally:
        session.close()


def _seed_builtin_connectors():
    """Google Calendar reachable with zero connector-specific Python
    code, purely as a demonstration that the config-driven system
    genuinely covers it - not a special case, the same RestConnector
    row any admin could create from the dashboard form. Only inserted
    if it isn't already there, so this is safe to run on every startup."""
    session = SessionLocal()
    try:
        if not session.query(RestConnector).filter_by(module_id="conn-google-calendar").first():
            session.add(RestConnector(
                client_id=None,
                module_id="conn-google-calendar",
                display_name="Google Calendar",
                description="Pulls calendar events matching the project's name, via the same connected Google account as Drive and Gmail.",
                search_url_template="https://www.googleapis.com/calendar/v3/calendars/primary/events?q={query}",
                auth_style="google_oauth",
                results_path="items",
                field_id="id",
                field_title="summary",
                field_content="description",
                field_url="htmlLink",
                field_date="start.dateTime",
            ))
            session.commit()
    finally:
        session.close()


def _serialize(obj):
    row = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
    return row


# ---------- Auth ----------

def get_current_user(request: Request) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = SessionLocal()
    user = session.query(User).filter_by(id=user_id).first()
    session.close()
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@app.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/")
    return FileResponse("../frontend/login.html")


@app.get("/auth/google/login")
async def google_login(request: Request):
    redirect_uri = request.url_for("google_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback", name="google_callback")
async def google_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        raise HTTPException(400, "Google did not return user info")

    email = userinfo["email"]
    if not is_email_allowed(email):
        return RedirectResponse(url="/login?error=domain_not_allowed")

    session = SessionLocal()
    user = session.query(User).filter_by(google_sub=userinfo["sub"]).first()
    if not user:
        user = User(
            google_sub=userinfo["sub"],
            email=email,
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
            role="admin" if is_admin_email(email) else "member",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    elif is_admin_email(email) and user.role != "admin":
        # Someone was added to ADMIN_EMAILS since they last signed in -
        # promote them now rather than requiring a manual DB edit.
        user.role = "admin"
        session.commit()
    request.session["user_id"] = user.id
    landing = "/portal" if user.role == "client" else "/projects"
    session.close()
    return RedirectResponse(url=landing)


@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


def require_role(*roles):
    """Dependency factory: require_role("admin") or require_role("admin", "member")."""
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Not permitted for your role")
        return user
    return checker


def get_effective_project_role(user, project_id, session):
    """A user's role for one specific project, not their global one - see
    ProjectMembership's docstring. A global admin is always project-admin
    everywhere, full stop. Otherwise, this project's own membership row
    for that email wins if one exists; a project with no membership rows
    at all (not upgraded yet, or nobody's been explicitly added) falls
    back to the user's global role, matching pre-per-project-role
    behavior rather than silently locking everyone out."""
    if user.role == "admin":
        return "admin"
    membership = session.query(ProjectMembership).filter_by(client_id=project_id, email=user.email).first()
    return membership.role if membership else user.role


def require_project_admin(user, project_id, session):
    """Raises 403 unless this user's role for this specific project is
    admin. Called inline (not a FastAPI Depends) since the endpoints this
    guards already take project_id as a path param and manage their own
    session, matching this file's existing style for project-scoped
    credential endpoints."""
    if get_effective_project_role(user, project_id, session) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required for this project's Team & Keys")


@app.get("/api/me")
def api_me(user: User = Depends(get_current_user)):
    return {
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "role": user.role,
        "preferred_provider": user.preferred_provider or "anthropic",
        "preferred_model": user.preferred_model or llm_client.SUGGESTED_MODELS.get(user.preferred_provider or "anthropic", ""),
        "custom_provider_name": user.custom_provider_name,
        "custom_endpoint_url": user.custom_endpoint_url,
        "has_key": {
            "anthropic": bool(user.encrypted_anthropic_key),
            "openai": bool(user.encrypted_openai_key),
            "gemini": bool(user.encrypted_gemini_key),
            "custom": bool(user.encrypted_custom_key),
        },
        # kept for any older frontend code still reading this directly
        "has_api_key": bool(user.encrypted_anthropic_key),
    }


class ProviderPayload(BaseModel):
    provider: str
    model: str = ""
    custom_provider_name: str = ""
    custom_endpoint_url: str = ""


@app.post("/api/me/provider")
def set_provider(payload: ProviderPayload, user: User = Depends(get_current_user)):
    if payload.provider not in ("anthropic", "openai", "gemini", "custom"):
        raise HTTPException(400, "provider must be anthropic, openai, gemini, or custom")
    if payload.provider == "custom" and not payload.custom_endpoint_url.strip():
        raise HTTPException(400, "custom_endpoint_url is required for the custom provider")
    session = SessionLocal()
    db_user = session.query(User).filter_by(id=user.id).first()
    db_user.preferred_provider = payload.provider
    db_user.preferred_model = payload.model.strip() or llm_client.SUGGESTED_MODELS.get(payload.provider, "")
    if payload.provider == "custom":
        db_user.custom_provider_name = payload.custom_provider_name.strip() or "Custom"
        db_user.custom_endpoint_url = payload.custom_endpoint_url.strip()
    session.commit()
    session.close()
    return {"ok": True}


class ApiKeyPayload(BaseModel):
    api_key: str
    provider: str = "anthropic"


_KEY_COLUMN = {
    "anthropic": "encrypted_anthropic_key",
    "openai": "encrypted_openai_key",
    "gemini": "encrypted_gemini_key",
    "custom": "encrypted_custom_key",
}


@app.post("/api/me/api-key")
def set_api_key(payload: ApiKeyPayload, user: User = Depends(get_current_user)):
    key = payload.api_key.strip()
    if not key:
        raise HTTPException(400, "api_key is required")
    column = _KEY_COLUMN.get(payload.provider)
    if not column:
        raise HTTPException(400, "provider must be anthropic, openai, gemini, or custom")
    session = SessionLocal()
    db_user = session.query(User).filter_by(id=user.id).first()
    setattr(db_user, column, encrypt(key))
    session.commit()
    session.close()
    return {"ok": True}


@app.delete("/api/me/api-key")
def clear_api_key(provider: str = "anthropic", user: User = Depends(get_current_user)):
    column = _KEY_COLUMN.get(provider)
    if not column:
        raise HTTPException(400, "provider must be anthropic, openai, gemini, or custom")
    session = SessionLocal()
    db_user = session.query(User).filter_by(id=user.id).first()
    setattr(db_user, column, None)
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Projects ----------

def _snippet(text, length=160):
    """A short, plain-text preview of a longer document - strips markdown
    headers/emphasis markers and collapses whitespace so a truncated brief
    reads as a clean sentence fragment in a table row, not a chopped-off
    '### Head' with stray asterisks."""
    if not text:
        return None
    import re
    plain = re.sub(r"[#*_`]+", "", text)
    plain = " ".join(plain.split())
    if len(plain) <= length:
        return plain
    return plain[:length].rsplit(" ", 1)[0] + "…"


@app.get("/api/projects")
def list_projects(user: User = Depends(get_current_user)):
    session = SessionLocal()
    projects = session.query(Client).order_by(Client.created_at.asc()).all()
    out = []
    for p in projects:
        latest_brief = session.query(Brief).filter_by(client_id=p.id).order_by(Brief.id.desc()).first()
        out.append({
            "id": p.id,
            "name": p.name,
            "domain": p.domain,
            "team_emails": p.team_emails,
            "drive_folder_id": p.drive_folder_id,
            "has_fireflies_key": bool(p.encrypted_fireflies_key),
            "has_resend_key": bool(p.encrypted_resend_key),
            "notify_email": p.notify_email,
            "has_drive_credentials": bool(p.encrypted_drive_credentials),
            "drive_oauth_email": p.drive_oauth_email,
            "has_drive_oauth": bool(p.encrypted_drive_refresh_token),
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "meetings_count": session.query(Meeting).filter_by(client_id=p.id).count(),
            "documents_count": session.query(Document).filter_by(client_id=p.id).count(),
            "decisions_count": session.query(Decision).filter_by(client_id=p.id).count(),
            "action_items_count": session.query(ActionItem).filter_by(client_id=p.id).count(),
            "latest_brief_snippet": _snippet(latest_brief.content) if latest_brief else None,
        })
    session.close()
    return out


@app.get("/api/projects/current")
def current_project(request: Request, user: User = Depends(get_current_user)):
    session = SessionLocal()
    project = get_current_project(request, session)
    out = None
    if project:
        out = {
            "id": project.id, "name": project.name,
            "my_role": get_effective_project_role(user, project.id, session),
        }
    session.close()
    return out


class ProjectPayload(BaseModel):
    name: str
    domain: str = ""


@app.post("/api/projects")
def create_project(payload: ProjectPayload, request: Request, user: User = Depends(get_current_user)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    session = SessionLocal()
    if session.query(Client).filter_by(name=name).first():
        session.close()
        raise HTTPException(400, "a project with this name already exists")
    project = Client(name=name, domain=payload.domain.strip())
    session.add(project)
    session.commit()
    session.refresh(project)
    project_id = project.id
    # The creator gets admin on their own new project - otherwise a
    # global "member" who isn't already project-admin somewhere would
    # create a project and immediately be locked out of its own Team &
    # Keys section.
    session.add(ProjectMembership(client_id=project_id, email=user.email, role="admin"))
    session.commit()
    session.close()
    request.session["project_id"] = project_id
    return {"id": project_id, "name": name, "domain": payload.domain.strip()}


@app.post("/api/projects/{project_id}/select")
def select_project(project_id: int, request: Request, user: User = Depends(get_current_user)):
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    session.close()
    if not project:
        raise HTTPException(404, "project not found")
    request.session["project_id"] = project_id
    return {"ok": True}


class ProjectUpdatePayload(BaseModel):
    domain: Optional[str] = None
    team_emails: Optional[str] = None
    notify_email: Optional[str] = None


@app.patch("/api/projects/{project_id}")
def update_project(project_id: int, payload: ProjectUpdatePayload, user: User = Depends(get_current_user)):
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    if payload.domain is not None:
        project.domain = payload.domain.strip()
    if payload.team_emails is not None:
        project.team_emails = payload.team_emails.strip()
    if payload.notify_email is not None:
        project.notify_email = payload.notify_email.strip()
    session.commit()
    out = {
        "id": project.id, "name": project.name, "domain": project.domain,
        "team_emails": project.team_emails, "notify_email": project.notify_email,
    }
    session.close()
    return out


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int, request: Request, user: User = Depends(require_role("admin", "member"))):
    """Deletes a project and everything scoped to it - meetings, documents,
    decisions, action items, briefs, open questions, contradictions, agent
    outputs, the event log, and its links to shared agents/connectors/OAuth
    connections. Shared PromptEngine/RestConnector definitions themselves
    aren't deleted (they may still serve other projects) - only this
    project's association with them, via the same *Project join tables
    used to scope them in the first place.

    Frontend loops this per id for "delete multiple" / "delete all" -
    there's no separate bulk endpoint, same pattern as Repository's
    existing per-record bulk delete."""
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")

    for model in (Meeting, Document, Decision, ActionItem, Brief, OpenQuestion,
                  Contradiction, EngineOutput, Event):
        session.query(model).filter_by(client_id=project_id).delete()
    session.query(PromptEngineProject).filter_by(client_id=project_id).delete()
    session.query(RestConnectorProject).filter_by(client_id=project_id).delete()
    session.query(RestConnectorCredential).filter_by(client_id=project_id).delete()
    session.query(OAuthConnection).filter_by(client_id=project_id).delete()
    # Not a scoping reference (see PromptEngine/RestConnector's own
    # comments) - just "created from" metadata, cleared so it doesn't
    # dangle at a client_id that no longer exists.
    session.query(PromptEngine).filter_by(client_id=project_id).update({"client_id": None})
    session.query(RestConnector).filter_by(client_id=project_id).update({"client_id": None})
    # A client-role user locked to this project would otherwise be locked
    # to a project id that no longer exists - get_current_project already
    # degrades that to "no project" rather than crashing, but clearing it
    # explicitly is more honest than leaving a dangling reference.
    session.query(User).filter_by(locked_project_id=project_id).update({"locked_project_id": None})

    session.delete(project)
    session.commit()
    session.close()

    if request.session.get("project_id") == project_id:
        request.session.pop("project_id", None)

    return {"ok": True}


class CredentialPayload(BaseModel):
    value: str


@app.post("/api/projects/{project_id}/fireflies-key")
def set_fireflies_key(project_id: int, payload: CredentialPayload, user: User = Depends(require_role("admin", "member"))):
    value = payload.value.strip()
    if not value:
        raise HTTPException(400, "value is required")
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    project.encrypted_fireflies_key = encrypt(value)
    session.commit()
    session.close()
    return {"ok": True}


@app.delete("/api/projects/{project_id}/fireflies-key")
def clear_fireflies_key(project_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    project.encrypted_fireflies_key = None
    session.commit()
    session.close()
    return {"ok": True}


@app.post("/api/projects/{project_id}/resend-key")
def set_resend_key(project_id: int, payload: CredentialPayload, user: User = Depends(require_role("admin", "member"))):
    value = payload.value.strip()
    if not value:
        raise HTTPException(400, "value is required")
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    project.encrypted_resend_key = encrypt(value)
    session.commit()
    session.close()
    return {"ok": True}


@app.delete("/api/projects/{project_id}/resend-key")
def clear_resend_key(project_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    project.encrypted_resend_key = None
    session.commit()
    session.close()
    return {"ok": True}


@app.post("/api/projects/{project_id}/drive-credentials")
def set_drive_credentials(project_id: int, payload: CredentialPayload, user: User = Depends(require_role("admin", "member"))):
    value = payload.value.strip()
    if not value:
        raise HTTPException(400, "value is required")
    try:
        import json
        json.loads(value)
    except Exception:
        raise HTTPException(400, "This doesn't look like valid JSON - paste the full downloaded service-account file contents")
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    project.encrypted_drive_credentials = encrypt(value)
    session.commit()
    session.close()
    return {"ok": True}


@app.delete("/api/projects/{project_id}/drive-credentials")
def clear_drive_credentials(project_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    project.encrypted_drive_credentials = None
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Drive OAuth (connect a real Google account instead of a service account) ----------
# The static /callback route MUST be registered before the dynamic
# /{project_id} route below it - FastAPI matches routes in registration
# order, and a request to /drive/connect/callback would otherwise match
# {project_id} first, with "callback" as its literal value.

@app.get("/drive/connect/callback", name="drive_connect_callback")
async def drive_connect_callback(request: Request):
    project_id = request.session.get("drive_auth_project_id")
    if not project_id:
        return RedirectResponse(url="/?error=drive_connect_expired")

    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    refresh_token = token.get("refresh_token")

    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        return RedirectResponse(url="/")

    project.drive_oauth_email = userinfo.get("email")
    project.encrypted_drive_access_token = encrypt(token["access_token"])
    if refresh_token:
        # Google only sends this when it actually issues one - with
        # prompt=consent it should be every time, but don't overwrite a
        # working refresh token with nothing on the rare call where it's absent.
        project.encrypted_drive_refresh_token = encrypt(refresh_token)
    expires_at = token.get("expires_at")
    if expires_at:
        project.drive_token_expiry = datetime.datetime.utcfromtimestamp(expires_at)
    session.commit()
    session.close()
    request.session.pop("drive_auth_project_id", None)
    return RedirectResponse(url="/")


@app.get("/drive/connect/{project_id}")
async def drive_connect(project_id: int, request: Request, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    session.close()
    # Stashed server-side in the session, not trusted from the callback's
    # query params, so this can't be redirected to attach to a project
    # the person clicking wasn't actually authorized to modify.
    request.session["drive_auth_project_id"] = project_id
    redirect_uri = request.url_for("drive_connect_callback")
    return await oauth.google.authorize_redirect(
        request,
        redirect_uri,
        scope="openid email https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly",
        access_type="offline",
        prompt="consent",  # forces Google to return a refresh_token every time, not just on first-ever consent
    )


@app.post("/api/projects/{project_id}/drive-disconnect")
def drive_disconnect(project_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    project = session.query(Client).filter_by(id=project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    project.drive_oauth_email = None
    project.encrypted_drive_access_token = None
    project.encrypted_drive_refresh_token = None
    project.drive_token_expiry = None
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Project members (per-project roles) ----------

PROJECT_ROLES = ("admin", "member", "viewer", "client")


def _serialize_membership(m):
    return {"id": m.id, "email": m.email, "role": m.role, "created_at": m.created_at.isoformat() if m.created_at else None}


@app.get("/api/projects/{project_id}/members")
def list_project_members(project_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    if not session.query(Client).filter_by(id=project_id).first():
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    members = session.query(ProjectMembership).filter_by(client_id=project_id).order_by(ProjectMembership.created_at.asc()).all()
    out = [_serialize_membership(m) for m in members]
    session.close()
    return out


class MemberPayload(BaseModel):
    email: str
    role: str = "member"


@app.post("/api/projects/{project_id}/members")
def add_project_member(project_id: int, payload: MemberPayload, user: User = Depends(require_role("admin", "member"))):
    email = payload.email.strip().lower()
    role = payload.role.strip().lower()
    if not email:
        raise HTTPException(400, "email is required")
    if role not in PROJECT_ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(PROJECT_ROLES)}")
    session = SessionLocal()
    if not session.query(Client).filter_by(id=project_id).first():
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    # Adding someone already on the roster just updates their role in
    # place, rather than erroring or creating a second row for the same
    # email - re-adding with a different role is how you'd naturally
    # expect to change it from this same form.
    existing = session.query(ProjectMembership).filter_by(client_id=project_id, email=email).first()
    if existing:
        existing.role = role
        session.commit()
        out = _serialize_membership(existing)
    else:
        m = ProjectMembership(client_id=project_id, email=email, role=role)
        session.add(m)
        session.commit()
        session.refresh(m)
        out = _serialize_membership(m)
    session.close()
    return out


class MemberRolePayload(BaseModel):
    role: str


@app.patch("/api/projects/{project_id}/members/{member_id}")
def update_project_member(project_id: int, member_id: int, payload: MemberRolePayload, user: User = Depends(require_role("admin", "member"))):
    role = payload.role.strip().lower()
    if role not in PROJECT_ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(PROJECT_ROLES)}")
    session = SessionLocal()
    if not session.query(Client).filter_by(id=project_id).first():
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    member = session.query(ProjectMembership).filter_by(id=member_id, client_id=project_id).first()
    if not member:
        session.close()
        raise HTTPException(404, "member not found")
    if member.role == "admin" and role != "admin":
        admin_count = session.query(ProjectMembership).filter_by(client_id=project_id, role="admin").count()
        if admin_count <= 1:
            session.close()
            raise HTTPException(400, "Can't remove this project's last admin")
    member.role = role
    session.commit()
    out = _serialize_membership(member)
    session.close()
    return out


@app.delete("/api/projects/{project_id}/members/{member_id}")
def remove_project_member(project_id: int, member_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    if not session.query(Client).filter_by(id=project_id).first():
        session.close()
        raise HTTPException(404, "project not found")
    require_project_admin(user, project_id, session)
    member = session.query(ProjectMembership).filter_by(id=member_id, client_id=project_id).first()
    if not member:
        session.close()
        raise HTTPException(404, "member not found")
    if member.role == "admin":
        admin_count = session.query(ProjectMembership).filter_by(client_id=project_id, role="admin").count()
        if admin_count <= 1:
            session.close()
            raise HTTPException(400, "Can't remove this project's last admin")
    session.delete(member)
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Dashboard (protected) ----------

@app.get("/")
def root(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")
    session = SessionLocal()
    user = session.query(User).filter_by(id=user_id).first()
    session.close()
    if user and user.role == "client":
        return RedirectResponse(url="/portal")
    return FileResponse("../frontend/dashboard.html")


@app.get("/portal")
def portal_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")
    session = SessionLocal()
    user = session.query(User).filter_by(id=user_id).first()
    session.close()
    if not user or user.role != "client":
        return RedirectResponse(url="/")
    return FileResponse("../frontend/portal.html")


@app.get("/api/portal/summary")
def portal_summary(user: User = Depends(get_current_user)):
    if user.role != "client":
        raise HTTPException(403, "This endpoint is for client accounts only")
    if not user.locked_project_id:
        raise HTTPException(403, "No project assigned to this account yet - contact your admin")
    session = SessionLocal()
    project = session.query(Client).filter_by(id=user.locked_project_id).first()
    if not project:
        session.close()
        raise HTTPException(404, "assigned project not found")
    latest_brief = session.query(Brief).filter_by(client_id=project.id).order_by(Brief.created_at.desc()).first()
    decisions = session.query(Decision).filter_by(client_id=project.id).order_by(Decision.id.desc()).all()
    out = {
        "project_name": project.name,
        "brief": {"content": latest_brief.content, "created_at": latest_brief.created_at.isoformat()} if latest_brief else None,
        "decisions": [{"description": d.description, "created_at": d.created_at.isoformat()} for d in decisions],
    }
    session.close()
    return out


@app.get("/projects")
def projects_page(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse(url="/login")
    return FileResponse("../frontend/projects.html")


@app.get("/admin")
def admin_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")
    session = SessionLocal()
    user = session.query(User).filter_by(id=user_id).first()
    session.close()
    if not user or user.role != "admin":
        return RedirectResponse(url="/")
    return FileResponse("../frontend/admin.html")


@app.get("/api/users")
def list_users(admin: User = Depends(require_role("admin"))):
    session = SessionLocal()
    users = session.query(User).order_by(User.created_at.asc()).all()
    projects = {p.id: p.name for p in session.query(Client).all()}
    out = [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "picture": u.picture,
            "role": u.role,
            "locked_project_id": u.locked_project_id,
            "locked_project_name": projects.get(u.locked_project_id),
            "has_api_key": bool(u.encrypted_anthropic_key),
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]
    session.close()
    return out


class RolePayload(BaseModel):
    role: str
    locked_project_id: Optional[int] = None


@app.post("/api/users/{user_id}/role")
def set_user_role(user_id: int, payload: RolePayload, admin: User = Depends(require_role("admin"))):
    if payload.role not in ("admin", "member", "viewer", "client"):
        raise HTTPException(400, "role must be admin, member, viewer, or client")
    if payload.role == "client" and not payload.locked_project_id:
        raise HTTPException(400, "a client role requires locked_project_id - which project they can see")
    session = SessionLocal()
    target = session.query(User).filter_by(id=user_id).first()
    if not target:
        session.close()
        raise HTTPException(404, "user not found")
    if target.role == "admin" and payload.role != "admin":
        admin_count = session.query(User).filter_by(role="admin").count()
        if admin_count <= 1:
            session.close()
            raise HTTPException(400, "Can't remove the platform's last admin")
    target.role = payload.role
    target.locked_project_id = payload.locked_project_id if payload.role == "client" else None
    session.commit()
    session.close()
    return {"ok": True}


@app.get("/api/modules")
def api_modules(request: Request, user: User = Depends(get_current_user)):
    session = SessionLocal()
    project = get_current_project(request, session)
    out = []
    for module_id, spec in MODULES.items():
        last_event = None
        if project:
            last_event = (
                session.query(Event)
                .filter_by(module_id=module_id, client_id=project.id)
                .order_by(Event.created_at.desc())
                .first()
            )
        out.append({
            **spec,
            "custom": False,
            "last_status": last_event.status if last_event else None,
            "last_run": last_event.created_at.isoformat() if last_event else None,
            "last_message": last_event.message if last_event else None,
        })

    # Custom, config-driven agents - which ones apply here now comes from
    # PromptEngineProject, a real many-to-many, not a single client_id
    # that could only mean "this one" or "literally every project".
    engines = []
    if project:
        engines = (
            session.query(PromptEngine)
            .join(PromptEngineProject, PromptEngineProject.engine_id == PromptEngine.id)
            .filter(PromptEngineProject.client_id == project.id)
            .all()
        )
    for engine in engines:
        last_event = None
        if project:
            last_event = (
                session.query(Event)
                .filter_by(module_id=engine.module_id, client_id=project.id)
                .order_by(Event.created_at.desc())
                .first()
            )
        out.append({
            "module_id": engine.module_id,
            "module_class": "custom",
            "display_name": engine.display_name,
            "reads": [t.strip() for t in (engine.reads or "").split(",") if t.strip()],
            "writes": ["EngineOutput"],
            "description": engine.description or "",
            "custom": True,
            "engine_id": engine.id,
            "last_status": last_event.status if last_event else None,
            "last_run": last_event.created_at.isoformat() if last_event else None,
            "last_message": last_event.message if last_event else None,
        })

    # Config-driven REST connectors - same idea as PromptEngine, applied
    # to connectors instead of engines.
    connectors_cfg = []
    if project:
        connectors_cfg = (
            session.query(RestConnector)
            .join(RestConnectorProject, RestConnectorProject.connector_id == RestConnector.id)
            .filter(RestConnectorProject.client_id == project.id)
            .all()
        )
    for rc in connectors_cfg:
        last_event = None
        if project:
            last_event = (
                session.query(Event)
                .filter_by(module_id=rc.module_id, client_id=project.id)
                .order_by(Event.created_at.desc())
                .first()
            )
        needs_credential = False
        oauth_provider_slug, oauth_provider_name = None, None
        if rc.auth_style == "header" and project:
            has_cred = session.query(RestConnectorCredential).filter_by(connector_id=rc.id, client_id=project.id).first()
            needs_credential = not has_cred
        elif rc.auth_style == "oauth_provider" and project:
            provider = session.query(OAuthProvider).filter_by(id=rc.oauth_provider_id).first()
            oauth_provider_slug = provider.slug if provider else None
            oauth_provider_name = provider.name if provider else None
            has_conn = session.query(OAuthConnection).filter_by(provider_id=rc.oauth_provider_id, client_id=project.id).first()
            needs_credential = not has_conn
        out.append({
            "module_id": rc.module_id,
            "module_class": "custom-connector",
            "display_name": rc.display_name,
            "reads": [],
            "writes": ["Meeting" if rc.content_type == "meeting" else "Document"],
            "description": rc.description or "",
            "oauth_provider_slug": oauth_provider_slug,
            "oauth_provider_name": oauth_provider_name,
            "custom": True,
            "connector_id": rc.id,
            "auth_style": rc.auth_style,
            "needs_credential": needs_credential,
            "last_status": last_event.status if last_event else None,
            "last_run": last_event.created_at.isoformat() if last_event else None,
            "last_message": last_event.message if last_event else None,
        })

    session.close()
    return out


class EnginePayload(BaseModel):
    display_name: str
    description: str = ""
    reads: str
    prompt_template: str
    project_ids: list[int] = []   # which projects this applies to - empty is valid (nothing to link to yet)


@app.get("/api/engines")
def list_engines(user: User = Depends(get_current_user)):
    session = SessionLocal()
    engines = session.query(PromptEngine).order_by(PromptEngine.created_at.desc()).all()
    out = []
    for e in engines:
        links = session.query(PromptEngineProject).filter_by(engine_id=e.id).all()
        out.append({
            "id": e.id, "module_id": e.module_id, "display_name": e.display_name,
            "description": e.description, "reads": e.reads, "prompt_template": e.prompt_template,
            "project_ids": [l.client_id for l in links],
        })
    session.close()
    return out


@app.post("/api/engines")
def create_engine(payload: EnginePayload, request: Request, user: User = Depends(require_role("admin", "member"))):
    name = payload.display_name.strip()
    if not name:
        raise HTTPException(400, "display_name is required")
    if not payload.prompt_template.strip():
        raise HTTPException(400, "prompt_template is required")
    valid_types = {"Meeting", "Document", "Decision", "ActionItem", "OpenQuestion", "Brief"}
    reads = [t.strip() for t in payload.reads.split(",") if t.strip()]
    if not reads or not all(t in valid_types for t in reads):
        raise HTTPException(400, f"reads must be a comma-separated list from: {', '.join(sorted(valid_types))}")

    session = SessionLocal()
    current_project = get_current_project(request, session)
    valid_project_ids = {c.id for c in session.query(Client.id).filter(Client.id.in_(payload.project_ids)).all()}
    if set(payload.project_ids) - valid_project_ids:
        session.close()
        raise HTTPException(400, "One or more selected projects don't exist")

    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
    module_id = f"custom-{slug}"
    suffix = 1
    while session.query(PromptEngine).filter_by(module_id=module_id).first():
        suffix += 1
        module_id = f"custom-{slug}-{suffix}"

    engine = PromptEngine(
        client_id=current_project.id if current_project else None,
        module_id=module_id,
        display_name=name,
        description=payload.description.strip(),
        reads=", ".join(reads),
        prompt_template=payload.prompt_template.strip(),
        created_by_id=user.id,
    )
    session.add(engine)
    session.commit()
    session.refresh(engine)
    for pid in valid_project_ids:
        session.add(PromptEngineProject(engine_id=engine.id, client_id=pid))
    session.commit()
    out = {"id": engine.id, "module_id": engine.module_id}
    session.close()
    return out


@app.post("/api/engines/{engine_id}/projects/{project_id}")
def link_engine_to_project(engine_id: int, project_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    if not session.query(PromptEngine).filter_by(id=engine_id).first() or not session.query(Client).filter_by(id=project_id).first():
        session.close()
        raise HTTPException(404, "agent or project not found")
    if not session.query(PromptEngineProject).filter_by(engine_id=engine_id, client_id=project_id).first():
        session.add(PromptEngineProject(engine_id=engine_id, client_id=project_id))
        session.commit()
    session.close()
    return {"ok": True}


@app.delete("/api/engines/{engine_id}")
def delete_engine(engine_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    engine = session.query(PromptEngine).filter_by(id=engine_id).first()
    if not engine:
        session.close()
        raise HTTPException(404, "engine not found")
    session.query(PromptEngineProject).filter_by(engine_id=engine_id).delete()
    session.delete(engine)
    session.commit()
    session.close()
    return {"ok": True}


class RestConnectorPayload(BaseModel):
    display_name: str
    description: str = ""
    search_url_template: str
    auth_style: str = "header"   # "header" | "google_oauth" | "oauth_provider"
    auth_header_name: str = "Authorization"
    auth_value_prefix: str = ""
    oauth_provider_id: Optional[int] = None
    results_path: str
    field_id: str = ""
    field_title: str = ""
    field_content: str = ""
    field_url: str = ""
    field_date: str = ""
    content_type: str = "document"   # "document" | "meeting" - which table synced records land in
    project_ids: list[int] = []   # which projects this applies to - empty is valid (nothing to link to yet)


@app.get("/api/rest-connectors")
def list_rest_connectors(user: User = Depends(get_current_user)):
    session = SessionLocal()
    rows = session.query(RestConnector).order_by(RestConnector.created_at.desc()).all()
    out = []
    for r in rows:
        links = session.query(RestConnectorProject).filter_by(connector_id=r.id).all()
        out.append({
            "id": r.id, "module_id": r.module_id, "display_name": r.display_name, "description": r.description,
            "search_url_template": r.search_url_template, "auth_style": r.auth_style,
            "results_path": r.results_path, "field_id": r.field_id, "field_title": r.field_title,
            "field_content": r.field_content, "field_url": r.field_url, "field_date": r.field_date,
            "content_type": r.content_type or "document",
            "project_ids": [l.client_id for l in links],
        })
    session.close()
    return out


@app.post("/api/rest-connectors")
def create_rest_connector(payload: RestConnectorPayload, request: Request, user: User = Depends(require_role("admin", "member"))):
    name = payload.display_name.strip()
    if not name:
        raise HTTPException(400, "display_name is required")
    if payload.auth_style not in ("header", "google_oauth", "oauth_provider"):
        raise HTTPException(400, "auth_style must be 'header', 'google_oauth', or 'oauth_provider'")
    if payload.auth_style == "oauth_provider" and not payload.oauth_provider_id:
        raise HTTPException(400, "oauth_provider_id is required when auth_style is 'oauth_provider'")
    if payload.content_type not in ("document", "meeting"):
        raise HTTPException(400, "content_type must be 'document' or 'meeting'")
    if "{query}" not in payload.search_url_template and "{domain}" not in payload.search_url_template:
        raise HTTPException(400, "search_url_template should include {query} and/or {domain} - otherwise every project would fetch the same thing")
    if not payload.results_path:
        raise HTTPException(400, "results_path is required - the JSON path to the list of results in the response")

    session = SessionLocal()
    current_project = get_current_project(request, session)
    valid_project_ids = {c.id for c in session.query(Client.id).filter(Client.id.in_(payload.project_ids)).all()}
    if set(payload.project_ids) - valid_project_ids:
        session.close()
        raise HTTPException(400, "One or more selected projects don't exist")

    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "connector"
    module_id = f"conn-{slug}"
    suffix = 1
    while session.query(RestConnector).filter_by(module_id=module_id).first():
        suffix += 1
        module_id = f"conn-{slug}-{suffix}"

    rc = RestConnector(
        client_id=current_project.id if current_project else None,
        module_id=module_id,
        display_name=name,
        description=payload.description.strip(),
        search_url_template=payload.search_url_template.strip(),
        auth_style=payload.auth_style,
        auth_header_name=payload.auth_header_name.strip() or "Authorization",
        auth_value_prefix=payload.auth_value_prefix,
        oauth_provider_id=payload.oauth_provider_id if payload.auth_style == "oauth_provider" else None,
        results_path=payload.results_path.strip(),
        field_id=payload.field_id.strip(),
        field_title=payload.field_title.strip(),
        field_content=payload.field_content.strip(),
        field_url=payload.field_url.strip(),
        field_date=payload.field_date.strip(),
        content_type=payload.content_type,
        created_by_id=user.id,
    )
    session.add(rc)
    session.commit()
    session.refresh(rc)
    for pid in valid_project_ids:
        session.add(RestConnectorProject(connector_id=rc.id, client_id=pid))
    session.commit()
    out = {"id": rc.id, "module_id": rc.module_id}
    session.close()
    return out


@app.post("/api/rest-connectors/{connector_id}/projects/{project_id}")
def link_connector_to_project(connector_id: int, project_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    if not session.query(RestConnector).filter_by(id=connector_id).first() or not session.query(Client).filter_by(id=project_id).first():
        session.close()
        raise HTTPException(404, "connector or project not found")
    if not session.query(RestConnectorProject).filter_by(connector_id=connector_id, client_id=project_id).first():
        session.add(RestConnectorProject(connector_id=connector_id, client_id=project_id))
        session.commit()
    session.close()
    return {"ok": True}


@app.delete("/api/rest-connectors/{connector_id}")
def delete_rest_connector(connector_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    rc = session.query(RestConnector).filter_by(id=connector_id).first()
    if not rc:
        session.close()
        raise HTTPException(404, "connector not found")
    session.query(RestConnectorCredential).filter_by(connector_id=connector_id).delete()
    session.query(RestConnectorProject).filter_by(connector_id=connector_id).delete()
    session.delete(rc)
    session.commit()
    session.close()
    return {"ok": True}


@app.post("/api/rest-connectors/{connector_id}/credential")
def set_rest_connector_credential(connector_id: int, payload: CredentialPayload, request: Request, user: User = Depends(require_role("admin", "member"))):
    value = payload.value.strip()
    if not value:
        raise HTTPException(400, "value is required")
    session = SessionLocal()
    project = get_current_project(request, session)
    if not project:
        session.close()
        raise HTTPException(400, "No project selected")
    require_project_admin(user, project.id, session)
    existing = session.query(RestConnectorCredential).filter_by(connector_id=connector_id, client_id=project.id).first()
    if existing:
        existing.encrypted_value = encrypt(value)
    else:
        session.add(RestConnectorCredential(connector_id=connector_id, client_id=project.id, encrypted_value=encrypt(value)))
    session.commit()
    session.close()
    return {"ok": True}


@app.delete("/api/rest-connectors/{connector_id}/credential")
def clear_rest_connector_credential(connector_id: int, request: Request, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    project = get_current_project(request, session)
    if not project:
        session.close()
        raise HTTPException(400, "No project selected")
    require_project_admin(user, project.id, session)
    session.query(RestConnectorCredential).filter_by(connector_id=connector_id, client_id=project.id).delete()
    session.commit()
    session.close()
    return {"ok": True}


# ---------- Tier B: generic OAuth2 providers ----------

class OAuthProviderPayload(BaseModel):
    name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str
    scopes: str = ""


@app.get("/api/oauth-providers")
def list_oauth_providers(user: User = Depends(get_current_user)):
    session = SessionLocal()
    rows = session.query(OAuthProvider).order_by(OAuthProvider.name).all()
    out = [{"id": p.id, "name": p.name, "slug": p.slug, "scopes": p.scopes} for p in rows]
    session.close()
    return out


@app.post("/api/oauth-providers")
def create_oauth_provider(payload: OAuthProviderPayload, user: User = Depends(require_role("admin"))):
    name = payload.name.strip()
    if not name or not payload.authorize_url.strip() or not payload.token_url.strip() or not payload.client_id.strip() or not payload.client_secret.strip():
        raise HTTPException(400, "name, authorize_url, token_url, client_id, and client_secret are all required")

    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "provider"
    session = SessionLocal()
    suffix = 1
    base_slug = slug
    while session.query(OAuthProvider).filter_by(slug=slug).first():
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    provider = OAuthProvider(
        name=name,
        slug=slug,
        authorize_url=payload.authorize_url.strip(),
        token_url=payload.token_url.strip(),
        client_id=payload.client_id.strip(),
        encrypted_client_secret=encrypt(payload.client_secret.strip()),
        scopes=payload.scopes.strip(),
        created_by_id=user.id,
    )
    session.add(provider)
    session.commit()
    session.refresh(provider)
    out = {"id": provider.id, "slug": provider.slug}
    session.close()
    return out


@app.delete("/api/oauth-providers/{provider_id}")
def delete_oauth_provider(provider_id: int, user: User = Depends(require_role("admin"))):
    session = SessionLocal()
    provider = session.query(OAuthProvider).filter_by(id=provider_id).first()
    if not provider:
        session.close()
        raise HTTPException(404, "provider not found")
    session.query(OAuthConnection).filter_by(provider_id=provider_id).delete()
    session.delete(provider)
    session.commit()
    session.close()
    return {"ok": True}


@app.get("/api/oauth-connections")
def list_oauth_connections(request: Request, user: User = Depends(get_current_user)):
    session = SessionLocal()
    project = get_current_project(request, session)
    if not project:
        session.close()
        return []
    rows = session.query(OAuthConnection).filter_by(client_id=project.id).all()
    out = [{"id": c.id, "provider_id": c.provider_id, "connected_label": c.connected_label} for c in rows]
    session.close()
    return out


@app.delete("/api/oauth-connections/{connection_id}")
def delete_oauth_connection(connection_id: int, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    session.query(OAuthConnection).filter_by(id=connection_id).delete()
    session.commit()
    session.close()
    return {"ok": True}


@app.get("/oauth/{slug}/connect/{project_id}")
def oauth_provider_connect(slug: str, project_id: int, request: Request, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    provider = session.query(OAuthProvider).filter_by(slug=slug).first()
    project = session.query(Client).filter_by(id=project_id).first()
    session.close()
    if not provider:
        raise HTTPException(404, "OAuth provider not found")
    if not project:
        raise HTTPException(404, "project not found")

    import secrets
    state = secrets.token_urlsafe(24)
    # Stashed server-side, same reasoning as the Drive connect flow -
    # this can't be redirected to attach to a project the person
    # clicking wasn't actually authorized to modify, and the state
    # value guards against a forged callback.
    request.session["oauth_state"] = state
    request.session["oauth_provider_id"] = provider.id
    request.session["oauth_project_id"] = project_id

    redirect_uri = str(request.url_for("oauth_provider_callback"))
    return RedirectResponse(url=oauth_generic.build_authorize_url(provider, redirect_uri, state))


@app.get("/oauth/callback", name="oauth_provider_callback")
def oauth_provider_callback(request: Request, code: str = None, state: str = None, error: str = None):
    expected_state = request.session.get("oauth_state")
    provider_id = request.session.get("oauth_provider_id")
    project_id = request.session.get("oauth_project_id")
    if error or not code or not state or state != expected_state or not provider_id or not project_id:
        return RedirectResponse(url="/?error=oauth_connect_failed")

    session = SessionLocal()
    provider = session.query(OAuthProvider).filter_by(id=provider_id).first()
    if not provider:
        session.close()
        return RedirectResponse(url="/?error=oauth_connect_failed")

    redirect_uri = str(request.url_for("oauth_provider_callback"))
    try:
        access_token, refresh_token, expires_in = oauth_generic.exchange_code(provider, code, redirect_uri)
    except Exception:
        session.close()
        return RedirectResponse(url="/?error=oauth_connect_failed")

    connection = session.query(OAuthConnection).filter_by(provider_id=provider_id, client_id=project_id).first()
    if not connection:
        connection = OAuthConnection(provider_id=provider_id, client_id=project_id)
        session.add(connection)
    connection.connected_label = provider.name
    connection.encrypted_access_token = encrypt(access_token)
    if refresh_token:
        connection.encrypted_refresh_token = encrypt(refresh_token)
    if expires_in:
        connection.token_expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
    session.commit()
    session.close()

    request.session.pop("oauth_state", None)
    request.session.pop("oauth_provider_id", None)
    request.session.pop("oauth_project_id", None)
    return RedirectResponse(url="/")


NEEDS_USER_KEY = {"extraction-engine", "brief-generator", "contradiction-detector"}
CONTENT_CONNECTOR_MODULES = {"fireflies-connector", "drive-connector", "gmail-connector"}

# How often the background loop below checks every project for new content -
# minutes, not seconds, since this is a poll, not a push subscription (no
# webhook receivers for Fireflies/Drive/Gmail here). 15 is a reasonable
# default for "shows up automatically without being instant"; override with
# AUTO_SYNC_INTERVAL_MINUTES in .env if a project needs it tighter or looser.
AUTO_SYNC_INTERVAL_MINUTES = int(os.getenv("AUTO_SYNC_INTERVAL_MINUTES", "15"))


def _project_has_drive_credential(client):
    return bool(client.encrypted_drive_refresh_token or client.encrypted_drive_credentials)


def run_auto_sync_for_project(session, client):
    """The unattended version of clicking RUN on every connected connector,
    then Extract & store, then the brief - run on a timer for every project,
    not triggered by a person. Deliberately skips any connector that has no
    real credential/connection rather than falling back to demo mode the way
    a manual RUN does - silently seeding a real project with fake sample
    data on a schedule would be actively harmful, not just unhelpful."""
    synced_any = False

    if client.encrypted_fireflies_key:
        try:
            if connectors.run_fireflies(session, client).get("synced", 0) > 0:
                synced_any = True
        except Exception as e:
            print(f"[auto-sync] fireflies failed for project {client.id}: {e}", flush=True)

    if _project_has_drive_credential(client):
        try:
            if connectors.run_drive(session, client).get("synced", 0) > 0:
                synced_any = True
        except Exception as e:
            print(f"[auto-sync] drive failed for project {client.id}: {e}", flush=True)
        try:
            if email_connector.run_gmail(session, client).get("synced", 0) > 0:
                synced_any = True
        except Exception as e:
            print(f"[auto-sync] gmail failed for project {client.id}: {e}", flush=True)

    rest_conns = (
        session.query(RestConnector)
        .join(RestConnectorProject, RestConnectorProject.connector_id == RestConnector.id)
        .filter(RestConnectorProject.client_id == client.id)
        .all()
    )
    for rc in rest_conns:
        if rc.auth_style == "header":
            has_cred = session.query(RestConnectorCredential).filter_by(connector_id=rc.id, client_id=client.id).first() is not None
        elif rc.auth_style == "google_oauth":
            has_cred = _project_has_drive_credential(client)
        else:  # oauth_provider
            has_cred = session.query(OAuthConnection).filter_by(provider_id=rc.oauth_provider_id, client_id=client.id).first() is not None
        if not has_cred:
            continue
        try:
            if rest_connector.run_rest_connector(session, client, rc).get("synced", 0) > 0:
                synced_any = True
        except Exception as e:
            print(f"[auto-sync] connector '{rc.display_name}' failed for project {client.id}: {e}", flush=True)

    llm_config = get_automation_llm_config(client, session)
    extracted_new = False
    if llm_config:
        # Extraction runs every cycle, not just ones that synced something
        # new - it already skips any record that doesn't need summarizing,
        # so this is what catches up a project's backlog (records synced
        # before a key was configured, or before this existed) within one
        # tick instead of leaving them stuck until something new happens
        # to sync again.
        try:
            extraction_result = extraction.run_extraction(session, client, llm_config=llm_config)
            extracted_new = isinstance(extraction_result, dict) and sum(extraction_result.values()) > 0
        except Exception as e:
            print(f"[auto-sync] extraction failed for project {client.id}: {e}", flush=True)

    if synced_any or extracted_new:
        # Contradiction detection deliberately isn't part of this
        # automatic chain - unlike extraction/brief, nobody asked for
        # it to run unattended; contradiction.py is untouched and
        # still reachable manually if it's ever wired back into the
        # module list.
        # The overall project summary is compiled by a plain backend
        # algorithm (see brief.py), not a second LLM call, so it always
        # runs here regardless of whether an AI provider key is
        # configured - there's nothing for it to be missing.
        try:
            brief_module.generate_brief(session, client)
        except Exception as e:
            print(f"[auto-sync] brief failed for project {client.id}: {e}", flush=True)

    return synced_any


async def _auto_sync_loop():
    """Runs for the lifetime of the process. A single asyncio task, not a
    separate worker/queue - fine at this app's scale (one web process), but
    means running two instances of this service would double up the work,
    not distribute it. Each project's sync is pushed to a thread since
    connectors.run_* and friends are synchronous/blocking (requests, DB
    calls) - running them directly on the event loop would stall every
    incoming HTTP request for the duration of each project's sync."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(AUTO_SYNC_INTERVAL_MINUTES * 60)
        session = SessionLocal()
        try:
            for client in session.query(Client).all():
                try:
                    await loop.run_in_executor(None, run_auto_sync_for_project, session, client)
                except Exception as e:
                    print(f"[auto-sync] project {client.id} failed: {e}", flush=True)
        finally:
            session.close()


@app.post("/api/run/{module_id}")
def api_run(module_id: str, request: Request, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    client = get_current_project(request, session)
    if not client:
        session.close()
        return JSONResponse({"error": "No project selected - create or select one first"}, status_code=400)

    engine = None
    rest_conn = None
    if module_id not in NEEDS_USER_KEY and module_id not in RUNNERS:
        engine = session.query(PromptEngine).filter(
            PromptEngine.module_id == module_id,
            (PromptEngine.client_id == None) | (PromptEngine.client_id == client.id),
        ).first()
        if not engine:
            rest_conn = session.query(RestConnector).filter(
                RestConnector.module_id == module_id,
                (RestConnector.client_id == None) | (RestConnector.client_id == client.id),
            ).first()
        if not engine and not rest_conn:
            session.close()
            return JSONResponse({"error": f"unknown module '{module_id}'"}, status_code=404)

    try:
        if module_id in NEEDS_USER_KEY:
            llm_config = get_user_llm_config(user)
            if module_id == "extraction-engine":
                result = extraction.run_extraction(session, client, llm_config=llm_config)
            elif module_id == "brief-generator":
                result = brief_module.generate_brief(session, client)
            else:
                result = contradiction.run_contradiction_check(session, client, llm_config=llm_config)
        elif engine:
            llm_config = get_user_llm_config(user)
            result = custom_engine.run_custom_engine(session, client, engine, llm_config=llm_config)
        elif rest_conn:
            result = rest_connector.run_rest_connector(session, client, rest_conn)
        else:
            result = RUNNERS[module_id](session, client)

        # Keep each record's own summary, and the project's overall brief,
        # current automatically after any connector run - not just ones
        # that happened to sync something new. run_extraction() already
        # skips any record that doesn't need summarizing, so calling it
        # unconditionally is cheap when there's nothing to do and is what
        # actually catches up a project's backlog (records that synced
        # before a key was configured, or before this auto-chain existed)
        # instead of leaving it stuck on "no summary yet - run the
        # extraction engine" until someone remembers to click RUN there.
        is_connector_run = module_id in CONTENT_CONNECTOR_MODULES or rest_conn is not None
        if is_connector_run:
            auto_llm_config = get_user_llm_config(user)
            extracted_new = False
            try:
                extraction_result = extraction.run_extraction(session, client, llm_config=auto_llm_config)
                extracted_new = isinstance(extraction_result, dict) and sum(extraction_result.values()) > 0
            except Exception:
                pass  # a failed extraction shouldn't fail the sync that triggered it

            wrote_new_content = isinstance(result, dict) and result.get("synced", 0) > 0
            if wrote_new_content or extracted_new:
                try:
                    brief_module.generate_brief(session, client)
                except Exception:
                    pass  # a failed summary refresh shouldn't fail the sync that triggered it

        return {"module_id": module_id, "result": result}
    except Exception as e:
        return {"module_id": module_id, "error": str(e)}
    finally:
        session.close()


@app.get("/api/events")
def api_events(request: Request, limit: int = 30, user: User = Depends(get_current_user)):
    session = SessionLocal()
    project = get_current_project(request, session)
    if not project:
        session.close()
        return []
    events = session.query(Event).filter_by(client_id=project.id).order_by(Event.created_at.desc()).limit(limit).all()
    out = [
        {"module_id": e.module_id, "status": e.status, "message": e.message, "created_at": e.created_at.isoformat()}
        for e in events
    ]
    session.close()
    return out


@app.get("/api/records/{record_type}")
def api_records(record_type: str, request: Request, user: User = Depends(get_current_user)):
    session = SessionLocal()
    client = get_current_project(request, session)
    if not client:
        session.close()
        return []
    model_map = {"meetings": Meeting, "documents": Document, "decisions": Decision, "action_items": ActionItem, "open_questions": OpenQuestion, "briefs": Brief, "contradictions": Contradiction, "engine_outputs": EngineOutput}
    model = model_map.get(record_type)
    if not model:
        session.close()
        return JSONResponse({"error": f"unknown record type '{record_type}'"}, status_code=404)
    rows = session.query(model).filter_by(client_id=client.id).order_by(model.id.desc()).all()
    out = [_serialize(r) for r in rows]
    session.close()
    return out


# source_type strings as stored on Decision/ActionItem rows, keyed by
# the /api/records/{record_type} URL segment that refers to that table.
_SOURCE_TYPE_LABEL = {"meetings": "Meeting", "documents": "Document"}


@app.delete("/api/records/{record_type}/{record_id}")
def delete_record(record_type: str, record_id: int, request: Request, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    client = get_current_project(request, session)
    if not client:
        session.close()
        raise HTTPException(400, "No project selected")
    model_map = {"meetings": Meeting, "documents": Document, "decisions": Decision, "action_items": ActionItem, "open_questions": OpenQuestion, "briefs": Brief, "contradictions": Contradiction, "engine_outputs": EngineOutput}
    model = model_map.get(record_type)
    if not model:
        session.close()
        raise HTTPException(404, f"unknown record type '{record_type}'")
    row = session.query(model).filter_by(id=record_id, client_id=client.id).first()
    if not row:
        session.close()
        raise HTTPException(404, "record not found")

    # Deleting a meeting or document also removes any decisions/action
    # items that were extracted from it - otherwise they'd point at a
    # source that no longer exists.
    cascaded = 0
    source_label = _SOURCE_TYPE_LABEL.get(record_type)
    if source_label:
        for related_model in (Decision, ActionItem, OpenQuestion):
            related = session.query(related_model).filter_by(
                client_id=client.id, source_type=source_label, source_id=record_id
            ).all()
            for r in related:
                session.delete(r)
                cascaded += 1

    session.delete(row)
    session.commit()
    session.close()
    return {"ok": True, "cascaded": cascaded}


class BulkDeletePayload(BaseModel):
    ids: List[int]


@app.post("/api/records/{record_type}/bulk-delete")
def bulk_delete_records(record_type: str, payload: BulkDeletePayload, request: Request, user: User = Depends(require_role("admin", "member"))):
    session = SessionLocal()
    client = get_current_project(request, session)
    if not client:
        session.close()
        raise HTTPException(400, "No project selected")
    model_map = {"meetings": Meeting, "documents": Document, "decisions": Decision, "action_items": ActionItem, "open_questions": OpenQuestion, "briefs": Brief, "contradictions": Contradiction, "engine_outputs": EngineOutput}
    model = model_map.get(record_type)
    if not model:
        session.close()
        raise HTTPException(404, f"unknown record type '{record_type}'")

    source_label = _SOURCE_TYPE_LABEL.get(record_type)
    deleted, cascaded = 0, 0
    for record_id in payload.ids:
        row = session.query(model).filter_by(id=record_id, client_id=client.id).first()
        if not row:
            continue
        if source_label:
            for related_model in (Decision, ActionItem, OpenQuestion):
                related = session.query(related_model).filter_by(
                    client_id=client.id, source_type=source_label, source_id=record_id
                ).all()
                for r in related:
                    session.delete(r)
                    cascaded += 1
        session.delete(row)
        deleted += 1

    session.commit()
    session.close()
    return {"ok": True, "deleted": deleted, "cascaded": cascaded}


@app.get("/api/query")
def api_query(q: str, request: Request, user: User = Depends(get_current_user)):
    session = SessionLocal()
    client = get_current_project(request, session)
    if not client:
        session.close()
        return {"meetings": [], "documents": [], "decisions": [], "action_items": []}
    like = f"%{q}%"
    results = {
        "meetings": [
            _serialize(m) for m in session.query(Meeting).filter(
                Meeting.client_id == client.id,
                or_(Meeting.title.ilike(like), Meeting.transcript.ilike(like), Meeting.summary.ilike(like)),
            ).all()
        ],
        "documents": [
            _serialize(d) for d in session.query(Document).filter(
                Document.client_id == client.id,
                or_(Document.title.ilike(like), Document.content.ilike(like)),
            ).all()
        ],
        "decisions": [
            _serialize(d) for d in session.query(Decision).filter(
                Decision.client_id == client.id, Decision.description.ilike(like)
            ).all()
        ],
        "action_items": [
            _serialize(a) for a in session.query(ActionItem).filter(
                ActionItem.client_id == client.id, ActionItem.description.ilike(like)
            ).all()
        ],
    }
    session.close()
    return results


class AskPayload(BaseModel):
    question: str


@app.post("/api/ask")
def api_ask(payload: AskPayload, request: Request, user: User = Depends(get_current_user)):
    question = payload.question.strip()
    if not question:
        raise HTTPException(400, "question is required")
    session = SessionLocal()
    client = get_current_project(request, session)
    if not client:
        session.close()
        return {"answer": "No project selected.", "sources": []}
    llm_config = get_user_llm_config(user)
    result = qa.ask(session, client, question, llm_config=llm_config)
    session.close()
    return result


@app.get("/repository")
def repository_page(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse(url="/login")
    return FileResponse("../frontend/repository.html")
