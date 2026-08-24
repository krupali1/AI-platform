"""
The deterministic half of the hybrid rules engine. evaluate_rules()
takes plain dicts (no DB/session access) so it's easy to exercise
directly in a test script, and is the only place condition/action
logic is interpreted - the pipeline never branches on a specific rule,
only on rule_group.

suggest_rule() and suggest_rules_from_document() are the AI-assisted
half: they propose rule shapes for a human to review and save via the
normal rule-create endpoint. Neither ever writes a TallyRule itself,
and neither is ever called from the deterministic pipeline - both are
separate, on-demand, opt-in helpers.
"""
import os
import json

MANDATORY_GROUPS = ("voucher_type", "ledger_mapping")

# tax_split is conditionally mandatory, not blanket-mandatory like the
# groups above: a row that already reports its own
# cgst_rate/sgst_rate/utgst_rate/igst_rate (Amazon's MTR does) needs no
# tax_split rule at all - tally_pipeline.compute_totals derives
# CGST/SGST/IGST straight from those rates. tax_split only matters as
# the fallback for a row with just a lump-sum tax_amount_reported and
# no rate columns, so it's only required to match in that case - see
# has_rate_columns() below.
RATE_COLUMN_KEYS = ("cgst_rate", "sgst_rate", "utgst_rate", "igst_rate")


def has_rate_columns(row_fields):
    """Whether this row was actually mapped from rate columns at all -
    checked by key presence, not by value, so a genuinely nil-rated
    line (every rate legitimately 0%, a real occurrence - confirmed
    against real PIL data for an exempt product) still takes the
    rate-based path instead of being misread as "no rate columns" and
    falling through to the lump-sum fallback, which would then demand
    a tax_split rule that was never meant to apply to it."""
    return any(k in row_fields for k in RATE_COLUMN_KEYS)

# row_filter is evaluated separately, before MANDATORY_GROUPS even sees a
# row - a row that fails it is excluded outright, not escalated. Kept out
# of MANDATORY_GROUPS/evaluate_rules on purpose: "no rule matched" means
# something different here (deliberately not included) than it does for
# tax_split/voucher_type/ledger_mapping (missing config, needs a human).
ROW_FILTER_GROUP = "row_filter"

RULE_GROUPS = {
    "row_filter": "Row filter - which transactions to include at all (e.g. Transaction Type)",
    "escalation": "Escalation - flag a row for human review",
    "tax_split": "Tax split - CGST+SGST vs IGST",
    "voucher_type": "Voucher type - which Tally voucher type to use",
    "ledger_mapping": "Ledger mapping - which sales ledger to post to",
}

CONDITION_OPERATORS = ["equals", "not_equals", "in", "not_in", "exists", "not_exists", "contains"]

ACTION_TYPES = ["set_field", "escalate", "split_tax", "use_ledger", "use_voucher_type", "include_row", "exclude_row"]

# godown is deliberately not settable by any rule - it must always equal
# the platform's own Warehouse ID column, verbatim, so a row's warehouse
# is never silently reassigned by config. Enforced here (checked in
# main.py's rule validation) rather than left as a convention.
FORBIDDEN_ACTION_FIELDS = {"godown"}

# A rules document can be long (a full SOP, an email thread pasted into
# Word); capped rather than sent whole so one oversized upload can't
# blow the request budget - the truncation itself is disclosed in the
# prompt so Claude doesn't silently treat a cut-off document as complete.
MAX_DOCUMENT_CHARS = 15000

RULE_PAYLOAD_KEYS = (
    "rule_group", "order_index", "condition_field", "condition_operator",
    "condition_value", "action_type", "action_field", "action_value", "description",
)


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _condition_matches(row_fields, rule, ledger_config):
    op = rule.get("condition_operator")
    value = row_fields.get(rule.get("condition_field"))
    value_s = "" if value in (None, "nan") else str(value).strip()

    target = rule.get("condition_value") or ""
    if target.startswith("$"):
        target = ledger_config.get(target[1:], "")

    if op == "exists":
        return value_s != ""
    if op == "not_exists":
        return value_s == ""
    if op == "equals":
        return value_s.lower() == str(target).strip().lower()
    if op == "not_equals":
        return value_s.lower() != str(target).strip().lower()
    if op == "in":
        options = [o.strip().lower() for o in target.split(",") if o.strip()]
        return value_s.lower() in options
    if op == "not_in":
        options = [o.strip().lower() for o in target.split(",") if o.strip()]
        return value_s.lower() not in options
    if op == "contains":
        return str(target).strip().lower() in value_s.lower()
    return False


