# Deploying to Render - step by step

This walks through the actual clicks and commands, start to finish. Budget about 30-40 minutes the first time, most of it waiting on builds.

Before starting, have these ready:
- A GitHub account
- A Render account ([render.com](https://render.com), sign up with GitHub - saves a step later)
- Your Google OAuth client already created (see the "Setting up Google sign-in" section in README.md) - you don't need real Fireflies/Drive/Anthropic credentials yet, the platform runs in demo mode without them

---

## 1. Push the project to GitHub

From the `sbami-agent-platform/` folder:

```bash
git init
git add .
git commit -m "Initial commit"
```

Create a new empty repository on github.com (no README, no .gitignore - you already have one), then:

```bash
git remote add origin https://github.com/YOUR_USERNAME/sbami-agent-platform.git
git branch -M main
git push -u origin main
```

Confirm `.env` did **not** get pushed - check the repo on GitHub. If it's there, your `.gitignore` didn't catch it; remove it from the repo immediately (`git rm --cached .env`, commit, push) since it would otherwise contain live secrets once you fill it in locally.

## 2. Create the Postgres database

In the Render dashboard: **New > PostgreSQL**.

- Name: `sbami-platform-db` (or whatever you'd recognize later)
- Region: Singapore is the closest Render region to Bengaluru
- Plan: **Basic** (~$6-7/month) - skip the free tier here, it deletes the database after 30 days, which is the wrong failure mode for anything the team is actually using

Click **Create Database**. Once it's provisioned, open it and copy the **Internal Database URL** (not the external one) - you'll use it as `DATABASE_URL` in the next step. The internal URL only works for services inside Render's own network, which is exactly what your web service will be, and it's faster and doesn't count against any external bandwidth.

## 3. Create the web service

**New > Web Service**, connect the GitHub repo you just pushed.

- Render will detect the `Dockerfile` automatically and set Environment to **Docker** - you shouldn't need to touch the build/start command fields.
- Region: same as the database (Singapore), so they talk to each other over the internal network.
- Instance type: **Starter** (~$7/month).

Before clicking create, add environment variables (**Environment** tab). At minimum, to get past the login screen:

| Key | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | from Google Cloud Console |
| `ALLOWED_EMAIL_DOMAIN` | `themirailabs.com` |
| `ADMIN_EMAILS` | your email(s), comma-separated |
| `SESSION_SECRET` | generate fresh - see below |
| `SECRET_KEY` | generate fresh - see below |
| `DATABASE_URL` | the Internal Database URL from step 2 |
| `COOKIE_SECURE` | `true` |

Generate the two secrets locally and paste the output in:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"                          # SESSION_SECRET
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # SECRET_KEY
```

Add this when you have it (fine to leave blank for now - the platform runs in demo mode without it):

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | leave blank - each user sets their own in the dashboard |

Projects (name, client domain, team member emails, Fireflies key, Drive credentials) are all set from within the app after you sign in - none of that is deployment configuration.

Click **Create Web Service**. First build takes a few minutes - watch the logs; a successful one ends with `Uvicorn running on http://0.0.0.0:$PORT`.

## 4. Point Google's OAuth callback at the new URL

Render assigns a URL like `https://sbami-platform.onrender.com`. Copy it.

Google Cloud Console > **APIs & Services > Google Auth Platform > Clients tab** > click your OAuth client > under **Authorized redirect URIs**, add both:

```
https://sbami-platform.onrender.com/auth/google/callback
https://sbami-platform.onrender.com/drive/connect/callback
```

Keep the `localhost:8000` versions of both too - local development still needs them. Save.

## 5. Test it

Visit your Render URL. You should land on `/login`. Sign in with Google, land on the dashboard, and - since your email is in `ADMIN_EMAILS` - visiting `/admin` should show the team table with just you in it.

Run the connectors and extraction engine once to confirm they still work against Postgres exactly like they did locally against SQLite (they will - nothing in the connector or extraction code knows or cares which database it's talking to).

## 6. Custom domain (optional)

Render **Settings > Custom Domain**, add something like `console.pineloop.ai` or whatever subdomain makes sense. Render gives you a CNAME target.

Go to wherever this domain's DNS lives (Squarespace, same as Pineloop) and add a CNAME record pointing your chosen subdomain at that target. Once it resolves, Render issues a TLS certificate automatically - no extra step needed.

Then repeat step 4: add `https://console.pineloop.ai/auth/google/callback` and `https://console.pineloop.ai/drive/connect/callback` as authorized redirect URIs in Google Cloud Console. `COOKIE_SECURE` is already `true`, nothing else changes.

## Ongoing deploys

From here, shipping a change is just:

```bash
git add .
git commit -m "whatever changed"
git push
```

Render rebuilds and redeploys automatically on push to `main`. Since the database is Postgres now, not the local SQLite file, a redeploy doesn't wipe any data - meetings, documents, decisions, and every user's role and encrypted key all persist across deploys.

## Deploying a second, isolated client

Once there's an actual second client to onboard, each one gets its own fully separate Render service and Postgres database - not a new row in the existing one. Nothing in the app code changes for this; it's all repeating steps 1-3 above against a fresh set of resources, plus the env vars below.

1. Fork or branch the repo (or push the same `main` to a second GitHub repo) and tag the commit you're deploying, e.g. `git tag client-acme-2026-08-18 && git push --tags` - see "Tracking releases across deployments" below for why.
2. Repeat step 2 (Create the Postgres database) and step 3 (Create the web service) for this client, using a name that identifies them (e.g. `acme-platform-db`, `acme-platform`) rather than reusing `sbami-platform-*`.
3. Add three more environment variables on top of the table in step 3:

   | Key | Value |
   |---|---|
   | `BRAND_NAME` | this client's name, e.g. `Acme Client Memory Console` |
   | `BRAND_ACCENT_COLOR` | a hex color, e.g. `#2a6f4f` |
   | `ENABLED_MODULES` | comma-separated built-in module IDs this client is allowed to run, or leave blank for all of them |

   `BRAND_NAME`/`BRAND_ACCENT_COLOR` are picked up by every page's header and browser tab automatically (`GET /api/branding`) - no rebuild needed, just an env var change and a redeploy. `ENABLED_MODULES` restricts which of the four built-in connectors/notifier show up under Step 2 and can be RUN.
4. Repeat step 4 (Google OAuth callback URLs) and step 5 (Test it) for this client's own Render URL.

### Tracking releases across deployments

With more than one client deployment on the same codebase, `git tag` is the cheap way to know what's actually running where - tag before each deploy rather than relying on commit messages or memory. Pushing a platform update to multiple clients is then just: merge to `main`, push to each client's remote/branch, let Render's auto-deploy pick it up, tag each one once it's live. No tooling to build for this - it's a discipline, not a feature.
