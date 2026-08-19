"""
Core artifact model, scaled down from the full infra spec to what
Phase 0-4 of the client memory agent actually need: Client, Meeting,
Document, Decision, ActionItem, and an Event log that stands in for
the event bus / audit log described in the architecture doc.
"""
import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class User(Base):
    """A person who has signed in with Google. Each user picks their
    own LLM provider (Anthropic, OpenAI, Gemini, or a custom
    OpenAI-compatible endpoint) and holds their own key per provider,
    encrypted and only ever decrypted for that same user's own runs -
    see crypto.py and llm_client.py."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    google_sub = Column(String, unique=True, nullable=False)   # Google's stable user ID
    email = Column(String, unique=True, nullable=False)
    name = Column(String)
    picture = Column(String)
    encrypted_anthropic_key = Column(Text, nullable=True)
    encrypted_openai_key = Column(Text, nullable=True)
    encrypted_gemini_key = Column(Text, nullable=True)
    encrypted_custom_key = Column(Text, nullable=True)
    custom_provider_name = Column(String)     # display name for the "custom" option, e.g. "Groq"
    custom_endpoint_url = Column(String)      # an OpenAI-compatible /chat/completions URL
    preferred_provider = Column(String, default="anthropic")   # "anthropic" | "openai" | "gemini" | "custom"
    preferred_model = Column(String, default="claude-sonnet-4-6")
    role = Column(String, default="member")   # "admin" | "member" | "viewer" | "client"
    locked_project_id = Column(Integer, ForeignKey("clients.id"), nullable=True)   # for role="client" - the one project they can ever see
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Client(Base):
    """Represents a team/project. Called Client in the schema for
    historical reasons, but functions as a 'team': a name and a client
    email domain drive matching now - a Fireflies meeting or Drive
    file matches if its title/filename contains the project name, or
    if a participant/collaborator's email is on the project's domain."""
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    keywords = Column(Text)              # deprecated - no longer used for matching, kept to avoid a destructive migration
    team_emails = Column(Text)           # comma-separated - a Fireflies meeting matches if any of these attended
    domain = Column(String)              # the project's client email domain, e.g. "actionhospital.in"
    drive_folder_id = Column(String)     # optional - narrows the Drive scan to one folder if set
    encrypted_fireflies_key = Column(Text)
    encrypted_drive_credentials = Column(Text)   # advanced path: service-account JSON, encrypted whole
    drive_oauth_email = Column(String)                    # which Google account authorized Drive access
    encrypted_drive_access_token = Column(Text)
    encrypted_drive_refresh_token = Column(Text)
    drive_token_expiry = Column(DateTime)
    encrypted_resend_key = Column(Text)   # for the digest-notifier module
    notify_email = Column(String)         # who receives the digest
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    source = Column(String, default="fireflies")
    external_id = Column(String)
    source_url = Column(String)          # link back to the meeting in Fireflies
    title = Column(String)
    occurred_at = Column(DateTime)
    summary = Column(Text)                # raw summary as pulled from Fireflies
    synthesized_summary = Column(Text)    # Claude-generated, consistent style with Document summaries
    transcript = Column(Text)
    participants = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    source = Column(String, default="google_drive")
    external_id = Column(String)
    source_url = Column(String)          # Drive's webViewLink - opens the file directly
    title = Column(String)
    content = Column(Text)
    synthesized_summary = Column(Text)
    modified_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    description = Column(Text)
    source_type = Column(String)   # "Meeting" or "Document"
    source_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ActionItem(Base):
    __tablename__ = "action_items"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    description = Column(Text)
    owner = Column(String)
    due_date = Column(String)
    status = Column(String, default="open")
    source_type = Column(String)
    source_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Brief(Base):
    """A generated status summary - the whole engagement's state in one
    document, written by Claude from everything else already stored."""
    __tablename__ = "briefs"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class OpenQuestion(Base):
    """Something raised but not yet resolved - deliberately the
    opposite of what Decision captures. Extraction excludes these from
    decisions on purpose; this is where they go instead of being
    discarded."""
    __tablename__ = "open_questions"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    description = Column(Text)
    status = Column(String, default="open")   # "open" | "resolved"
    source_type = Column(String)
    source_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Contradiction(Base):
    """Two decisions on record that appear to disagree. Regenerated
    fresh on every run rather than accumulated - see contradiction.py."""
    __tablename__ = "contradictions"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    description = Column(Text)
    decision_a_id = Column(Integer)
    decision_b_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class PromptEngine(Base):
    """A config-driven agent - no Python file, no redeploy. Defined by
    which artifact types it reads and a prompt template; run generically
    by custom_engine.py the same way every built-in engine works, just
    without a dedicated runner function. Which projects it applies to
    is tracked in PromptEngineProject, not a single client_id - an
    agent can now apply to any specific subset of projects, not just
    "this one" or "literally every one, forever". client_id here is
    kept only as a record of which project (if any) it was created
    from; it no longer controls where the agent actually runs."""
    __tablename__ = "prompt_engines"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    module_id = Column(String, unique=True)          # slug used for dispatch, e.g. "risk-tracker"
    display_name = Column(String)
    description = Column(Text)
    reads = Column(Text)                              # comma-separated: "Meeting,Document,Decision" - optional, blank means no records step
    prompt_template = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    trigger_type = Column(String, default="manual")             # "manual" | "schedule"
    schedule_interval_minutes = Column(Integer, nullable=True)   # only meaningful when trigger_type == "schedule"
    next_run_at = Column(DateTime, nullable=True)                # only meaningful when trigger_type == "schedule"
    action_connector_id = Column(Integer, ForeignKey("action_connectors.id"), nullable=True)


class PromptEngineProject(Base):
    """Which projects a given agent actually applies to - a real
    many-to-many, so "every project" is just "linked to every project
    that currently exists", not a separate global flag. This is also
    what makes the "apply this to my new project too?" prompt possible:
    creating a project doesn't automatically add rows here for existing
    agents, a person has to say yes."""
    __tablename__ = "prompt_engine_projects"
    id = Column(Integer, primary_key=True)
    engine_id = Column(Integer, ForeignKey("prompt_engines.id"))
    client_id = Column(Integer, ForeignKey("clients.id"))


class EngineOutput(Base):
    """What a custom PromptEngine produces on a run. Generic on purpose
    - every custom agent's results land here regardless of what the
    agent is actually for, tagged by which engine produced them."""
    __tablename__ = "engine_outputs"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    engine_id = Column(Integer, ForeignKey("prompt_engines.id"))
    engine_name = Column(String)   # denormalized so old outputs still show a name if the engine is later deleted
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class RestConnector(Base):
    """A config-driven connector - the SHAPE of how to talk to an
    external REST API, not the credential itself (that's
    RestConnectorCredential, kept separate so a connector definition
    used by several projects can still take a different key per
    project, same as Fireflies/Resend).

    Which projects a connector actually runs for is tracked in
    RestConnectorProject, a real many-to-many - see PromptEngineProject
    for why this replaced a single nullable client_id.

    auth_style is "header" (a static API key/token in a request
    header - Slack bot tokens, most SaaS API keys) or "google_oauth"
    (reuses the project's already-connected Google account rather than
    a separate credential - this is what makes Calendar, and anything
    else reachable with that same token, addable with zero new code).

    results_path and the field_* columns are dotted JSON paths - e.g.
    results_path="items" pulls response["items"]; field_title="summary"
    pulls item["summary"] from each entry in that list."""
    __tablename__ = "rest_connectors"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)   # which project it was created from, if any - doesn't control where it runs
    module_id = Column(String, unique=True)
    display_name = Column(String)
    description = Column(Text)
    search_url_template = Column(Text)     # {query} -> project name, {domain} -> client domain, both URL-encoded
    auth_style = Column(String, default="header")
    auth_header_name = Column(String, default="Authorization")
    auth_value_prefix = Column(String, default="")   # e.g. "Bearer " prepended to the credential value
    oauth_provider_id = Column(Integer, ForeignKey("oauth_providers.id"), nullable=True)   # set when auth_style == "oauth_provider"
    results_path = Column(String)
    field_id = Column(String)
    field_title = Column(String)
    field_content = Column(String)
    field_url = Column(String)
    field_date = Column(String)
    content_type = Column(String, default="document")   # "document" | "meeting" - which table synced records land in
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class RestConnectorProject(Base):
    """Which projects a given connector actually runs for - see
    PromptEngineProject, same reasoning, applied to connectors."""
    __tablename__ = "rest_connector_projects"
    id = Column(Integer, primary_key=True)
    connector_id = Column(Integer, ForeignKey("rest_connectors.id"))
    client_id = Column(Integer, ForeignKey("clients.id"))


class RestConnectorCredential(Base):
    """The actual per-project secret for a header-auth RestConnector.
    Only used when auth_style == 'header' - google_oauth connectors
    have nothing here, they reuse the Drive/Gmail connection."""
    __tablename__ = "rest_connector_credentials"
    id = Column(Integer, primary_key=True)
    connector_id = Column(Integer, ForeignKey("rest_connectors.id"))
    client_id = Column(Integer, ForeignKey("clients.id"))
    encrypted_value = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class RestConnectorResource(Base):
    """Which specific upstream resources (currently: GitHub repos) an
    oauth_provider connector should scope its search to, for a given
    project - a real per-(connector, project) table rather than a
    serialized list, same reasoning as RestConnectorProject. No rows
    for a given (connector_id, client_id) means "no restriction" -
    search everything the connected account can see, which is the
    behavior every connector had before this table existed, so it
    stays the default rather than something that has to be opted out
    of."""
    __tablename__ = "rest_connector_resources"
    id = Column(Integer, primary_key=True)
    connector_id = Column(Integer, ForeignKey("rest_connectors.id"))
    client_id = Column(Integer, ForeignKey("clients.id"))
    resource_id = Column(String)      # e.g. a GitHub repo's "owner/name" full_name
    resource_label = Column(String)   # display label - same as resource_id for GitHub


class ActionConnector(Base):
    """The outbound sibling of RestConnector - the SHAPE of how to POST
    an agent's generated output to a third-party REST API (a Slack
    incoming webhook, a generic API endpoint), not the credential
    itself (ActionConnectorCredential, kept separate for the same
    reason RestConnectorCredential is - one connector definition used
    by several projects can still take a different key per project).

    Which projects a connector actually runs for is tracked in
    ActionConnectorProject, a real many-to-many - see
    PromptEngineProject for why.

    Unlike RestConnector there is no content_type/results_path/field_*
    - nothing is parsed back out of the response; the response is only
    ever logged (see action_connector.py), never stored as a record.
    url_template deliberately isn't named search_url_template - it
    isn't a search, it's usually a fixed target (a single webhook URL),
    though {query}/{domain} substitution is still supported for parity
    with RestConnector and for the rare case a target genuinely varies
    by project.

    body_template is a JSON template string - e.g. '{"text": "{content}"}'
    - with {content} (the agent's generated text output), plus
    {project_name}, {date}, and {engine_name} available the same way
    {query}/{domain} are available in url_template. Each value is
    JSON-string-escaped before substitution so the result is always
    valid JSON, even if the generated content itself contains quotes
    or newlines - see action_connector.py's render_body."""
    __tablename__ = "action_connectors"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)   # which project it was created from, if any - doesn't control where it runs
    module_id = Column(String, unique=True)
    display_name = Column(String)
    description = Column(Text)
    url_template = Column(Text)
    http_method = Column(String, default="POST")     # "POST" | "PUT"
    auth_style = Column(String, default="header")     # "header" | "google_oauth" | "oauth_provider"
    auth_header_name = Column(String, default="Authorization")
    auth_value_prefix = Column(String, default="")
    oauth_provider_id = Column(Integer, ForeignKey("oauth_providers.id"), nullable=True)
    body_template = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ActionConnectorProject(Base):
    """Which projects a given action connector actually runs for - see
    PromptEngineProject/RestConnectorProject, same reasoning."""
    __tablename__ = "action_connector_projects"
    id = Column(Integer, primary_key=True)
    connector_id = Column(Integer, ForeignKey("action_connectors.id"))
    client_id = Column(Integer, ForeignKey("clients.id"))