def _resolve_config_or_literal(value, ledger_config):
    if value is None:
        return None
    return ledger_config.get(value, value)


def _apply_action(rule, resolved, ledger_config):
    action_type = rule.get("action_type")
    if action_type == "set_field" and rule.get("action_field"):
        resolved[rule["action_field"]] = _resolve_config_or_literal(rule.get("action_value"), ledger_config)
    elif action_type == "split_tax":
        resolved["tax_split_mode"] = rule.get("action_value")   # "cgst_sgst" | "igst"
    elif action_type == "use_ledger":
        resolved[rule.get("action_field") or "sales_ledger"] = _resolve_config_or_literal(rule.get("action_value"), ledger_config)
    elif action_type == "use_voucher_type":
        resolved["voucher_type"] = _resolve_config_or_literal(rule.get("action_value"), ledger_config)


def passes_row_filter(row_fields, rules, ledger_config):
    """Row-level include/exclude, evaluated once per row before
    anything else in the pipeline. Two independent lists within the
    "row_filter" group:
      - any matching "exclude_row" rule excludes the row outright.
      - if at least one "include_row" rule is configured, the row must
        match one of them to survive (an allow-list, e.g. PRD's
        Transaction Type in Shipment/Refund/Free replacement); if none
        are configured, every row passes by default (no filter set up
        yet = no filtering, so this stays backward compatible for a
        platform that hasn't configured one).
    A row that fails this is simply not in the output at all - it is
    never counted, flagged, or escalated, unlike a mandatory-group miss
    in evaluate_rules() below."""
    group_rules = [r for r in rules if r.get("is_active", True) and r.get("rule_group") == ROW_FILTER_GROUP]
    exclude_rules = [r for r in group_rules if r.get("action_type") == "exclude_row"]
    include_rules = [r for r in group_rules if r.get("action_type") == "include_row"]

    for r in exclude_rules:
        if _condition_matches(row_fields, r, ledger_config):
            return False
    if include_rules:
        return any(_condition_matches(row_fields, r, ledger_config) for r in include_rules)
    return True


def evaluate_rules(row_fields, rules, ledger_config):
    """rules: list of plain dicts shaped like TallyRule columns.
    Returns (resolved_fields, escalations, applied_rule_ids).

    Every rule_group in MANDATORY_GROUPS must have at least one
    matching active rule for a row to resolve cleanly - a row with no
    match anywhere in a mandatory group is itself an escalation
    (trigger_reason "no_matching_rule"), never a silent default.
    "escalation"-group rules are evaluated on top of that, for edge
    cases the deterministic fields alone can't express (e.g. "quantity
    is zero or negative").

    Unlike the mandatory-group gate itself, action application is NOT
    first-match-only: every matching rule in a group applies its
    action, in ascending order_index. This lets one group carry several
    independent set_field/use_ledger actions that all key off the same
    condition (e.g. "order_type == B2C" setting both sales_ledger and
    party_ledger) without them shadowing each other - later rules
    overwrite earlier ones only if they actually target the same
    field, which is the intuitive "this rule refines/overrides that
    one" behavior of an ordered rule list."""
    resolved = {}
    escalations = []
    applied_rule_ids = []

    by_group = {}
    for r in rules:
        if not r.get("is_active", True):
            continue
        by_group.setdefault(r["rule_group"], []).append(r)
    for group in by_group:
        by_group[group].sort(key=lambda r: r.get("order_index") or 0)

    def _require_group_match(group):
        group_rules = by_group.get(group, [])
        matched_rules = [r for r in group_rules if _condition_matches(row_fields, r, ledger_config)]
        if not matched_rules:
            escalations.append({
                "trigger_reason": "no_matching_rule",
                "rule_id": None,
                "rule_name": None,
                "message": f'No active "{RULE_GROUPS.get(group, group)}" rule matched this row.',
            })
            return
        for r in matched_rules:
            applied_rule_ids.append(r["id"])
            _apply_action(r, resolved, ledger_config)

    for group in MANDATORY_GROUPS:
        _require_group_match(group)
    if not has_rate_columns(row_fields):
        _require_group_match("tax_split")

    for r in by_group.get("escalation", []):
        if _condition_matches(row_fields, r, ledger_config):
            applied_rule_ids.append(r["id"])
            escalations.append({
                "trigger_reason": "rule_escalation",
                "rule_id": r["id"],
                "rule_name": r.get("description") or f"Rule #{r['id']}",
                "message": r.get("description") or "Flagged by an escalation rule.",
            })

    return resolved, escalations, applied_rule_ids


