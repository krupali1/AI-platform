"""
Deterministic, non-AI integrity checks run against a built Tally
generation - a second, independent verification pass on top of the
row-level flagging generate_output() already does (missing fields,
unmapped SKUs, batch shortfalls), aimed at the kind of systemic issue
no single row can reveal on its own: a row silently dropped somewhere
in the pipeline, a line item double-counted, tax math that doesn't
add up, or the same product carrying two different HSN codes because
of a Master File or SKU-mapping inconsistency.

Every check here either re-derives its own answer independently
(row_count_conservation re-parses the source files and re-applies the
row filter itself, rather than trusting generate_output()'s own
counters) or re-verifies a property of the generation's own saved
output that a per-row pass has no reason to check (duplicates, tax
exclusivity, cross-row consistency). Nothing here mutates any data.

Surfaced at GET /api/tally-generations/{id}/qa-report. Deliberately
informational, not a new blocking gate - the existing approval
workflow (submit-for-approval / approve / send-back) stays the actual
release authority; this exists so nothing reaches that Approver, or a
download, without a second, automated pair of eyes on it first.
"""
import json
from collections import defaultdict

import tally_rules
from tally_pipeline import (
    parse_uploads, _mappings_index, _platforms_by_slug, _rule_dicts,
    _ledger_config_dict, resolve_field_mapping, _dedupe_rows,
)
from models import TallyOutputRow

CLEARED_STATUSES = ("ok", "resolved")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _check_row_count_conservation(session, run, generation, rows):
    """Independently re-derives how many rows should exist for this
    slice (parse -> dedupe -> slice filter -> row filter) using the
    same building blocks generate_output() itself calls, but through
    this separate path rather than trusting its counters. Combo
    unbundling and batch splitting only ever grow the row count from
    here, so the real output can never be LESS than this floor -
    a shortfall means something was silently dropped further down the
    pipeline (SKU resolution, batch allocation, rule application) that
    a per-row check can't see, because the row was never created for
    it to look at."""
    files, parsed_by_file = parse_uploads(session, run)
    mappings_index = _mappings_index(session)
    platforms_by_slug = _platforms_by_slug(session)
    rules = _rule_dicts(session)
    ledger_config = _ledger_config_dict(session)

    expected = resolve_field_mapping(files, parsed_by_file, mappings_index, platforms_by_slug)
    expected = _dedupe_rows(expected)
    if generation.order_type:
        expected = [(p, ref, f) for p, ref, f in expected if (f.get("order_type") or "") == generation.order_type]
    if generation.location:
        expected = [(p, ref, f) for p, ref, f in expected if (f.get("godown") or "") == generation.location]
    expected = [(p, ref, f) for p, ref, f in expected if tally_rules.passes_row_filter(f, rules, ledger_config)]

    expected_min = len(expected)
    actual = len(rows)
    if actual < expected_min:
        return {"key": "row_conservation", "label": "No rows silently dropped", "status": "fail",
                "detail": f"{expected_min} source row(s) matched this slice and the row filter, but only {actual} output row(s) exist - up to {expected_min - actual} may have been lost somewhere in SKU resolution, batch allocation, or rule application.",
                "affected_count": expected_min - actual, "affected_row_ids": [], "fix_fields": [], "suggest_regenerate": True}
    return {"key": "row_conservation", "label": "No rows silently dropped", "status": "pass",
            "detail": f"{actual} output row(s) - at least {expected_min} expected from the source files.",
            "affected_count": 0, "affected_row_ids": [], "fix_fields": [], "suggest_regenerate": False}


