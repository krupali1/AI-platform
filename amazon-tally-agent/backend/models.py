"""
Single-tenant data model for the Amazon -> Tally automation agent.
No client_id / multi-project scoping anywhere - one deployment serves
one company. TallyRule / TallyFieldMapping / TallyLedgerConfig are
global config that persists across every monthly TallyRun, which is
the whole point of setting them up once.
"""
import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, LargeBinary
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class TallyRun(Base):
    """One month's worth of uploaded input files for one platform -
    the Online Sales flow's "Upload input sheets" step. A run is
    validated once (see TallyReviewItem stage="input"); from the same
    validated run, any number of TallyGenerations can be built, one per
    (order type, location) combination, without re-uploading anything -
    matches the PRD's "separate Tally sheet per B2B/B2C per location"
    requirement without needing a separate upload per slice."""
    __tablename__ = "tally_runs"
    id = Column(Integer, primary_key=True)
    platform_slug = Column(String, nullable=True)  # which TallyPlatform this run's files belong to
    period_label = Column(String)                  # free text, e.g. "July 2026" - not parsed
    status = Column(String, default="draft")        # "draft" | "validating" | "needs_review" | "ready" | "failed"
    error_message = Column(Text, nullable=True)
    review_pending_count = Column(Integer, default=0)   # input-stage only
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    validated_at = Column(DateTime, nullable=True)

    # Deprecated: superseded by TallyGeneration's own total_rows/
    # flagged_rows now that one run can have several generations, but
    # left in place (unused going forward) rather than dropped, so an
    # old row from before this split still reads without a migration.
    total_rows = Column(Integer, default=0)
    flagged_rows = Column(Integer, default=0)
    processed_at = Column(DateTime, nullable=True)


class TallyGeneration(Base):
    """One "Build the Tally sheet" run for a specific (order_type,
    location) slice of an already-validated TallyRun - the unit that
    TallyOutputRow and stage="final" TallyReviewItems hang off. location
    is a Godown value found in the run's own data (or blank to mean
    "all locations, unsplit") - not a separate upload, just a filter
    over the rows the run's files already produced."""
    __tablename__ = "tally_generations"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("tally_runs.id"))
    order_type = Column(String, nullable=True)     # e.g. "B2C" | "B2B" - blank means "every order type in the run"
    location = Column(String, nullable=True)       # a Godown value, or blank for "every location"
    status = Column(String, default="processing")  # "processing" | "needs_review" | "ready" | "failed"
    error_message = Column(Text, nullable=True)
    total_rows = Column(Integer, default=0)
    flagged_rows = Column(Integer, default=0)
    review_pending_count = Column(Integer, default=0)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)


class TallyPlatform(Base):
    """A sales channel the agent pulls reports from - Amazon, Flipkart,
    the company's own website, or anything a user adds from the
    Upload step. A few are seeded on first startup (see main.py's
    _ensure_builtin_platforms), marked is_builtin so they can't be
    deleted by accident, but nothing about the pipeline treats a
    built-in platform as special - a custom one works identically.
    This is what makes "add a new marketplace" pure configuration
    (a new row here + new TallyFieldMapping rows) instead of a code
    change, the promise tally_pipeline.py's module docstring makes."""
    __tablename__ = "tally_platforms"
    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True)       # url/id-safe, e.g. "amazon", "flipkart", "company_website"
    display_name = Column(String)            # shown in the UI, e.g. "Amazon"
    is_builtin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class TallyUploadedFile(Base):
    """Raw bytes stored in the DB rather than local disk - keeps the
    whole app self-contained in one SQLite file with no separate
    storage to provision for a single-tenant deployment."""
    __tablename__ = "tally_uploaded_files"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("tally_runs.id"))
    file_role = Column(String)           # "sales" | "master" | "sample_tally" | "other"
    platform_slug = Column(String, nullable=True)    # which TallyPlatform this is - only set when file_role == "sales"
    order_type = Column(String, nullable=True)       # e.g. "B2C" | "B2B" | "General" - only set when file_role == "sales"
    label = Column(String, nullable=True)    # user-given label, mainly for file_role == "other"
    original_filename = Column(String)
    content_type = Column(String)
    file_blob = Column(LargeBinary)
    size_bytes = Column(Integer)
    sheet_names = Column(Text, nullable=True)    # comma-separated, backfilled after first parse
    uploaded_by = Column(String, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow)


