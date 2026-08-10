# Testing the platform locally

This walks through going from the unzipped folder to a working, logged-in session where you can watch every piece actually do something. The one step that blocks everything else is the Google OAuth client - every route requires a real sign-in now, so there's no skipping it to "just look around" in demo mode.

## 1. Unzip and install

```bash
unzip sbami-agent-platform.zip
cd sbami-agent-platform
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

## 2. Create a Google OAuth client (required, ~5 minutes)

You need this even to reach the dashboard - login isn't optional.

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project if you don't have one for this.
2. **APIs & Services > Google Auth Platform > Clients tab.** If you're using your `themirailabs.com` Workspace account, choose **Internal** when prompted for user type - simpler, no Google review needed, and it restricts sign-in to your org automatically.
3. **APIs & Services > Credentials > Create Credentials > OAuth client ID**, type **Web application**.
4. Under **Authorized redirect URIs**, add exactly: `http://localhost:8000/auth/google/callback`
5. Save. Copy the **Client ID** and **Client Secret** - you'll need both in the next step.

## 3. Generate the two local secrets

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 4. Fill in .env

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:

```
GOOGLE_CLIENT_ID=<from step 2>
GOOGLE_CLIENT_SECRET=<from step 2>
SESSION_SECRET=<first command's output from step 3>
SECRET_KEY=<second command's output from step 3>
ADMIN_EMAILS=<your own email> 
```

Leave `ANTHROPIC_API_KEY` blank for now, and don't set anything in the Team & keys panel yet - that's what keeps the platform in demo mode, which is exactly what you want for this first pass. `COOKIE_SECURE` should stay `false` for local testing over plain `http://localhost`.

## 5. Run it

```bash
cd backend
uvicorn main:app --reload
```

Open `http://localhost:8000`.

## 6. Test checklist

Work through these in order - each one builds on the last actually working.

