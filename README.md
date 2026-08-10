# Client Memory Console

A working platform for running the client memory agent's phases - not a mockup. Three modules (Fireflies connector, Google Drive connector, extraction engine), a shared artifact store, an event log standing in for the event bus, and a dashboard to run modules and query what's been ingested. Every project matches by identity, not freeform text - a project's own name (in meeting titles and filenames) and its client email domain (as a participant or file collaborator) - set once when you create the project, so this isn't tied to one client and doesn't risk pulling another project's content in on a coincidental word match. Access is gated behind Google sign-in, and each signed-in person has their own encrypted Anthropic API key that only their own extraction runs use.

It starts in **demo mode**: with no credentials configured, each module generates realistic sample data for whichever project you've created instead of failing, so you can see the whole pipeline work - ingest, extract, query - before wiring up a single real API key. Add credentials later and the exact same code path runs against real data; nothing about the logic changes, only which branch it takes.

## Roles

Four roles: **admin**, **member**, **viewer**, **client**.

- **Viewer** can sign in, browse everything (modules, event feed, query, records) but cannot run a module - the RUN button is replaced with a "read-only access" note, and the API rejects the run request server-side too, not just in the UI.
- **Member** can run modules and manage their own API key, same as before roles existed.
- **Admin** can additionally reach `/admin` to see the team and change anyone's role. The platform always keeps at least one admin - trying to demote the last one is rejected with a clear error rather than silently locking everyone out.
- **Client** is different in kind, not just permission level - locked to exactly one project, redirected to `/portal` instead of the dashboard, and can only ever see that project's latest brief and decisions. See "The full set of agents" below for how the lock is enforced.

New users default to `member`. Anyone whose email is in `ADMIN_EMAILS` is promoted to admin automatically on sign-in (not just their first sign-in), so adding someone there is enough to make them an admin without touching the database.

## Login and multi-provider AI access

- Every route except `/login` and the OAuth callback requires a signed-in Google account. Signing in creates a `User` row keyed on Google's stable account ID, not just the email, so an account can't be impersonated by re-registering the same address elsewhere.
- Each person picks their own AI provider - **Anthropic (Claude), OpenAI (ChatGPT), Google (Gemini), or a custom OpenAI-compatible endpoint** (Groq, Together, a local Ollama server, anything exposing the same `/chat/completions` shape) - from the header settings panel, based on whatever mix of capability and cost makes sense for them. Each provider has its own key, encrypted separately (`encrypted_anthropic_key`, `encrypted_openai_key`, `encrypted_gemini_key`, `encrypted_custom_key`), so switching providers doesn't mean losing or overwriting a key you already had set for another one.
- Every module that talks to an AI provider - extraction, the status brief generator, the contradiction detector, Ask, and any config-driven custom agent - goes through one shared function, `llm_client.complete()`, instead of each having its own hardcoded call. Which actual provider that reaches is built fresh per request from that user's own settings (`main.py`'s `get_user_llm_config()`) - never a shared global, so one person's provider choice and key never leak into another's runs. The event log records which provider a run actually used (`live, openai`, `live, gemini`, ...) or `demo` if nothing's configured.
- Model names are never hardcoded as the only option - every provider takes whatever model string is typed into the settings panel. Suggested defaults are shown when you first pick a provider, but these go stale within months of being written, so they're a starting point, not a lock-in.
- `ALLOWED_EMAIL_DOMAIN` in `.env` restricts sign-in to your Workspace domain as a second layer, independent of however the Google OAuth consent screen itself is configured.

**Known, honest gap:** the OpenAI and Gemini code paths are correct by inspection and match each provider's documented API shape, but were never verified against the real services - `api.openai.com` and `generativelanguage.googleapis.com` aren't reachable from the sandbox this was built in. The Anthropic path is unchanged from what's been running in this codebase for months. Worth actually testing the other two directly once real keys are available, rather than assuming the untested paths work just because the code looks right.

### Setting up Google sign-in