def _check_duplicate_rows(rows):
    """A repeated source_row_ref among cleared rows means the pipeline
    wrote the same input row to the output more than once - unambiguous
    double-counting, whatever product or order it's for.

    Deliberately keyed on (source_row_ref, order_id, sku) rather than
    reconstructed from (order, product, batch, ...) fields alone. Two
    earlier versions of this check tried simpler keys and both
    produced false positives on real PIL data: keying on the raw
    source SKU flagged every combo pack (its components fan out into
    several rows that all share one raw SKU - confirmed against SKU
    "ASNG_C", which correctly and legitimately unbundles into two
    different products); keying on the resolved product still flagged
    two genuinely separate purchases of the same product within one
    order (e.g. a combo containing Acneguard Soap *and* a separate
    3-pack of the same soap in the same order - two real source rows,
    two real sales, same product). source_row_ref alone isn't safe
    either: it's built as just "<sheet>:row<n>" (see
    resolve_field_mapping in tally_pipeline.py), with no file
    identifier, so a run with two separate uploaded sales files (say a
    B2B and a B2C report, both sheet "Sheet1") gets a genuine
    collision - "Sheet1:row1" from each file - that this check would
    otherwise misread as the same row processed twice. Pairing it with
    order_id and sku closes that gap: a real double-processing bug
    reproduces the exact same ref *and* the exact same order/SKU, which
    two unrelated rows from different files coincidentally sharing a
    row number essentially never do."""
    seen = defaultdict(list)
    for r in rows:
        if r.status not in CLEARED_STATUSES:
            continue
        seen[(r.source_row_ref, r.order_id, r.sku)].append(r.id)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    count = sum(len(v) for v in dupes.values())
    # For each duplicate group, keep the first row and mark the rest as
    # the ones to exclude - excluding all of them would zero out a real
    # sale, excluding none would leave the double-count in place.
    exclude_ids = [row_id for ids in dupes.values() for row_id in ids[1:]]
    if dupes:
        return {"key": "duplicate_rows", "label": "No duplicate line items", "status": "fail",
                "detail": f"{len(dupes)} source row(s) were written to the output more than once ({count} row(s) total) - would double-count that sale in the final sheet.",
                "affected_count": count, "affected_row_ids": exclude_ids, "fix_fields": [], "suggest_exclude": True}
    return {"key": "duplicate_rows", "label": "No duplicate line items", "status": "pass", "detail": "No duplicates found among cleared rows.",
            "affected_count": 0, "affected_row_ids": [], "fix_fields": [], "suggest_exclude": False}


def _check_tax_exclusivity(rows):
    """CGST+SGST (intra-state) and IGST (inter-state) should never
    both be nonzero on the same row - a real sale is taxed one way or
    the other, never both. Seeing both charged points at a tax_split
    misconfiguration, not a real transaction."""
    bad = []
    for r in rows:
        d = json.loads(r.data)
        cgst_sgst = _num(d.get("cgst_amount")) + _num(d.get("sgst_amount"))
        igst = _num(d.get("igst_amount"))
        if cgst_sgst > 0.01 and igst > 0.01:
            bad.append(r.id)
    fix_fields = [{"key": "cgst_amount", "label": "CGST"}, {"key": "sgst_amount", "label": "SGST"}, {"key": "igst_amount", "label": "IGST"}]
    if bad:
        return {"key": "tax_exclusivity", "label": "CGST+SGST and IGST never both charged", "status": "fail",
                "detail": f"{len(bad)} row(s) have both CGST/SGST and IGST charged at once - check the tax_split rule for this run.",
                "affected_count": len(bad), "affected_row_ids": bad, "fix_fields": fix_fields}
    return {"key": "tax_exclusivity", "label": "CGST+SGST and IGST never both charged", "status": "pass", "detail": "Consistent on every row.",
            "affected_count": 0, "affected_row_ids": [], "fix_fields": fix_fields}


def _check_hsn_gst_consistency(rows):
    """The same internal product should carry exactly one HSN code and
    one GST% across the whole sheet - seeing two means either the
    Master File has conflicting entries for it, or two source rows
    that shouldn't have resolved to the same product did.

    affected_row_ids deliberately lists only the minority outlier
    rows (whichever HSN/GST value is NOT the product's most common
    one), not every row for a conflicted product - a product sold 50
    times at the right HSN and once at a typo'd one has 1 row worth
    fixing, not 51."""
    hsn_counts = defaultdict(lambda: defaultdict(int))
    gst_counts = defaultdict(lambda: defaultdict(int))
    row_values = {}   # row.id -> (product, hsn, gst)
    for r in rows:
        d = json.loads(r.data)
        code = d.get("internal_product_code")
        if not code:
            continue
        hsn = (d.get("hsn_code") or "").strip()
        gst = d.get("gst_percent")
        gst_val = round(_num(gst), 2) if gst not in (None, "") else None
        row_values[r.id] = (code, hsn, gst_val)
        if hsn:
            hsn_counts[code][hsn] += 1
        if gst_val is not None:
            gst_counts[code][gst_val] += 1

    def majority(counts_by_key, product):
        counts = counts_by_key.get(product) or {}
        return max(counts, key=counts.get) if counts else None

    hsn_conflicts = {p: c for p, c in hsn_counts.items() if len(c) > 1}
    gst_conflicts = {p: c for p, c in gst_counts.items() if len(c) > 1}
    conflicted_products = set(hsn_conflicts) | set(gst_conflicts)

    affected_row_ids = []
    for row_id, (code, hsn, gst_val) in row_values.items():
        if code not in conflicted_products:
            continue
        hsn_off = code in hsn_conflicts and hsn and hsn != majority(hsn_counts, code)
        gst_off = code in gst_conflicts and gst_val is not None and gst_val != majority(gst_counts, code)
        if hsn_off or gst_off:
            affected_row_ids.append(row_id)

    fix_fields = [{"key": "hsn_code", "label": "HSN Code"}, {"key": "gst_percent", "label": "GST%"}]
    total = len(hsn_conflicts) + len(gst_conflicts)
    if total:
        parts = []
        if hsn_conflicts:
            parts.append(f"{len(hsn_conflicts)} product(s) with more than one HSN code")
        if gst_conflicts:
            parts.append(f"{len(gst_conflicts)} product(s) with more than one GST%")
        return {"key": "hsn_gst_consistency", "label": "Each product has one HSN code and GST%", "status": "warn",
                "detail": ", ".join(parts) + f" in this sheet ({len(affected_row_ids)} outlier row(s)).",
                "affected_count": len(affected_row_ids), "affected_row_ids": affected_row_ids, "fix_fields": fix_fields}
    return {"key": "hsn_gst_consistency", "label": "Each product has one HSN code and GST%", "status": "pass", "detail": "Consistent for every product.",
            "affected_count": 0, "affected_row_ids": [], "fix_fields": fix_fields}