class ActionConnectorCredential(Base):
    """The actual per-project secret for a header-auth ActionConnector -
    see RestConnectorCredential. Only used when auth_style == 'header'."""
    __tablename__ = "action_connector_credentials"
    id = Column(Integer, primary_key=True)
    connector_id = Column(Integer, ForeignKey("action_connectors.id"))
    client_id = Column(Integer, ForeignKey("clients.id"))
    encrypted_value = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class OAuthProvider(Base):
    """Tier B: a generic OAuth2 provider definition - Slack, or any
    other OAuth2 SaaS, registered as configuration rather than a new
    Python route per provider. Google itself stays on its own
    dedicated, already-working path (Drive/Gmail/Calendar); this is
    for providers added after this system exists, admin-only since
    registering one means holding a real client secret."""
    __tablename__ = "oauth_providers"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    slug = Column(String, unique=True)          # used in the connect URL path, e.g. "slack"
    authorize_url = Column(String)
    token_url = Column(String)
    client_id = Column(String)
    encrypted_client_secret = Column(Text)
    scopes = Column(String)                      # bot-token scopes, sent as the "scope" param - provider's own format
    user_scopes = Column(String)                 # user-token scopes, sent as the separate "user_scope" param Slack (and
                                                  # some other providers) require for anything done on behalf of the
                                                  # signed-in user rather than the app/bot itself - e.g. Slack's
                                                  # search.messages API only works with a user scope (search:read),
                                                  # never a bot one. Requesting it via "scope" instead gets rejected
                                                  # by Slack's own authorize page as an invalid permission.
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class OAuthConnection(Base):
    """A project's actual connection to an OAuthProvider - the token,
    kept separate from the provider definition the same way
    RestConnectorCredential is kept separate from RestConnector."""
    __tablename__ = "oauth_connections"
    id = Column(Integer, primary_key=True)
    provider_id = Column(Integer, ForeignKey("oauth_providers.id"))
    client_id = Column(Integer, ForeignKey("clients.id"))
    connected_label = Column(String)              # e.g. workspace/team name, for display
    encrypted_access_token = Column(Text)
    encrypted_refresh_token = Column(Text)
    token_expiry = Column(DateTime)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Event(Base):
    """Stands in for the event bus / audit log - every module run writes
    one of these, and the dashboard's event feed reads from it, scoped
    to whichever project is currently selected."""
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    module_id = Column(String)
    status = Column(String)   # success | warning | error
    message = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ProjectMembership(Base):
    """A person's role for one specific project - keyed by email, not
    user_id, so someone can be added to a project's roster before they've
    ever signed in (same spirit team_emails used to work). This is what
    lets two different people have different levels of access to the SAME
    project, which User.role alone can't express (that's global - one
    role for every project). A global admin (User.role == "admin") always
    has full access regardless of what's here; see get_effective_project_role
    in main.py. Currently the only thing this actually gates is the Team
    & Keys section - not a full per-project rewrite of every permission
    check in the app."""
    __tablename__ = "project_memberships"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    email = Column(String, nullable=False)
    role = Column(String, nullable=False, default="member")   # "admin" | "member" | "viewer" | "client"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