- [ ] **Redirect to login.** Visiting `/` with no session should land you on `/login`, not the dashboard.
- [ ] **Sign in with Google.** Click the button, go through Google's real consent screen, land back on the dashboard. If this fails, see Troubleshooting below - it's almost always the redirect URI.
- [ ] **Confirm you're admin.** Your email should show in the header with an `ADMIN` tag next to it, and an `ADMIN` link should be visible - both only appear because your email matched `ADMIN_EMAILS`.
- [ ] **Create a project.** Use the project bar under the header - a name, and optionally a client email domain. Demo data will be titled using this name, so pick something recognizable.
- [ ] **Run all three modules.** Click RUN on each module card in order (Fireflies, then Drive, then extraction). Watch the event feed fill in below - you should see messages like "Demo mode - generated 2 sample meeting(s)" and eventually "Extracted N decision(s) and N action item(s) (demo)".
- [ ] **Query the data.** Try "UHID" or "HIS/LIS" in the search bar - you should get back a meeting, a document, and a decision or action item, all grouped and shown together. This is the actual point of the whole pipeline - confirm it works before anything else matters.
- [ ] **Add and remove an API key.** Click your email to open the settings panel, paste anything into the API key field (a real key isn't needed yet, just to see the flow), save it, confirm the status line changes to "Key set". Clear it and confirm it reverts. This is testing the UI and storage, not extraction quality - that needs a real key later.
- [ ] **Visit /admin.** You should see a table with just you in it, role `admin`, a green dot under API key if you left one set from the previous step.
- [ ] **Try to demote yourself.** In the admin table, change your own role to `viewer`. This should fail with "Can't remove the platform's last admin" - if it lets you, something's wrong; that safeguard exists specifically to stop you locking yourself out.
- [ ] **Sign out and back in.** Confirm `/auth/logout` actually clears the session (you land back on `/login`), and signing in again works a second time without issues.

## 7. Testing the viewer role (optional, needs a second account)

Everything above tests the platform as an admin. To see the actual restriction in effect: have a colleague sign in with their own Google account (or use a second Google account of your own), which creates them as `member` by default. From the admin table, change their role to `viewer`, then have them refresh - the RUN buttons on their module cards should be replaced with a quiet "read-only access" note, while search and browsing still work normally for them.

## 8. Testing all 7 modules

By this point you have a project and can reach the dashboard. Here's how to actually verify each of the 7 modules - what "it worked" looks like for each one, and what you need before it'll do more than demo mode.

### The fast pass - demo mode, no credentials at all

This is the quickest way to confirm all 7 are wired correctly, since demo mode needs nothing set up. Click **RUN** on each, in this order (later ones depend on earlier ones having data to work with):

1. **Fireflies connector** → event feed shows "Demo mode - generated 2 sample meeting(s)"
2. **Drive connector** → "Demo mode - generated 2 sample document(s)"
3. **Gmail connector** → "Demo mode - generated 1 sample email(s)" (emails show up in the Repository's **Documents** tab alongside Drive files, not a separate tab - Gmail messages are stored the same way documents are)
4. **Extraction engine** → "Summarized 5 record(s)..." - open the **Repository** page, Meetings and Documents tabs should now show a summary under each item
5. **Brief generator** → "Generated a new status brief (demo)" - check the Repository's **Briefs** tab, should show one entry
6. **Contradiction detector** → "Fewer than two decisions on record - nothing to compare" is the expected result here, since demo-mode extraction rarely produces real decisions unless your project name/content happens to match SBAMI-style demo triggers (UHID, LIS, billing, ABHA) - this is normal, not a failure
7. **Digest notifier** → "Resend key or notify email not set for this project - nothing sent" if you haven't configured those yet in Team & keys - also expected, confirms the module runs correctly without crashing

If all 7 produce one of these messages and nothing shows as an error in the event feed, the wiring is confirmed correct end to end.

### Testing with real credentials

Each module needs different real credentials before it does more than generate samples - here's the minimum per module:

| Module | Needs |
|---|---|
| Fireflies connector | A Fireflies API key (Team & keys) |
| Drive connector | A connected Google account (Team & keys → Connect Google Account) |
| Gmail connector | The same connected Google account - no separate setup, but see the note below if you connected before Gmail support existed |
| Extraction engine | Your own Anthropic API key (click your email in the header) |
| Brief generator | Same Anthropic key, plus at least one meeting or document already synced |
| Contradiction detector | Same Anthropic key, plus at least two decisions already extracted |
| Digest notifier | A Resend API key and a notify email (both in Team & keys) |

**If you connected Google Drive before Gmail support was added**, the Gmail connector will fail with a permissions error the first time you run it live - the old connection only granted Drive access. Fix: open Team & keys, click **Disconnect**, then **Connect Google Account** again to grant both scopes in one new consent.

**To actually test the contradiction detector finding something real** (not just running without error): manually create two meetings or documents whose content states clearly conflicting facts about the same thing, run the extraction engine on both so they each produce a decision, then run the contradiction detector - with a real Anthropic key, it should flag the pair and the Repository's **Contradictions** tab should show why.

**To confirm the digest notifier actually sends**, check the inbox of whatever address you put in "notify email" - the event feed will say "Sent digest to ..." on success, but the real confirmation is the email actually landing (spam folder included, especially if you're still using Resend's shared test address rather than a verified domain).

## 9. Testing the config-driven agent system

This is the newest piece, and the thing most worth verifying specifically is the claim behind it: that a new agent can be created *and used* without touching code or restarting anything.

### Create one and watch it appear live

1. Go to **Step 2 · Extract & store**, click **+ New agent**.
2. Fill in something concrete rather than a placeholder - try:
   - **Name:** Risk Tracker
   - **Description:** Flags anything that looks like a risk to this engagement
   - **Reads:** tick Meetings, Documents, and Open questions
   - **Prompt:** "Read the records and list anything that looks like a risk to this engagement succeeding, with why."
   - **Scope:** This project only
3. Click **Create agent**. It should appear in the module list immediately, right alongside Fireflies/Drive/extraction - no page reload needed, and definitely no restart. If you have a second browser tab open on the same dashboard, refresh it - the new agent should be there too, since it's reading from the same database, not something held only in your browser's memory.
4. Click **RUN** on it. Without an Anthropic key set, you'll get a demo-mode message describing what it *would* do rather than a fabricated result - that's correct, not a failure. With a key set (yours, in the header), it should produce a real answer.
5. Open the **Repository** page → **Agent outputs** tab. Your Risk Tracker's result should be there, with the agent's name and a timestamp.

### Test the two scope levels

Create a second agent, this time with **Scope: Every project**. Switch to a different project (or create a new one) using the project selector, and confirm the global agent shows up there too, while the project-scoped Risk Tracker from step 1 does *not* - that's the actual behavior to verify, not just that something appears.

### Test that it's genuinely gone when deleted

Click **Delete agent** on the module card. It should disappear from Step 2 immediately. Its past output should still be sitting in the Repository's Agent outputs tab, unaffected - deleting the agent stops it from running again, it doesn't erase what it already produced.

### Test the validation, not just the happy path

Try creating an agent with the name filled in but the prompt left blank, or with no "reads" boxes checked - it should be rejected with a clear message rather than silently creating a broken agent you'd only discover was broken when you tried to run it.

## Troubleshooting

**"redirect_uri_mismatch" from Google.** The URI in Google Cloud Console doesn't exactly match what the app sent - check for a trailing slash mismatch, `http` vs `https`, or a typo. It must be exactly `http://localhost:8000/auth/google/callback`.

**Stuck in a redirect loop between `/` and `/login`.** Almost always `SESSION_SECRET` being blank or changing between restarts (each restart with a different value invalidates existing sessions) - confirm it's actually set in `.env` and that `.env` is being loaded (you're running from inside `backend/` with the venv active).

**"SECRET_KEY is not set" error when saving an API key.** `SECRET_KEY` must be a real Fernet key from the exact command in step 3, not an arbitrary string - Fernet requires a specific 32-byte urlsafe-base64 format.

**Nothing happens when you click RUN.** Open your browser's dev tools console - a 401 there means your session expired (sign in again); a 500 means check the terminal running `uvicorn` for the actual Python traceback.
