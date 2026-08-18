"""
Module registry. Each entry is a scaled-down manifest (module_class,
reads, writes) matching the shape defined in the architecture spec -
this is the seed of that manifest system, not a simulation of it.
Adding a new module later means adding an entry here plus a runner
function registered in main.py's RUNNERS dict; nothing else in the
platform needs to change - the dashboard's Step 2 renders whatever's
in this dict automatically.
"""

MODULES = {
    "fireflies-connector": {
        "module_id": "fireflies-connector",
        "module_class": "connector",
        "display_name": "Fireflies connector",
        "reads": [],
        "writes": ["Meeting"],
        "description": "Pulls meetings matching the project's name or client domain.",
    },
    "drive-connector": {
        "module_id": "drive-connector",
        "module_class": "connector",
        "display_name": "Google Drive connector",
        "reads": [],
        "writes": ["Document"],
        "description": "Pulls documents matching the project's name or client domain.",
    },
    "gmail-connector": {
        "module_id": "gmail-connector",
        "module_class": "connector",
        "display_name": "Gmail connector",
        "reads": [],
        "writes": ["Document"],
        "description": "Pulls emails matching the project's name or client domain.",
    },
    # extraction-engine, brief-generator, and contradiction-detector are
    # deliberately not registered here - removed from the Step 2 module
    # grid (and from being manually RUN-able) rather than deleted. Their
    # runner code (extraction.py, brief.py, contradiction.py) is untouched,
    # nothing in the database they've already written gets removed, and
    # extraction/brief still run automatically in the background (see
    # api_run's post-sync auto-chain and run_auto_sync_for_project in
    # main.py) - only the manual, visible "engine" entry point is gone.
    # Uncomment to bring any of them back as a clickable module:
    #
    # "extraction-engine": {
    #     "module_id": "extraction-engine",
    #     "module_class": "engine",
    #     "display_name": "Extraction engine",
    #     "reads": ["Meeting", "Document"],
    #     "writes": ["Decision", "ActionItem", "OpenQuestion"],
    #     "description": "Extracts decisions, action items, and open questions via Claude.",
    # },
    # "brief-generator": {
    #     "module_id": "brief-generator",
    #     "module_class": "engine",
    #     "display_name": "Status brief generator",
    #     "reads": ["Meeting", "Document", "Decision", "ActionItem", "OpenQuestion"],
    #     "writes": ["Brief"],
    #     "description": "Writes a full status brief for the engagement via Claude.",
    # },
    # "contradiction-detector": {
    #     "module_id": "contradiction-detector",
    #     "module_class": "engine",
    #     "display_name": "Contradiction detector",
    #     "reads": ["Decision"],
    #     "writes": ["Contradiction"],
    #     "description": "Flags decisions on record that appear to disagree with each other.",
    # },
    "digest-notifier": {
        "module_id": "digest-notifier",
        "module_class": "notifier",
        "display_name": "Digest notifier",
        "reads": ["Event", "Brief"],
        "writes": [],
        "description": "Emails a digest of recent activity via Resend.",
    },
}

# Connector presets for the "+ New Connector" form. Each entry pre-fills a
# known service's field mapping - a dotted path into that service's own
# JSON response shape, worked out once here instead of by whoever's
# creating the connector. Served to the frontend via GET
# /api/connector-presets, so adding a new preset means adding an entry
# here; nothing in the frontend needs to change.
#
# github and slack use auth_style "oauth_provider" rather than a pasted
# API key - this only works once an admin has registered a matching OAuth
# provider (Admin page -> "+ New OAuth provider") with a GitHub OAuth App
# / Slack App's own client ID and secret, the same one-time setup Claude's
# own GitHub connector relies on. Until that's done, these two presets
# still fill in the connector's shape, but the provider dropdown they
# point at will read "No providers registered yet".
CONNECTOR_PRESETS = {
    "calendar": {
        "name": "Google Calendar",
        "description": "Pulls calendar events matching the project name, via the connected Google account.",
        "content_type": "document",
        "auth_style": "google_oauth",
        "url": "https://www.googleapis.com/calendar/v3/calendars/primary/events?q={query}",
        "results_path": "items",
        "field_id": "id",
        "field_title": "summary",
        "field_content": "description",
        "field_url": "htmlLink",
        "field_date": "start.dateTime",
    },
    "fathom": {
        "name": "Fathom",
        "description": "Pulls meeting recaps for anyone at the client's domain.",
        "content_type": "meeting",
        "auth_style": "header",
        "auth_header_name": "X-Api-Key",
        "auth_value_prefix": "",
        "url": "https://api.fathom.ai/external/v1/meetings?calendar_invitees_domains[]={domain}&include_summary=true&limit=10",
        "results_path": "items",
        "field_id": "recording_id",
        "field_title": "meeting_title",
        "field_content": "default_summary.markdown_formatted",
        "field_url": "share_url",
        "field_date": "recording_start_time",
    },
    "github": {
        "name": "GitHub",
        "description": "Pulls issues and pull requests matching the project name, via a connected GitHub account.",
        "content_type": "document",
        "auth_style": "oauth_provider",
        "url": "https://api.github.com/search/issues?q={query}+in:title,body",
        "results_path": "items",
        "field_id": "id",
        "field_title": "title",
        "field_content": "body",
        "field_url": "html_url",
        "field_date": "updated_at",
    },
    "slack": {
        "name": "Slack",
        "description": "Pulls messages matching the project name, via a connected Slack workspace.",
        "content_type": "document",
        "auth_style": "oauth_provider",
        "url": "https://slack.com/api/search.messages?query={query}",
        # Slack's search API has no ISO date field - "ts" is a unix-epoch
        # string, which rest_connector.py's _parse_dt can't parse, so
        # synced messages land with no date. Left pointed at "ts" anyway,
        # since it's the closest thing available and a failed parse just
        # stores a null date rather than erroring - same as any other
        # record whose date field comes back empty.
        "results_path": "messages.matches",
        "field_id": "ts",
        "field_title": "text",
        "field_content": "text",
        "field_url": "permalink",
        "field_date": "ts",
    },
}
