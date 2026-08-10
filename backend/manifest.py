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
    "extraction-engine": {
        "module_id": "extraction-engine",
        "module_class": "engine",
        "display_name": "Extraction engine",
        "reads": ["Meeting", "Document"],
        "writes": ["Decision", "ActionItem", "OpenQuestion"],
        "description": "Extracts decisions, action items, and open questions via Claude.",
    },
    "brief-generator": {
        "module_id": "brief-generator",
        "module_class": "engine",
        "display_name": "Status brief generator",
        "reads": ["Meeting", "Document", "Decision", "ActionItem", "OpenQuestion"],
        "writes": ["Brief"],
        "description": "Writes a full status brief for the engagement via Claude.",
    },
    "contradiction-detector": {
        "module_id": "contradiction-detector",
        "module_class": "engine",
        "display_name": "Contradiction detector",
        "reads": ["Decision"],
        "writes": ["Contradiction"],
        "description": "Flags decisions on record that appear to disagree with each other.",
    },
    "digest-notifier": {
        "module_id": "digest-notifier",
        "module_class": "notifier",
        "display_name": "Digest notifier",
        "reads": ["Event", "Brief"],
        "writes": [],
        "description": "Emails a digest of recent activity via Resend.",
    },
}