def _check_negative_values(rows):
    """A negative quantity/rate/taxable value is legitimate on a
    Refund line (a credit) but not otherwise - flags a sign error or
    source-data problem outside that one expected case."""
    bad = []
    for r in rows:
        d = json.loads(r.data)
        if d.get("transaction_type") == "Refund":
            continue
        if any(_num(d.get(k)) < 0 for k in ("quantity", "rate", "taxable_value")):
            bad.append(r.id)
    fix_fields = [{"key": "quantity", "label": "Quantity"}, {"key": "rate", "label": "Rate"}, {"key": "taxable_value", "label": "Taxable Value"}]
    if bad:
        return {"key": "negative_values", "label": "No unexpected negative values", "status": "warn",
                "detail": f"{len(bad)} non-Refund row(s) have a negative quantity, rate, or taxable value.",
                "affected_count": len(bad), "affected_row_ids": bad, "fix_fields": fix_fields}
    return {"key": "negative_values", "label": "No unexpected negative values", "status": "pass", "detail": "Nothing negative outside Refund rows.",
            "affected_count": 0, "affected_row_ids": [], "fix_fields": fix_fields}


def _check_zero_value_cleared_rows(rows):
    """A row marked clear (nothing pending) with a taxable value of
    exactly zero could be a legitimate free item, but is also the
    signature of a missing sale-price mapping slipping through - worth
    a human glance either way, so this warns rather than fails."""
    bad = [r.id for r in rows if r.status in CLEARED_STATUSES and _num(json.loads(r.data).get("taxable_value")) == 0]
    fix_fields = [{"key": "taxable_value", "label": "Taxable Value"}, {"key": "rate", "label": "Rate"}]
    if bad:
        return {"key": "zero_value_rows", "label": "No zero-value cleared rows", "status": "warn",
                "detail": f"{len(bad)} row(s) marked clear have a taxable value of exactly 0 - worth a glance before this goes out.",
                "affected_count": len(bad), "affected_row_ids": bad, "fix_fields": fix_fields}
    return {"key": "zero_value_rows", "label": "No zero-value cleared rows", "status": "pass", "detail": "Every cleared row has a nonzero taxable value.",
            "affected_count": 0, "affected_row_ids": [], "fix_fields": fix_fields}


def run_qa_checks(session, run, generation):
    """Returns a list of check result dicts:
    {key, label, status: "pass"|"warn"|"fail", detail, affected_count,
    affected_row_ids, fix_fields}. affected_row_ids are real
    TallyOutputRow ids the caller can act on directly with the
    existing PATCH .../output-rows/{id} (override) and POST
    .../output-rows/{id}/exclude endpoints - fix_fields names which
    of that row's fields are the ones this check actually cares about,
    for a UI that wants to show a tight fix form instead of the whole
    row. "fail" = a real integrity problem (lost/duplicated rows,
    impossible tax combination). "warn" = worth a human glance but not
    proof of a bug (a real free-sample row, a real cross-batch product
    rename)."""
    rows = session.query(TallyOutputRow).filter_by(generation_id=generation.id).all()
    return [
        _check_row_count_conservation(session, run, generation, rows),
        _check_duplicate_rows(rows),
        _check_tax_exclusivity(rows),
        _check_hsn_gst_consistency(rows),
        _check_negative_values(rows),
        _check_zero_value_cleared_rows(rows),
    ]