1. In [Google Cloud Console](https://console.cloud.google.com), open **APIs & Services > Google Auth Platform**. If this is on your `themirailabs.com` Workspace, choose **Internal** as the user type when prompted - only your org's accounts can sign in, and Google's app-review process doesn't apply.
2. Go to **APIs & Services > Google Auth Platform > Clients tab**, click your OAuth client (or **Create Client** if you haven't yet, application type **Web application**). Add these two as authorized redirect URIs (both needed - one for login, one for the Team & keys "Connect Google Drive" button):
   - `http://localhost:8000/auth/google/callback`
   - `http://localhost:8000/drive/connect/callback`
   
   Add your production equivalents of both once you deploy.
3. Copy the client ID and secret into `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
4. Generate the two secrets and add them to `.env`:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"                         # SESSION_SECRET
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # SECRET_KEY
   ```

## How this maps to what we designed

| Piece | What it is here |
|---|---|
| Module manifest | `backend/manifest.py` - a scaled-down version of the manifest spec from the architecture doc (module_class, reads, writes) |
| Connectors (Phase 2) | `backend/connectors.py` - Fireflies and Google Drive, both matching by project name (title/filename) and client domain (participant/sharing) |
| Extraction engine (Phase 3) | `backend/extraction.py` - Claude-based extraction of decisions and action items |
| Core artifact model (Phase 1) | `backend/models.py` - Client, Meeting, Document, Decision, ActionItem |
| Event bus / audit log | `backend/models.py::Event`, surfaced live in the dashboard's event feed |
| Storage and retrieval (Phase 4) | SQLite by default (swap to Postgres via `DATABASE_URL` when ready) plus a simple keyword query endpoint, a repository view (`/repository`) for browsing every synced record with its synthesized summary, and an Ask layer (`/api/ask`, `backend/qa.py`) that synthesizes an actual answer with cited sources rather than just returning matches |
| Interface (Phase 5) | `frontend/dashboard.html` - the console itself, plus `frontend/repository.html`, `frontend/projects.html`, and `frontend/admin.html` |

## Run it

```bash
cd sbami-agent-platform
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env            # fill in credentials - see "Setting up Google sign-in" below
cd backend
uvicorn main:app --reload
```

`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`, and `SECRET_KEY` are required even to reach the dashboard - the app won't let you in without a real Google sign-in. Everything else (Fireflies, Drive, Anthropic) can stay blank to start in demo mode.

Open `http://localhost:8000`, sign in with Google, then create your first project - a name, and if you have one, the client's email domain. Click **RUN** on each module card - you'll see demo meetings and documents appear in the event feed immediately, titled using your project's own name. Click your email in the header to add your own Anthropic API key (optional - extraction runs in demo mode without one). Run the extraction engine after the connectors - it writes a synthesized summary onto every meeting and document, not just decisions and action items. Try **Ask a question** with something like "what is this about" to get a synthesized answer with cited sources, or use the plain keyword search below it for a quick lookup - either way, results include a **View source** link straight back to Fireflies or Drive. **REPOSITORY** in the header opens a dedicated page to browse every synced meeting and document, expand to see the full transcript or content, and jump to the source.

## Teams, matching, and going from demo to live

Each project is really a **team**: a name, a client email domain, a list of team member emails (for Fireflies), and its own Fireflies/Drive credentials - all set from the project bar and the **Team & keys** panel, not from `.env`. Nothing about Fireflies or Drive access lives in backend configuration anymore; it's per-project, encrypted the same way each user's Anthropic key is.

Matching is identity-based, not freeform keyword search, on purpose - a keyword can coincidentally match another project's content, an email address or a title generally can't:

- **Project name** always matches - a meeting title or a Drive filename containing the project's name gets pulled in. This is the one rule that's always active, since every project has a name.
- **Client domain** (optional, set in "Edit domain") matches anyone at that domain as a Fireflies participant, or anyone at that domain the Drive file is shared with. Neither Fireflies' nor Drive's API supports filtering server-side by "anyone at this domain" - both only match exact email addresses in their query language - so this works by scanning the most recent 100 meetings/files and checking their actual participants/permissions in code. That's a real bound, not a stylistic choice: it's a scan of what's recent, not an exhaustive guarantee across everything ever shared with that domain.
- **Team member emails** are a third, independent way to match Fireflies meetings specifically: a meeting is pulled in if *any* of the listed emails attended, regardless of title or domain. Useful for internal-only discussions about a client where no one from the client's domain was actually on the call.
- **Drive has no team-email equivalent.** Its access is gated by what's shared with the connected account or service account in the first place - a matching rule can only narrow what's already visible, it can't unlock new access.

To go from demo to live, open **Team & keys** on a project and set:
1. **Team members** - comma-separated emails.
2. **Fireflies API key** - from app.fireflies.ai → Integrations → Fireflies API → Get API Key (or Settings → Developer settings → Personal tab directly). If your Fireflies plan has team/admin roles, generate this from an account with visibility into the whole team's shared meetings, not just one person's own - otherwise the connector will miss meetings recorded by teammates.
3. **Drive access** - click **Connect Google Drive** and sign in with whichever Google account should provide the documents. No folder-sharing step needed - you're authorizing your own Drive access, not a robot account's. The connection is tied to the project, not to whoever's logged into the console at the time, so it keeps working regardless of who clicks RUN later. (A **"Use a service account instead"** option is still available if you specifically want a shared credential not tied to any one person's account - that path still requires manually sharing the target folder with the service account's email address.)

Each of these shows a status line once set ("Key set for this team") without ever displaying the secret itself back to the browser. `ANTHROPIC_API_KEY` in `.env` remains an optional server-wide fallback for extraction, but the intended path is still each signed-in user setting their own from the header, same as before.

Once credentials are in place, the same `RUN` buttons hit the live APIs instead of generating samples - no restart, no code changes, no `.env` edits.

## Deploying to the cloud

**This isn't a Vercel-shaped app**, unlike Pineloop or the Signal Engine. Vercel is built around serverless functions with an ephemeral, read-only filesystem - fine for a marketing site or stateless API calls, but this platform needs a database that persists between requests and a session/cookie-based login that assumes a normal always-on server process. Deploying this to Vercel as-is would mean the SQLite file (or any local state) gets wiped on every cold start. Don't fight the platform - use one built for persistent full-stack apps instead.

**Recommended: Render.** Closest thing to "as easy as Vercel" for an app shaped like this one - connects to a Git repo or a Dockerfile, gives you a managed Postgres instance, HTTPS and a public URL out of the box. As of now, a Starter web service is $7/month and Basic Postgres is around $6-7/month - roughly $13-14/month for something the whole team can actually rely on. There's a free tier too, but it's worth knowing its limits going in: the free web service spins down after 15 minutes idle (30-60 second cold start on the next request) and the free Postgres database is deleted after 30 days - fine for kicking the tyres, not for the team depending on this daily.

**Alternatives worth knowing about:**
- **Fly.io** - more manual configuration, but more control and often cheaper at small scale if you're comfortable with it.
- **Railway** - similarly easy to Render, but its free/hobby tier is usage-based credits rather than a flat plan, so cost is less predictable once a database is in the mix.
- **A plain VPS** (DigitalOcean, Hetzner) - full control, generally cheapest option long-term, but you're managing TLS certificates, reverse proxy, and process supervision yourself rather than the platform doing it.

### Steps (Render)

The short version below covers the shape of it - for the actual clicks, commands, and environment variable list, see `DEPLOY.md`.

1. **Push this project to a Git repo** (GitHub) - Render deploys from a repo, not a manual upload.
2. **Create a Postgres instance** in Render's dashboard. Copy the connection string it gives you.
3. **Create a Web Service**, pointing at your repo. Render will detect the `Dockerfile` and build from it - no other configuration needed there.
4. **Set environment variables** in the Web Service's dashboard - everything from `.env.example` except leave `DATABASE_URL` as the connection string from step 2, and set `COOKIE_SECURE=true` now that you're on HTTPS.
5. **Update the Google OAuth client.** Back in Google Cloud Console > APIs & Services > Google Auth Platform > Clients tab, add Render's assigned URL (`https://your-service.onrender.com/auth/google/callback`) as an authorized redirect URI - keep the localhost one too, so local development still works.
6. **Custom domain (optional).** Render will give you a CNAME target to point at from wherever your DNS lives - Squarespace, in your case, same as Pineloop's setup. Once that resolves, add the custom domain to both Render's settings and the Google OAuth redirect URIs.
7. **Deploy, then sign in once as yourself** to confirm `ADMIN_EMAILS` actually promoted your account - check `/admin` loads before inviting anyone else.

## The full set of agents

Beyond the original three (Fireflies connector, Drive connector, extraction engine), the platform now includes:

- **Gmail connector** - rides the same Google OAuth connection Drive already uses ("Connect Google Account" in Team & keys requests both scopes in one consent). Matches emails the same way as Drive and Fireflies: subject contains the project name, or sender/recipient is on the client's domain. Connections made before this existed only granted Drive access - reconnect once to pick up Gmail too.
- **Open questions**, extracted by the same extraction engine alongside decisions and action items - anything raised but not resolved (a question asked but not answered, a dependency on someone else confirming something). Previously discarded entirely; now its own artifact type, visible in the Repository's "Open questions" tab.
- **Status brief generator** - reads everything stored for a project and writes a real status document, the kind you'd send someone catching up cold. Visible in the Repository's "Briefs" tab.
- **Contradiction detector** - reads every decision on record and flags pairs that genuinely disagree. Regenerated fresh on every run rather than accumulated, since it's a computed view over the current decisions, not an append-only log.
- **Digest notifier** - a Resend API key and a recipient address per project (set in Team & keys), sends an email summarizing recent activity and the latest brief. Uses Resend rather than sending from a connected Gmail account on purpose - sending automated mail is a different, more sensitive thing than reading it.
- **Client portal** - a new `client` role, locked to exactly one project (set from the Admin panel, which prompts for which project once you pick that role). Client accounts land on `/portal` instead of the dashboard - a minimal read-only page showing only the latest brief and decisions, nothing else. The project lock is enforced at `get_current_project` itself, not just hidden in the UI - a client account can't see another project's data even by manipulating session state directly, which was tested against directly, not assumed.

All of these show up in Step 2 automatically via the same manifest system the original three used - `manifest.py` plus a runner function in `main.py`'s `RUNNERS` dict, nothing in the dashboard needed to change to display them. The three new artifact types (open questions, briefs, contradictions) are readable, deletable, and bulk-deletable through the same generic `/api/records/{type}` endpoints the original four types already used - extending `model_map` in three places in `main.py` was the entire backend cost of exposing them.

## Config-driven agents - no redeploy needed

Beyond the seven built-in modules, any admin or member can create a new agent from the dashboard itself - Step 2 → **+ New agent**: a name, which artifact types it reads (Meetings, Documents, Decisions, Action items, Open questions, Briefs), and a plain-language prompt. No Python file, no deployment. It's available either to just the current project or every project, and it shows up in the module list immediately, runs like any other module, and its output lands in the Repository's **Agent outputs** tab.

This works because `extraction.py`, `brief.py`, and `qa.py` were all doing the same shape of thing already - gather some records, hand them to Claude with a prompt, do something with the result - just never pulled into one reusable path. `custom_engine.py` is that abstraction: one generic runner (`run_custom_engine`) that reads a `PromptEngine` config row (which artifact types to pull, what to ask Claude to do with them) and writes to a generic `EngineOutput` table, rather than a dedicated model per agent. `/api/modules` merges these config rows into the same list the built-in modules already populate, and `/api/run/{module_id}` falls back to looking up a matching `PromptEngine` whenever the module ID isn't one of the built-ins - so the dashboard, the run button, and the event feed all work identically for a custom agent without knowing it's config-driven at all.

The tradeoff, stated plainly: a custom agent can only read existing artifact types and write plain text back - it can't define a new structured output (no custom "risk severity" field, no new relationships between records) the way a real Python module can. That's genuinely a different, larger project - see "Can we make this a platform where agents can be made?" in the project history for where the line sits between this and true no-code agent authoring.

### Config-driven connectors too - Tier A

The same idea, applied to pulling data *in* rather than reasoning over what's already there. Step 2 → **+ New connector**: a display name, a search URL (with `{query}`/`{domain}` placeholders that get replaced with the project's name and client domain), how to authenticate, and a field mapping - dotted JSON paths telling it which part of the response is the list of results, and which fields within each one map to title/content/url/date. No Python file. Results land as Documents, same as Drive or Gmail.

Two authentication styles, kept deliberately separate rather than unified, because they're genuinely different problems:

- **Static API key** ("header") - a value pasted per-project (same pattern as the Fireflies/Resend keys), sent in whichever header the target API expects. This covers Slack (a bot token needs no OAuth dance for basic search) and most API-key-based SaaS search endpoints.
- **Connected Google account** ("google_oauth") - no separate credential at all; reuses the same OAuth token Drive and Gmail already use. **Google Calendar ships pre-configured this way** - seeded automatically on startup as a real `RestConnector` row, not a special case, proof that the abstraction genuinely covers it: `calendar.readonly` was added to the existing consent screen, and Calendar's own REST shape (`GET .../events`, results in `items`, fields like `summary`/`description`/`htmlLink`) fit the same generic field-mapping model as everything else. Anything else reachable with that same Google token - Sheets, Tasks, Contacts - could be added the identical way, as configuration, not code; none of those are pre-seeded, since their data doesn't fit this platform's Document model as cleanly as Calendar events do. One thing worth knowing plainly: **Google Meet transcripts need no connector at all** - when Workspace records them, they land in Drive as Google Docs, which the existing Drive connector already picks up.

What Tier A alone doesn't cover: OAuth2 for any provider other than Google, and GraphQL-shaped APIs like Fireflies, which don't fit a REST field-mapping model at all. The first of those is Tier B, below.

### Config-driven OAuth2 providers - Tier B

Slack (and most other OAuth2 SaaS APIs) need a real consent screen, not just a static key - a person clicks "Connect," approves access, and the platform gets a token back. Before Tier B, that meant a dedicated Python route per provider, the way Drive/Gmail/Calendar's connect flow works. Tier B generalizes the *mechanics* of that flow - build an authorize URL, exchange a code for a token, refresh an expired one - into one generic path driven by an `OAuthProvider` config row (authorize URL, token URL, client ID/secret, scopes), admin-registered from the Admin page rather than written as code.

One connect route (`/oauth/{slug}/connect/{project_id}`) and one callback (`/oauth/callback`) serve every provider - which provider is in play comes from server-side session state set during connect, not the URL, so only one redirect URI ever needs registering with each provider's own app settings. A `RestConnector`'s `auth_style` can now be `"oauth_provider"` alongside `"header"` and `"google_oauth"`, referencing which `OAuthProvider` to use - so a Slack-backed connector built this way needs no code at all: register Slack once as a provider (admin-only, since it means holding a real client secret), then anyone building a "+ New connector" can pick "OAuth-connected app," choose Slack, and click Connect per project from the module card.

This was tested against a real, reachable OAuth2 provider rather than assumed correct - and that testing caught a real mistake worth being honest about: an earlier claim that this reached Slack's actual servers was wrong, based on checking a status code without checking the response body. The request never left this development sandbox; `slack.com` isn't in its network allowlist, and the 403 it returned was the sandbox's own proxy, not Slack. Corrected by testing against GitHub's real OAuth endpoints instead, where the response's authentic GitHub headers, session cookie, and security policy left no ambiguity that the token exchange genuinely reached GitHub's infrastructure - real proof, not an assumption. Worth remembering going forward: a matching status code alone isn't verification.

What Tier B doesn't cover: PKCE-only flows, providers whose token exchange doesn't fit the standard `code` + `client_id` + `client_secret` + `redirect_uri` shape, and anything needing more than a bearer token in an `Authorization` header for subsequent calls. Still real code for those, same as GraphQL APIs and Google's own flow.

## Extending it

- **A second project** is no longer a future step - the project selector in the header handles it. Per-project data isolation is enforced everywhere (events, meetings, documents, decisions, action items all carry `client_id`), and matching by project name + client domain is meant specifically to avoid the cross-project bleed risk a generic keyword could cause. It's not airtight - two projects that happen to share the same word in their name, or the same client domain, could still collide - but that's a much narrower coincidence than a keyword matching unrelated content.
- **A new module** (e.g. a Notion connector, or a second engine for auto-generated client briefs): add an entry to `manifest.py`, write a runner function, register it in `RUNNERS` in `main.py`. Nothing else changes - this is the manifest pattern from the architecture doc doing its job.
- **Vector search**: the query endpoint is a keyword `LIKE` match today, which is enough to prove retrieval works. The Ask layer (`backend/qa.py`) goes further but takes a shortcut of its own - it hands every meeting and document summary for the project to Claude in one call rather than retrieving just the relevant few first. That's honest and fine at the scale this platform runs at today (tens of records per project), and wrong once a project has hundreds - at that point, moving both `/api/query` and `/api/ask` to real retrieval means adding embeddings (on write, in the connectors/extraction runners) and a vector column - `pgvector` is the natural fit once you're on Postgres.
- **Webhooks instead of polling**: Fireflies supports webhooks that fire on new meeting content - swapping the connector's pull-based `run_fireflies` for a webhook receiver is a Phase 2 upgrade once the manual pilot is proven, per the original build guide.
- **Bringing Fathom back**: the old Fathom connector was removed in favor of Fireflies, but nothing about the manifest/RUNNERS pattern prevents running both side by side - it would just be a fourth module, registered the same way as any other.
- **Per-project roles**: roles are still global - a viewer is a viewer across every project, not scoped to one. Worth doing once different people need different access to different clients.
- **Production cookies**: `SessionMiddleware` is set with defaults suited to local development. Before deploying anywhere reachable over plain HTTP, set `https_only=True` on it in `main.py`.
- **Updating from an older version**: `init_db()` now auto-migrates the schema on every startup - it compares the models against the actual database and adds any missing columns automatically (never drops or alters existing ones, so it's safe to run every time). Pulling a code update and restarting is enough; there's no manual `ALTER TABLE` step anymore. If you were previously using `.env`-based `FIREFLIES_API_KEY` or `DRIVE_SERVICE_ACCOUNT_JSON`, those are no longer read at all - re-enter them from the Team & keys panel on the relevant project.