def suggest_rule(context, canonical_fields=None):
    """Calls Claude to propose a rule shape from a plain-language
    description of an ambiguous case. Returns a dict shaped like the
    rule-create payload, plus a "demo" flag when no API key is
    configured - the caller must never treat this as a saved rule."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "demo": True,
            "rule_group": "escalation",
            "order_index": 0,
            "condition_field": "sku_in_master",
            "condition_operator": "equals",
            "condition_value": "false",
            "action_type": "escalate",
            "action_field": None,
            "action_value": None,
            "description": "Demo suggestion (no ANTHROPIC_API_KEY configured) - describe your case and set a real key to get a tailored suggestion. Review before saving.",
        }

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    field_list = ", ".join(canonical_fields or [])
    prompt = f"""You are helping configure a deterministic rules engine that maps Amazon marketplace sales rows to Tally accounting entries. A rule is: IF condition_field condition_operator condition_value THEN action_type (with action_field/action_value).

Valid rule_group values: {", ".join(RULE_GROUPS)}
Valid condition_operator values: {", ".join(CONDITION_OPERATORS)}
Valid action_type values: {", ".join(ACTION_TYPES)}
Known canonical fields you can use as condition_field or action_field: {field_list}

A user described this case that isn't currently handled:
\"\"\"{context}\"\"\"

Propose ONE rule as JSON with exactly these keys: rule_group, order_index (0), condition_field, condition_operator, condition_value, action_type, action_field, action_value, description (a short plain-English label). Respond with ONLY the JSON object, no other text."""

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fences(resp.content[0].text))
    parsed["demo"] = False
    return parsed


def suggest_rules_from_document(document_text, canonical_fields=None):
    """Calls Claude to extract every distinct rule/condition it can
    find in a whole document (an SOP, a policy note, an email thread
    pasted into Word) rather than the single case suggest_rule()
    handles. Returns a list of dicts, each shaped like the rule-create
    payload plus a "demo" flag - same "AI only ever proposes" contract
    as suggest_rule(): nothing here is saved until a human reviews each
    one and calls the normal create-rule endpoint themselves."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return [
            {
                "demo": True,
                "rule_group": "tax_split",
                "order_index": 0,
                "condition_field": "ship_to_state",
                "condition_operator": "equals",
                "condition_value": "$company_state",
                "action_type": "split_tax",
                "action_field": None,
                "action_value": "cgst_sgst",
                "description": "Demo suggestion (no ANTHROPIC_API_KEY configured) - upload a real document and set a key to extract your own rules. Review before saving.",
            },
            {
                "demo": True,
                "rule_group": "escalation",
                "order_index": 0,
                "condition_field": "sku_in_master",
                "condition_operator": "equals",
                "condition_value": "false",
                "action_type": "escalate",
                "action_field": None,
                "action_value": None,
                "description": "Demo suggestion (no ANTHROPIC_API_KEY configured) - upload a real document and set a key to extract your own rules. Review before saving.",
            },
        ]

    import anthropic

    truncated = len(document_text) > MAX_DOCUMENT_CHARS
    text = document_text[:MAX_DOCUMENT_CHARS]

    client = anthropic.Anthropic(api_key=api_key)
    field_list = ", ".join(canonical_fields or [])
    prompt = f"""You are helping configure a deterministic rules engine that maps Amazon marketplace sales rows to Tally accounting entries. A rule is: IF condition_field condition_operator condition_value THEN action_type (with action_field/action_value).

Valid rule_group values: {", ".join(RULE_GROUPS)}
Valid condition_operator values: {", ".join(CONDITION_OPERATORS)}
Valid action_type values: {", ".join(ACTION_TYPES)}
Known canonical fields you can use as condition_field or action_field: {field_list}

A user uploaded this document describing rules/conditions for how their sales data should be handled{" (truncated to the first "  + str(MAX_DOCUMENT_CHARS) + " characters)" if truncated else ""}:
\"\"\"{text}\"\"\"

Find every distinct rule or condition actually stated or clearly implied in the document - do not invent ones that aren't there, and do not merge two genuinely different conditions into one rule. Propose each as a JSON object with exactly these keys: rule_group, order_index (0), condition_field, condition_operator, condition_value, action_type, action_field, action_value, description (a short plain-English label quoting or closely paraphrasing the source). Respond with ONLY a JSON array of these objects - an empty array [] if the document states no rule your schema can express. No other text."""

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fences(resp.content[0].text))
    if not isinstance(parsed, list):
        raise ValueError("Expected a JSON array of rules from the model")
    out = []
    for item in parsed:
        rule = {k: item.get(k) for k in RULE_PAYLOAD_KEYS}
        rule["order_index"] = rule.get("order_index") or 0
        rule["demo"] = False
        out.append(rule)
    return out