class TallyFieldMapping(Base):
    """Global, persists across runs. One generic concept for both
    'which uploaded file/sheet/column feeds canonical field X' and
    'which canonical field fills sample-sheet output column Y'
    (source_file_role == "sample_tally" case). platform_slug scopes a
    "sales" mapping to one platform - Amazon's "Order ID" column and
    Flipkart's "OrderID" column both feed the same canonical order_id
    field, but through two independent mapping rows. order_type further
    scopes it within a platform - a B2B report commonly carries "Bill
    to X" columns where the matching B2C report carries "Ship to X"
    for the same canonical billing_* field, so the two need
    independent mappings too even though they're the same platform."""
    __tablename__ = "tally_field_mappings"
    id = Column(Integer, primary_key=True)
    target_field = Column(String)        # canonical field name - see tally_pipeline.CANONICAL_FIELDS
    source_file_role = Column(String)    # "sales" | "master" | "other" | "sample_tally"
    platform_slug = Column(String, nullable=True)    # only meaningful when source_file_role == "sales"
    order_type = Column(String, nullable=True)       # only meaningful when source_file_role == "sales"
    source_sheet_name = Column(String, nullable=True)
    source_column_name = Column(String, nullable=True)   # null when constant_value is set instead
    constant_value = Column(String, nullable=True)   # a literal value applied to every row, e.g. Courier = "Amazon" - mutually exclusive with source_column_name
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class TallyLedgerConfig(Base):
    """Global key/value settings - ledger names, voucher types, company
    state - always-applies config, separate from TallyRule's
    conditional logic. A TallyRule's action_value can reference one of
    these by config_key instead of hardcoding a literal ledger name."""
    __tablename__ = "tally_ledger_config"
    id = Column(Integer, primary_key=True)
    config_key = Column(String, unique=True)
    config_value = Column(String)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class TallyRule(Base):
    """The deterministic half of the hybrid rules engine - condition ->
    action, editable via the UI, no redeploy needed. Evaluated ascending
    order_index within its rule_group; first match wins."""
    __tablename__ = "tally_rules"
    id = Column(Integer, primary_key=True)
    rule_group = Column(String)          # "escalation" | "tax_split" | "voucher_type" | "ledger_mapping"
    order_index = Column(Integer, default=0)
    condition_field = Column(String)     # canonical field name, e.g. "ship_to_state", "order_type", "sku_in_master"
    condition_operator = Column(String)  # "equals" | "not_equals" | "in" | "not_in" | "exists" | "not_exists" | "contains"
    condition_value = Column(Text, nullable=True)    # comma-separated for in/not_in
    action_type = Column(String)         # "set_field" | "escalate" | "split_tax" | "use_ledger" | "use_voucher_type"
    action_field = Column(String, nullable=True)
    action_value = Column(Text, nullable=True)   # literal value, a config_key reference, or a small JSON blob for split_tax
    is_active = Column(Boolean, default=True)
    description = Column(Text)           # human label shown in the UI, e.g. "IGST for out-of-state B2C orders"
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class TallyOutputRow(Base):
    """One generated row, scoped to a TallyGeneration (one (order_type,
    location) slice), not the run directly - a run can produce several
    generations without any of them clobbering another. `data` is a
    single JSON blob of canonical field -> value rather than dozens of
    dedicated columns, so a new marketplace's extra fields need no
    migration."""
    __tablename__ = "tally_output_rows"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("tally_runs.id"))
    generation_id = Column(Integer, ForeignKey("tally_generations.id"), nullable=True)
    source_file_role = Column(String)    # the platform_slug this row came from, e.g. "amazon", "flipkart"
    source_row_ref = Column(String)      # e.g. "Sheet1:row482" - traceability back to the raw upload
    order_id = Column(String, nullable=True)     # denormalized for search/filter
    sku = Column(String, nullable=True)
    data = Column(Text)                  # JSON string: canonical field -> value
    status = Column(String, default="ok")    # "ok" | "flagged" | "escalated" | "resolved" | "excluded"
    applied_rule_ids = Column(Text, nullable=True)   # comma-separated, for "why did this row get IGST" traceability
    is_manual_override = Column(Boolean, default=False)
    override_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class TallyReviewItem(Base):
    """The human-in-the-loop queue, shaped around three resolution
    modes rather than a single answer box - matching PIL's own
    prototype: "AI suggestion" (approve/reject a proposed change),
    "Fix here" (type the values in directly), or "Re-upload sheet"
    (replace the source file and re-validate). affected_row_ids (not a
    single FK) because one item - e.g. one unmapped SKU - commonly
    covers many rows. rule_name is denormalized so the item still
    reads sensibly if the rule that created it is later edited or
    deleted.

    stage="input" items belong to the run itself (generation_id null) -
    found by validate_inputs(), before any output exists (e.g. an
    unmapped SKU). stage="final" items belong to one TallyGeneration -
    found by generate_output() while building that specific
    (order_type, location) slice (e.g. a row with no matching rule).

    detail is one JSON blob for everything that varies by trigger_reason
    rather than dozens of nullable columns: {"facts": [{"label",
    "value"}, ...], "available_modes": ["fix", "reupload", ...],
    "default_mode": "fix", "suggestion": {"confidence", "summary",
    "changes": [{"from", "to"}]} | null, "fix": {"note", "fields":
    [{"key","label","value","type","options","placeholder","width"}]}
    | null, "reupload": {"target_file_role", "note"} | null}."""
    __tablename__ = "tally_review_items"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("tally_runs.id"))
    generation_id = Column(Integer, ForeignKey("tally_generations.id"), nullable=True)
    stage = Column(String, default="final")      # "input" | "final"
    severity = Column(String, default="warning")   # "error" | "warning"
    affected_row_ids = Column(Text, nullable=True)   # comma-separated TallyOutputRow ids; blank = not row-specific
    source_label = Column(String, nullable=True)     # e.g. "Amazon MTR - B2C"
    location_label = Column(String, nullable=True)   # e.g. "4 rows - column 'Sku'"
    title = Column(Text)
    body = Column(Text, nullable=True)
    detail = Column(Text, nullable=True)     # JSON - see class docstring
    rule_id = Column(Integer, ForeignKey("tally_rules.id"), nullable=True)
    rule_name = Column(String, nullable=True)    # denormalized, see PendingAction.connector_name in the Console
    trigger_reason = Column(String)      # "unmapped_sku" | "no_matching_rule" | "missing_required_field" | "rule_escalation"
    status = Column(String, default="pending")   # "pending" | "answered" | "dismissed"
    resolution_mode = Column(String, nullable=True)   # "suggest" | "fix" | "reupload" - which mode the human actually used
    answer_value = Column(Text, nullable=True)   # JSON of the fix field values, or the accepted suggestion's summary
    answered_by = Column(String, nullable=True)
    answered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Event(Base):
    """Lightweight audit log - stand-in for a real event bus, same role
    as the Console's Event model but local to this standalone app.
    generation_id, when set, is what the frontend polls to animate the
    "agent run" log step by step while a generation is processing."""
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    generation_id = Column(Integer, ForeignKey("tally_generations.id"), nullable=True)
    module = Column(String)
    status = Column(String)      # "info" | "success" | "error"
    message = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