MAPPING_PAYLOAD_KEYS = ("target_field", "source_file_role", "order_type", "source_column_name", "constant_value", "description")

# Fields the pipeline itself computes (totals, lookups, internal codes) -
# there is never a literal source column to map these FROM, only an
# output ("sample_tally") column to put them IN. Kept as a constant here
# rather than trusting the model to infer it every time, since getting
# this wrong would propose a mapping that can never actually be filled.
_COMPUTED_FIELD_KEYS = {
    "platform", "order_type", "internal_product_code", "sku_in_master", "taxable_value",
    "rate", "gst_percent", "cgst_amount", "sgst_amount", "igst_amount", "total_tax",
    "invoice_total", "voucher_type", "sales_ledger", "party_ledger", "party_name", "narration",
}


def _demo_field_mapping_suggestions():
    note = "Demo suggestion (no ANTHROPIC_API_KEY configured) - upload a real document and set a key to extract your own mappings. Review before saving."
    return {
        "demo": True,
        "mappings": [
            {"target_field": "order_id", "source_file_role": "sales", "order_type": "", "source_column_name": "Order Id", "constant_value": "", "description": note},
            {"target_field": "order_id", "source_file_role": "sample_tally", "order_type": "", "source_column_name": "Orderid", "constant_value": "", "description": note},
        ],
        "flagged": [
            {"label": "Demo mode", "reason": note},
        ],
    }


def suggest_field_mappings_from_document(document_text, canonical_fields, platform_slug=None):
    """Calls Claude to read a column-mapping spec (a spreadsheet or doc
    shaped like 'Tally column | Source sheet | Source column', e.g. a
    PRD's field table) and propose TallyFieldMapping rows - both the
    input side (which uploaded file's column feeds a canonical field)
    and the output side (which canonical field fills which column of
    the sample Tally sheet). Same "AI only ever proposes" contract as
    the rule-suggesting functions above: nothing is saved until a human
    reviews each one and calls the normal mapping-upsert endpoint.

    canonical_fields: the full list of dicts from tally_pipeline.CANONICAL_FIELDS
    (key/label/group), not just keys - the model needs the labels to
    match a document's human-readable column names like "Billing_Pincode"
    to the right canonical key, and the group to know which fields are
    pipeline-computed (see _COMPUTED_FIELD_KEYS) rather than mappable
    from a literal source column.

    Deliberately refuses to guess two kinds of things, surfacing them
    in "flagged" instead: anything that's a conditional/exception/lookup
    (e.g. "Godown = Warehouse ID, except if Invoice Number starts with
    IN-") belongs in the Rules feature, not a field mapping; anything
    this build doesn't implement yet (batch/FIFO allocation columns) has
    nowhere real to map to."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _demo_field_mapping_suggestions()

    import anthropic

    truncated = len(document_text) > MAX_DOCUMENT_CHARS
    text = document_text[:MAX_DOCUMENT_CHARS]

    client = anthropic.Anthropic(api_key=api_key)
    field_list = "\n".join(f"- {f['key']} ({f['group']}): {f['label']}" for f in (canonical_fields or []))
    computed_list = ", ".join(sorted(_COMPUTED_FIELD_KEYS))
    platform_note = f'This is being configured for the platform "{platform_slug}" - use that as the mapping\'s platform for any "sales" role suggestion.' if platform_slug else "No specific platform was given - propose \"sales\" mappings without worrying about which platform, the human will assign it."

    prompt = f"""You are helping configure field mapping for a pipeline that turns marketplace sales reports into Tally accounting entries. A field mapping row says either:
(a) INPUT: canonical field X is read from column "Y" in an uploaded file of role "sales" (a sales report) or "master" (the SKU/product master file), or is a fixed constant value applied to every row instead of a column, or
(b) OUTPUT: canonical field X should fill the output column named "Y" of the sample Tally sheet (source_file_role = "sample_tally").

Known canonical fields (key (group): label) - a field in group "computed" is worked out by the pipeline itself (a total, a lookup, an internal formula) and can NEVER be the target of an INPUT mapping (there is no literal column to read it from) - it can only be the target of an OUTPUT ("sample_tally") mapping:
{field_list}

Computed field keys, for quick reference: {computed_list}

{platform_note}

A user uploaded this column-mapping specification{" (truncated to the first " + str(MAX_DOCUMENT_CHARS) + " characters)" if truncated else ""}:
\"\"\"{text}\"\"\"

For each row/entry in the document that names an actual output column and where it comes from:
1. Identify the canonical field key it corresponds to (best match from the list above - skip the entry entirely if nothing reasonably matches).
2. If the document gives an output column name for it (e.g. a "Tally sheet"/output-column column in a table), propose ONE mapping with source_file_role "sample_tally", source_column_name = that exact output column name, constant_value "".
3. If the document also states a literal source column feeding it from an uploaded sales report or master file, AND the canonical field is NOT in the computed list above, propose an additional mapping with source_file_role "sales" or "master" as appropriate, source_column_name = that exact source column name, constant_value "". If the document states the source column differs by order type (e.g. "Bill to X" for B2B vs "Ship to X" for B2C), propose one such mapping per order type (order_type = "B2B", "B2C", etc.) instead of one general one.
4. If the document says a field is always a fixed literal value rather than read from any column (e.g. "Courier = 'Amazon' by default"), propose a mapping with constant_value set to that literal and source_column_name "" instead - for BOTH the sample_tally output mapping and, if source_file_role sales/master would otherwise apply, that too.
5. Do NOT propose an input (sales/master) mapping for anything in the computed list, even if the document shows a formula for it (e.g. "RATE = Tax exclusive gross / Quantity") - a sample_tally output mapping for it is still correct and expected.
6. Do NOT invent a mapping for anything that is actually a conditional override, exception, or a lookup-table depending on another field's value (e.g. "if Invoice Number starts with IN-, Warehouse ID = IN", "Party Name depends on Godown's value") - these are business rules, not column mappings. Instead add ONE entry to "flagged" per such case with a short label and a one-sentence reason explaining it belongs in the rules feature instead.
7. Do NOT invent a mapping for anything this system doesn't have a place for yet - specifically batch numbers, manufacturing dates, or expiry dates (FIFO batch allocation). Add these to "flagged" instead, noting they aren't built yet.
8. Never invent a source or output column name that isn't actually stated in the document.

Respond with ONLY a JSON object with exactly two keys:
"mappings": a JSON array of objects, each with exactly these keys: target_field, source_file_role, order_type (empty string "" if not order-type-specific), source_column_name (empty string "" if using constant_value instead), constant_value (empty string "" if using source_column_name instead), description (a short plain-English label quoting or closely paraphrasing the source).
"flagged": a JSON array of objects, each with exactly these keys: label, reason.
No other text."""

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fences(resp.content[0].text))
    if not isinstance(parsed, dict) or "mappings" not in parsed:
        raise ValueError("Expected a JSON object with a 'mappings' key from the model")
    mappings = []
    for item in parsed.get("mappings") or []:
        m = {k: (item.get(k) or "") for k in MAPPING_PAYLOAD_KEYS}
        mappings.append(m)
    flagged = []
    for item in parsed.get("flagged") or []:
        flagged.append({"label": item.get("label") or "", "reason": item.get("reason") or ""})
    return {"demo": False, "mappings": mappings, "flagged": flagged}
