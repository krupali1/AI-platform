"""
Orchestrator for the multi-platform -> Tally pipeline. Named, ordered
steps mirror a straight-line batch job: parse uploads, resolve field
mapping, resolve SKU mapping, apply rules, compute totals, validate,
finalize. Output generation itself lives in tally_output.py and is
called on-demand at download time, not part of this list.

A new platform (Flipkart, the company website, anything else) is pure
configuration, not a code change: a new TallyPlatform row (see
models.py) plus new TallyFieldMapping rows scoped to it - every step
below generalizes over file_role == "sales" and platform_slug, never
branching on which specific platform a file belongs to. A genuinely
new rule_group (a different country's tax regime, say) is the one
honest seam that *does* need a small change here and in
tally_rules.py - the same class of limit the Client Memory Console's
custom_engine.py documents for its own fixed READABLE_TYPES dict.

Scope note: inventory/stock-style reports aren't consumed by this
pipeline yet, only order-level sales rows. An inventory file can still
be uploaded via file_role "other" for now.
"""
import json
import re
import datetime
from collections import defaultdict

from models import TallyUploadedFile, TallyFieldMapping, TallyLedgerConfig, TallyRule, TallyPlatform, TallyGeneration, TallyOutputRow, TallyReviewItem, Event, TallySkuMaster
import tally_parsing
import tally_rules

MAX_ROWS = 20000

SALES_FILE_ROLE = "sales"
BATCH_FILE_ROLE = "batch_summary"

CANONICAL_FIELDS = [
    {"key": "order_id", "label": "Order ID", "group": "source"},
    {"key": "order_date", "label": "Order Date", "group": "source"},
    {"key": "transaction_type", "label": "Transaction Type (e.g. Shipment / Refund / Free replacement)", "group": "source"},
    {"key": "sku", "label": "SKU / Item Code", "group": "source"},
    {"key": "asin", "label": "ASIN / Platform Product ID", "group": "source"},
    {"key": "product_description", "label": "Product Description", "group": "source"},
    {"key": "quantity", "label": "Quantity", "group": "source"},
    {"key": "unit_price", "label": "Unit Price", "group": "source"},
    {"key": "gross_amount", "label": "Gross Amount", "group": "source"},
    {"key": "discount", "label": "Discount", "group": "source"},
    {"key": "shipping_charge", "label": "Shipping Charge", "group": "source"},
    {"key": "courier", "label": "Courier (usually a constant per platform, e.g. \"Amazon\")", "group": "source"},
    {"key": "units", "label": "Units (usually a constant, e.g. \"Pcs\")", "group": "source"},
    {"key": "freight", "label": "Freight (usually a constant, e.g. 0)", "group": "source"},
    {"key": "tax_amount_reported", "label": "Tax Amount, lump sum (fallback when rate columns aren't available)", "group": "source"},
    {"key": "cgst_rate", "label": "CGST Rate %", "group": "source"},
    {"key": "sgst_rate", "label": "SGST Rate %", "group": "source"},
    {"key": "utgst_rate", "label": "UTGST Rate % (folded into SGST on output - no separate Tally column)", "group": "source"},
    {"key": "igst_rate", "label": "IGST Rate %", "group": "source"},
    {"key": "ship_to_state", "label": "Ship-to State", "group": "source"},
    {"key": "ship_to_pincode", "label": "Ship-to Pincode", "group": "source"},
    {"key": "billing_pincode", "label": "Billing Pincode (Bill-to for B2B, Ship-to for B2C)", "group": "source"},
    {"key": "billing_city", "label": "Billing City (Bill-to for B2B, Ship-to for B2C)", "group": "source"},
    {"key": "billing_state", "label": "Billing State (Bill-to for B2B, Ship-to for B2C)", "group": "source"},
    {"key": "buyer_gstin", "label": "Buyer GSTIN (B2B)", "group": "source"},
    {"key": "invoice_number", "label": "Invoice Number", "group": "source"},
    {"key": "hsn_code", "label": "HSN Code", "group": "source"},
    {"key": "godown", "label": "Godown (Warehouse ID, verbatim - never rule-overridden)", "group": "source"},
    {"key": "cn_no", "label": "Credit Note No. (Refund/Return rows only)", "group": "source"},
    {"key": "cn_date", "label": "Credit Note Date (Refund/Return rows only)", "group": "source"},
    {"key": "master_sku_column", "label": "Master Sheet: SKU / Item Code column", "group": "master"},
    {"key": "master_code_column", "label": "Master Sheet: Internal Product Code column", "group": "master"},
    {"key": "master_qty_multiplier_column", "label": "Master Sheet: Quantity Multiplier column (optional, for combo packs)", "group": "master"},
    {"key": "batch_product_column", "label": "Batch Summary: Internal Product Code column", "group": "batch"},
    {"key": "batch_location_column", "label": "Batch Summary: Location / Godown column", "group": "batch"},
    {"key": "batch_no_column", "label": "Batch Summary: Batch No. column", "group": "batch"},
    {"key": "batch_qty_column", "label": "Batch Summary: Opening Quantity column", "group": "batch"},
    {"key": "batch_mfg_date_column", "label": "Batch Summary: MFG Date column (also used for FIFO order - earliest first)", "group": "batch"},
    {"key": "batch_exp_date_column", "label": "Batch Summary: EXP Date column", "group": "batch"},
    {"key": "platform", "label": "Platform (auto: which marketplace this row came from)", "group": "computed"},
    {"key": "order_type", "label": "Order Type (e.g. B2C / B2B / General, set per upload)", "group": "computed"},
    {"key": "internal_product_code", "label": "Internal Product Code", "group": "computed"},
    {"key": "sku_in_master", "label": "SKU Found in Master?", "group": "computed"},
    {"key": "taxable_value", "label": "Taxable Value", "group": "computed"},
    {"key": "rate", "label": "Rate (taxable value / quantity)", "group": "computed"},
    {"key": "gst_percent", "label": "GST %", "group": "computed"},
    {"key": "cgst_amount", "label": "CGST Amount", "group": "computed"},
    {"key": "sgst_amount", "label": "SGST Amount", "group": "computed"},
    {"key": "igst_amount", "label": "IGST Amount", "group": "computed"},
    {"key": "total_tax", "label": "Total Tax", "group": "computed"},
    {"key": "invoice_total", "label": "Invoice Total", "group": "computed"},
    {"key": "voucher_type", "label": "Voucher Type", "group": "computed"},
    {"key": "sales_ledger", "label": "Sales Ledger", "group": "computed"},
    {"key": "party_ledger", "label": "Party Ledger", "group": "computed"},
    {"key": "party_name", "label": "Party Name", "group": "computed"},
    {"key": "narration", "label": "Narration", "group": "computed"},
    {"key": "batch_no", "label": "Batch No. (FIFO-allocated from the Batch Summary upload)", "group": "computed"},
    {"key": "mfg_date", "label": "MFG Date (from the allocated batch)", "group": "computed"},
    {"key": "exp_date", "label": "EXP Date (from the allocated batch)", "group": "computed"},
]

# voucher_type/sales_ledger deliberately excluded - not part of the
# PRD's 23-column Sales Register, and PIL confirmed their real data
# has neither. A row is never flagged for lacking them; they only
# matter for the optional native Tally XML export.
REQUIRED_FOR_OK = ("order_id", "internal_product_code", "godown")

LEDGER_CONFIG_KEYS = [
    ("company_state", "Company's home state (for CGST+SGST vs IGST)"),
    ("cgst_ledger", "CGST ledger name"),
    ("sgst_ledger", "SGST ledger name"),
    ("igst_ledger", "IGST ledger name"),
    ("b2c_sales_ledger", "B2C sales ledger name"),
    ("b2b_sales_ledger", "B2B sales ledger name"),
    ("b2c_party_ledger", "B2C settlement / party ledger name"),
    ("b2b_party_ledger", "B2B settlement / party ledger name"),
    ("b2c_voucher_type", "B2C voucher type (default: Sales)"),
    ("b2b_voucher_type", "B2B voucher type (default: Sales)"),
    ("round_off_ledger", "Round-off ledger name"),
    ("combo_sku_pattern", "Combo/bundle SKU pattern (regex, first group = pack size). Default: PK[\\s_-]*(\\d+) - matches PK2, PK-3, PK_4, etc."),
]


def _log(session, module, status, message):
    session.add(Event(module=module, status=status, message=message))
    session.commit()


def _rule_dicts(session):
    rules = session.query(TallyRule).order_by(TallyRule.rule_group, TallyRule.order_index).all()
    return [{
        "id": r.id, "rule_group": r.rule_group, "order_index": r.order_index,
        "condition_field": r.condition_field, "condition_operator": r.condition_operator,
        "condition_value": r.condition_value, "action_type": r.action_type,
        "action_field": r.action_field, "action_value": r.action_value,
        "is_active": r.is_active, "description": r.description,
    } for r in rules]


def _ledger_config_dict(session):
    rows = session.query(TallyLedgerConfig).all()
    return {r.config_key: r.config_value for r in rows}


def _mappings_index(session):
    """Keyed by (source_file_role, platform_slug, order_type) - both
    platform_slug and order_type are None for every role except
    "sales". platform_slug distinguishes e.g. Amazon's column layout
    from Flipkart's; order_type further distinguishes a platform's own
    B2B report from its B2C report, since the two commonly carry
    different column names for the same canonical field (Bill-to vs
    Ship-to)."""
    rows = session.query(TallyFieldMapping).all()
    index = defaultdict(list)
    for m in rows:
        index[(m.source_file_role, m.platform_slug, m.order_type)].append(m)
    return index


def _platforms_by_slug(session):
    return {p.slug: p.display_name for p in session.query(TallyPlatform).all()}


def _parse_file(f: TallyUploadedFile):
    return tally_parsing.parse_excel_file(f.file_blob, f.content_type, f.original_filename)


def _normalize_col(s):
    return re.sub(r"[\s_.\-]+", "", str(s or "")).strip().lower()


def _normalize_product_notation(s):
    """_normalize_col plus a couple of pack-size notation quirks
    confirmed against real PIL data (Master File vs Tally's own Stock
    Summary product names): parentheses around a pack size are
    sometimes present, sometimes not ("(1X50 GM)" vs "1X50GM"); a
    redundant "1x" pack-count prefix is sometimes written, sometimes
    omitted (a bare product is definitionally the same as an explicit
    "1x" of it, so dropping it never conflates two different pack
    sizes); and "50gm" vs "50g" is the same unit abbreviated
    differently. Deliberately NOT more aggressive than this - it never
    touches a real quantity/multiplier digit (a genuine "3x" combo
    pack, or "100g" vs "125g", are left exactly as different as they
    are), since collapsing those would risk matching a sale to the
    wrong product's batch, not just fixing a spelling difference."""
    s = _normalize_col(s)
    s = s.replace("(", "").replace(")", "")
    s = re.sub(r"(?<!\d)1x(?=\d)", "", s)
    s = re.sub(r"(\d)gm\b", r"\1g", s)
    return s


# Confirmed by PIL - each group below is the SAME physical product
# under more than one name, not different products: the SKU was kept
# unchanged across a rename or a discontinued/replaced pack, so the
# Master File and Tally's own Stock Summary records disagree in text
# only, never in what they actually refer to. This is an explicit,
# named-pair override, not a general rule - _normalize_product_notation's
# own notation-only logic deliberately never guesses that two
# different quantities (e.g. "100g" vs "125g") are the same product,
# since that guess would be wrong for any pair PIL hasn't confirmed.
_CONFIRMED_PRODUCT_NAME_ALIAS_GROUPS = [
    ["SENSOPIL GEL 1X125G", "SENSOPIL GEL 1X100G", "SENSOPIL GEL 1X125 GM"],
    ["NEEM & ALOEVERA SOAP 100 GM", "DERMACARE NEEM & ALOEVERA SOAP 1X100G", "DERMACARE NEEM & ALOVERA SOAP 1X100G"],
    ["CALPIL PET SYRUP 1X100ML", "CALPIL PET FEED SUP. SYRUP 1X200ML"],
]


def _build_confirmed_product_name_aliases():
    aliases = {}
    for group in _CONFIRMED_PRODUCT_NAME_ALIAS_GROUPS:
        canonical = _normalize_product_notation(group[0])
        for name in group:
            aliases[_normalize_product_notation(name)] = canonical
    return aliases


_CONFIRMED_PRODUCT_NAME_ALIASES = _build_confirmed_product_name_aliases()


def _normalize_product_name(s):
    """_normalize_product_notation, then _CONFIRMED_PRODUCT_NAME_ALIASES -
    a short, explicit list of specific name pairs PIL has confirmed
    really are the same product (a rename or discontinued/replaced
    pack that kept its SKU unchanged), checked only after the generic
    notation normalization above has already run."""
    normalized = _normalize_product_notation(s)
    return _CONFIRMED_PRODUCT_NAME_ALIASES.get(normalized, normalized)


def _apply_mapping_to_sheet(parsed, mappings):
    """Restricts to sheets actually referenced by a mapping (falls back
    to the file's first sheet if no mapping names one) - Amazon reports
    often ship extra instruction/summary sheets that must not be read
    as data rows. A mapping with constant_value set (Courier = "Amazon",
    Units = "Pcs", ...) injects that literal into every row regardless
    of columns present, rather than reading from any column.

    A mapped column name is resolved against the sheet's real headers
    case/whitespace/punctuation-insensitively when there's no exact
    match - "Warehouse ID" still resolves against a real header of
    "Warehouse Id", "Bill to postal code" against "Bill To Postalcode".
    This is real-world necessary: a PRD-style spec sheet's transcribed
    column name and Amazon's actual export header routinely differ by
    exactly this much, and an exact-match-only lookup would otherwise
    silently leave the whole column blank with no error anywhere -
    confirmed against a real upload where Godown/Billing_* were 100%
    blank purely from this kind of mismatch. Never fuzzy-matches beyond
    that (no typo tolerance) - a genuinely absent column still resolves
    to nothing, same as before."""
    names = tally_parsing.sheet_names_of(parsed)
    if not names:
        return []
    column_mappings = [m for m in mappings if not m.constant_value]
    constant_mappings = [m for m in mappings if m.constant_value]
    sheet_names = {m.source_sheet_name for m in column_mappings if m.source_sheet_name} or {names[0]}
    canonical_rows = []
    for sheet in sheet_names:
        if sheet not in parsed:
            continue
        real_columns = tally_parsing.column_names_of(parsed, sheet)
        real_by_norm = {_normalize_col(c): c for c in real_columns}
        real_set = set(real_columns)
        col_map = {}
        for m in column_mappings:
            if m.source_sheet_name and m.source_sheet_name != sheet:
                continue
            mapped_name = m.source_column_name
            if mapped_name in real_set:
                col_map[mapped_name] = m.target_field
            else:
                real = real_by_norm.get(_normalize_col(mapped_name))
                if real:
                    col_map[real] = m.target_field
        for row_num, raw in tally_parsing.rows_of(parsed, sheet):
            canonical = {}
            for col, field in col_map.items():
                if col in raw:
                    canonical[field] = raw[col]
            for m in constant_mappings:
                canonical[m.target_field] = m.constant_value
            canonical_rows.append((f"{sheet}:row{row_num}", canonical))
    return canonical_rows


# Matches PK2, PK-3, PK_4, "PK 5", case-insensitively - confirmed against
# real PIL SKU data (AGFG-PK2, NHDG_PK3_DUP, HI_PK_2, ...) as the actual
# convention in use today, on a Master file whose own multiplier column is
# blank for the overwhelming majority of real combo SKUs (181 of 183).
# Editable via the "combo_sku_pattern" ledger-config setting, not hardcoded
# only here, in case PIL's naming convention changes or a future platform
# uses a different one.
DEFAULT_COMBO_SKU_PATTERN = r"PK[\s_-]*(\d+)"


def _extract_sku_multiplier(sku, pattern):
    if not sku or not pattern:
        return None
    try:
        m = re.search(pattern, sku, re.IGNORECASE)
    except re.error:
        return None
    if not m or not m.groups():
        return None
    try:
        value = float(m.group(1))
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


def _global_sku_rows(session, platform_slug):
    return session.query(TallySkuMaster).filter_by(platform_slug=platform_slug).all()


def _build_master_map(files, mappings_index, ledger_config=None, global_sku_rows=None):
    """Returns {sku: [{code, multiplier}, ...]} - see
    tally_parsing.build_master_sku_map for why this is a list rather
    than a single code. A component's multiplier is resolved in
    priority order: the Master file's own multiplier column if it has
    a value there, else a pattern match against the SKU itself (see
    DEFAULT_COMBO_SKU_PATTERN), else 1 - confirmed against a real PIL
    order (SKU "GT_PK2", Amazon-reported quantity 1) whose own
    historical Tally entry shows quantity 2, i.e. this fallback is
    reproducing an unbundling PIL's team already does today, not
    inventing a new one.

    global_sku_rows (TallySkuMaster rows for this run's platform, set
    from Skills - SKU/Product Master, not tied to any one run) seed
    the map first; a per-run uploaded Master file is then layered on
    top and wins for any SKU it also covers, since re-uploading a
    corrected file for a specific run is a more specific, deliberate
    action than the standing global mapping."""
    combo_pattern = (ledger_config or {}).get("combo_sku_pattern") or DEFAULT_COMBO_SKU_PATTERN
    combined = {}
    for row in (global_sku_rows or []):
        if not row.sku or not row.internal_code:
            continue
        combined.setdefault(row.sku, []).append({"code": row.internal_code, "multiplier": row.qty_multiplier})
    master_files = [f for f in files if f.file_role == "master"]
    master_mappings = mappings_index.get(("master", None, None), [])
    sku_col = next((m.source_column_name for m in master_mappings if m.target_field == "master_sku_column"), None)
    code_col = next((m.source_column_name for m in master_mappings if m.target_field == "master_code_column"), None)
    multiplier_col = next((m.source_column_name for m in master_mappings if m.target_field == "master_qty_multiplier_column"), None)
    if sku_col and code_col:
        for f in master_files:
            parsed = _parse_file(f)
            names = tally_parsing.sheet_names_of(parsed)
            if not names:
                continue
            sheet = next((m.source_sheet_name for m in master_mappings if m.source_sheet_name), None) or names[0]
            combined.update(tally_parsing.build_master_sku_map(parsed, sheet, sku_col, code_col, multiplier_col))
    for sku, components in combined.items():
        for component in components:
            if component["multiplier"] is None:
                component["multiplier"] = _extract_sku_multiplier(sku, combo_pattern) or 1.0
    return combined


def _build_batch_queues(files, mappings_index):
    """({(normalized_product, normalized_location): [{batch_no, qty,
    mfg_date, exp_date}, ...]}, location_aware) - oldest MFG date first
    - from every uploaded Batch wise Summary file. Returns ({}, False)
    if none was uploaded - batch allocation is entirely optional, never
    required to build a sheet, matching the PRD's own open question
    ("Upload the batch wise summary every month?").

    Each file is tried first as a native Tally "Stock Summary" export
    (tally_parsing.parse_tally_stock_summary) - PIL's real Batch wise
    Summary files turned out to be exactly this: one per warehouse,
    exported straight out of Tally, needing no column mapping at all
    since the layout is fixed and self-describing, with the location
    read straight from the file's own title block. Only a file that
    doesn't match that shape at all falls back to the older flat,
    user-mapped path (batch_product_column etc. in Field Mapping) - for
    a differently-shaped batch file from some other source.

    Keys are normalized (via _normalize_col - case/whitespace/
    punctuation tolerant) on both sides so a Stock Summary's product
    name ("ACNEGUARD FACE WASH GEL") and location ("BLR-7") match the
    same product's internal_product_code and Amazon's unpunctuated
    Warehouse Id ("BLR7") without an exact string match being required.
    location_aware is true the moment any batch data carries a location
    at all (every native Stock Summary file does) - a flat file with no
    location_col mapped contributes product-only entries (location
    key "") that simply won't be reachable once location_aware is on;
    this is an accepted edge case for mixing an unmapped flat file
    alongside real Stock Summary exports, not the common path."""
    batch_files = [f for f in files if f.file_role == BATCH_FILE_ROLE]
    if not batch_files:
        return {}, False

    native_by_file = {f.id: tally_parsing.parse_tally_stock_summary(f.file_blob) for f in batch_files}
    combined = {}
    location_aware = False

    for f in batch_files:
        native = native_by_file[f.id]
        if native is None:
            continue
        location = native["location_code"] or ""
        if not location:
            continue
        location_aware = True
        loc_key = _normalize_col(location)
        for product, batches in native["batches_by_product"].items():
            key = (_normalize_product_name(product), loc_key)
            combined.setdefault(key, []).extend(dict(b) for b in batches)

    flat_files = [f for f in batch_files if native_by_file[f.id] is None]
    if flat_files:
        batch_mappings = mappings_index.get((BATCH_FILE_ROLE, None, None), [])
        product_col = next((m.source_column_name for m in batch_mappings if m.target_field == "batch_product_column"), None)
        location_col = next((m.source_column_name for m in batch_mappings if m.target_field == "batch_location_column"), None)
        batch_col = next((m.source_column_name for m in batch_mappings if m.target_field == "batch_no_column"), None)
        qty_col = next((m.source_column_name for m in batch_mappings if m.target_field == "batch_qty_column"), None)
        mfg_col = next((m.source_column_name for m in batch_mappings if m.target_field == "batch_mfg_date_column"), None)
        exp_col = next((m.source_column_name for m in batch_mappings if m.target_field == "batch_exp_date_column"), None)
        if product_col and batch_col and qty_col:
            if location_col:
                location_aware = True
            for f in flat_files:
                parsed = _parse_file(f)
                names = tally_parsing.sheet_names_of(parsed)
                if not names:
                    continue
                sheet = next((m.source_sheet_name for m in batch_mappings if m.source_sheet_name), None) or names[0]
                queue = tally_parsing.build_batch_queue(parsed, sheet, product_col, location_col, batch_col, qty_col, mfg_col, exp_col)
                for (product, location), batches in queue.items():
                    key = (_normalize_product_name(product), _normalize_col(location))
                    combined.setdefault(key, []).extend(batches)
    for batches in combined.values():
        batches.sort(key=lambda b: (b["mfg_date"] == "", b["mfg_date"]))
    return combined, location_aware


def _allocate_batch_fifo(queues, product_code, location, quantity):
    """Consumes `quantity` units from the oldest batch(es) for
    (product_code, location), oldest MFG date first, mutating each
    batch's remaining qty in place so later rows continue where this
    one left off - FIFO consumption order matters across the whole
    run, not just within one row. Returns a list of allocations
    ({batch_no, qty, mfg_date, exp_date}) - more than one entry when a
    single sale spans more than one batch's remaining stock, exactly
    the PRD's own worked example (Batch 1: 22 units, Batch 2: 80
    units, 50 units sold -> 22 from Batch 1, 28 from Batch 2).

    None if there's no batch data at all for this (product, location) -
    distinct from "found some, but not enough", which instead returns
    a final entry with batch_no None for the shortfall. Either case is
    the caller's job to surface as a review item, never to guess a
    batch or silently short the row's quantity.

    product_code/location are normalized (_normalize_product_name /
    _normalize_col) before the lookup - _build_batch_queues stores its
    keys normalized the same way, so a row's raw internal_product_code
    /godown values match regardless of case/whitespace/punctuation/
    pack-size-notation differences against the Batch wise Summary's
    own text."""
    key = (_normalize_product_name(product_code), _normalize_col(location))
    if key not in queues:
        return None
    remaining = quantity
    allocations = []
    for batch in queues[key]:
        if remaining <= 0:
            break
        if batch["qty"] <= 0:
            continue
        take = min(remaining, batch["qty"])
        allocations.append({"batch_no": batch["batch_no"], "qty": take, "mfg_date": batch["mfg_date"], "exp_date": batch["exp_date"]})
        batch["qty"] -= take
        remaining -= take
    if remaining > 0:
        allocations.append({"batch_no": None, "qty": remaining, "mfg_date": None, "exp_date": None})
    return allocations


# PIL confirmed: a Cancel or Free Replacement transaction genuinely
# never consumes real stock, so it having no Batch/MFG/EXP data isn't
# a Batch Summary gap worth a human's attention - it's expected. This
# only suppresses the review-item/flag for these transaction types;
# Batch No./MFG/EXP still come back blank exactly as they would for
# any other unmatched row, just without asking anyone to chase it.
_BATCH_DATA_EXEMPT_TRANSACTION_TYPES = {"cancel", "freereplacement"}


def _batch_data_exempt(fields):
    return _normalize_col(fields.get("transaction_type")) in _BATCH_DATA_EXEMPT_TRANSACTION_TYPES


def apply_batch_allocation(rows, batch_queues, location_aware=False):
    """Runs after SKU/combo resolution, so internal_product_code,
    godown, and quantity are all final. No-op (returns rows unchanged)
    if no batch data was uploaded/configured at all.

    location_aware mirrors the flag _build_batch_queues returned: when
    the Batch wise Summary file has its own location/warehouse column
    mapped, allocation is scoped per (product, godown); when it
    doesn't, every batch for a product is pooled into one FIFO queue
    regardless of godown, since the PRD's own description of this
    logic never scopes it by location.

    A sale that spans more than one batch fans out into one row per
    batch consumed - same source_row_ref suffix convention as combo
    fan-out. gross_amount and discount are scaled proportionally to
    each split's share of the original quantity: unlike a same-product
    combo (which redistributes one sale's value only by unit count,
    Amazon's own gross_amount already covering the full sale), a batch
    split divides one sale's *existing* quantity across batches, so
    its existing financial fields must divide the same way or the
    sale's tax would be counted more than once across the split rows.

    A row tagged "batch_allocation_status" (no_batch_data or
    insufficient_stock) needed batch data the queues couldn't fully
    supply - the caller turns that into a review item; batch_no/
    mfg_date/exp_date are left blank on that row rather than guessed."""
    if not batch_queues:
        return rows
    new_rows = []
    for platform_slug, ref, fields in rows:
        product = str(fields.get("internal_product_code") or "").strip()
        location = str(fields.get("godown") or "").strip() if location_aware else ""
        try:
            qty = float(fields.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not product or (location_aware and not location) or qty <= 0:
            new_rows.append((platform_slug, ref, fields))
            continue

        allocations = _allocate_batch_fifo(batch_queues, product, location, qty)
        if allocations is None:
            fields["batch_allocation_status"] = "no_batch_data"
            new_rows.append((platform_slug, ref, fields))
            continue

        if len(allocations) == 1 and allocations[0]["batch_no"] is not None:
            a = allocations[0]
            fields["batch_no"] = a["batch_no"]
            fields["mfg_date"] = a["mfg_date"]
            fields["exp_date"] = a["exp_date"]
            new_rows.append((platform_slug, ref, fields))
            continue

        gross_amount = float(fields.get("gross_amount") or 0)
        discount = float(fields.get("discount") or 0)
        for idx, a in enumerate(allocations):
            row_fields = dict(fields)
            share = (a["qty"] / qty) if qty else 0
            row_fields["quantity"] = a["qty"]
            row_fields["gross_amount"] = round(gross_amount * share, 2)
            row_fields["discount"] = round(discount * share, 2)
            if a["batch_no"] is not None:
                row_fields["batch_no"] = a["batch_no"]
                row_fields["mfg_date"] = a["mfg_date"]
                row_fields["exp_date"] = a["exp_date"]
            else:
                row_fields["batch_allocation_status"] = "insufficient_stock"
            new_rows.append((platform_slug, f"{ref}#batch{idx}", row_fields))
    return new_rows


def parse_uploads(session, run):
    files = session.query(TallyUploadedFile).filter_by(run_id=run.id).all()
    parsed_by_file = {}
    for f in files:
        parsed = _parse_file(f)
        parsed_by_file[f.id] = parsed
        names = ",".join(tally_parsing.sheet_names_of(parsed))
        if f.sheet_names != names:
            f.sheet_names = names
    session.commit()
    return files, parsed_by_file


def _sales_mappings_for(mappings_index, platform_slug, order_type):
    """A mapping saved with no order_type is a general mapping for that
    platform, applying to every order type's report - most fields keep
    the same column across a platform's B2B and B2C exports, so this
    means mapping something once is normally enough. An order-type-
    specific mapping for the same target_field overrides the general
    one only for that order type (e.g. billing_pincode, which commonly
    does need Bill-to vs Ship-to), rather than requiring every field to
    be mapped twice."""
    general = mappings_index.get((SALES_FILE_ROLE, platform_slug, None), [])
    if not order_type:
        return general
    specific = mappings_index.get((SALES_FILE_ROLE, platform_slug, order_type), [])
    if not specific:
        return general
    overridden_fields = {m.target_field for m in specific}
    return [m for m in general if m.target_field not in overridden_fields] + specific


def resolve_field_mapping(files, parsed_by_file, mappings_index, platforms_by_slug):
    """Returns [(platform_slug, source_row_ref, canonical_fields_dict), ...].
    Every uploaded file with file_role == "sales" is a candidate, on
    equal footing regardless of which platform it's tagged with - this
    is the generalization that lets a new platform need zero pipeline
    code."""
    all_rows = []
    for f in files:
        if f.file_role != SALES_FILE_ROLE:
            continue
        mappings = _sales_mappings_for(mappings_index, f.platform_slug, f.order_type)
        parsed = parsed_by_file[f.id]
        for ref, canonical in _apply_mapping_to_sheet(parsed, mappings):
            canonical["platform"] = platforms_by_slug.get(f.platform_slug, f.platform_slug or "")
            canonical["order_type"] = f.order_type or "General"
            all_rows.append((f.platform_slug, ref, canonical))
    return all_rows


# Warehouse ID (Godown) comes back blank on self-fulfilled/MFN orders.
# The PRD documents that its Invoice Number prefix names the warehouse
# in that case - confirmed and explicitly authorized by PIL to apply
# for real (reversing an earlier "never override Godown" instruction,
# specifically and only for this narrow, PRD-documented pattern).
# Deliberately implemented as dedicated pipeline logic, not a TallyRule
# - godown stays permanently un-settable by any *configurable* rule
# (still enforced via FORBIDDEN_ACTION_FIELDS), so an arbitrary future
# rule still can't silently reassign a warehouse; only this one
# specific, auditable, code-reviewed inference can.
_GODOWN_INVOICE_PREFIXES = {"IN": "IN", "VZPL": "VZPL"}


def _infer_godown_from_invoice(fields):
    """Fills a blank godown from invoice_number's prefix when it's one
    of the two PRD-documented codes. Never touches a godown that's
    already set, and never guesses for a prefix outside that known
    set - an unrecognized or missing invoice number is left blank and
    still surfaces as a normal review exception. Returns True if it
    filled something in."""
    if fields.get("godown"):
        return False
    invoice = str(fields.get("invoice_number") or "").strip().upper()
    if not invoice:
        return False
    prefix = invoice.split("-")[0].strip()
    inferred = _GODOWN_INVOICE_PREFIXES.get(prefix)
    if not inferred:
        return False
    fields["godown"] = inferred
    return True


def _dedupe_rows(rows):
    """Drops an exact repeat of a row already seen - every mapped field
    identical, not just the order ID. Confirmed against real PIL data:
    the same order/SKU/amounts repeated byte-for-byte more than once,
    a known real-world Amazon MTR export quirk (a report regenerated
    or assembled from overlapping batches). Keeps the first occurrence
    only; source_row_ref is deliberately excluded from the comparison
    since it's expected to differ even for a genuine duplicate (it's
    just "which raw file row this came from").

    Never merges rows that only share an order ID - a shipment and its
    later refund/reversal share the same order_id but have opposite-
    signed amounts, and both must survive as distinct rows. Only a
    field-for-field identical row is treated as a duplicate."""
    seen = set()
    out = []
    for platform_slug, ref, fields in rows:
        key = tuple(sorted(fields.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append((platform_slug, ref, fields))
    return out


def resolve_sku_mapping(rows, sku_map):
    """Resolves each row's SKU against the master map and expands combo
    packs. sku_map values are lists of {code, multiplier} (see
    tally_parsing.build_master_sku_map):
      - one component -> same row, internal_product_code set and
        quantity multiplied (a same-product combo, e.g. a 3-pack) -
        gross_amount is left as-is, since Amazon's own gross_amount
        already covers the full sale regardless of how many units it
        was split into.
      - multiple components -> the row fans out into one output row
        per component, each with its own code and multiplied quantity
        (a combo of different products) - source_row_ref gets a "#i"
        suffix per fanned-out row so each stays individually traceable
        back to the one input row it came from. Unlike the same-
        product case, gross_amount/discount here DO need splitting:
        Amazon reports one combined price for the whole combo, so
        crediting that full amount to every component would multiply
        the combo's real revenue and tax by its own component count.
        Each component's share is its own multiplier's proportion of
        the combo's total multiplier weight (equal shares when every
        component's multiplier is 1, which is the common case; a
        component explicitly weighted 2x a sibling gets 2x its share) -
        the only split rule the master file's own data can support,
        since it carries no separate per-component price.
      - no match -> row is kept as a single row, flagged sku_in_master
        = "false".
    Returns (new_rows, unmapped_skus) - new_rows replaces the input
    list (its length can grow from fan-out), unmapped_skus maps each
    unmatched SKU to the indices *within new_rows* that need it, so one
    TallyReviewItem gets created per unique SKU rather than per row."""
    new_rows = []
    unmapped_skus = defaultdict(list)
    for platform_slug, ref, fields in rows:
        sku = str(fields.get("sku") or "").strip()
        components = sku_map.get(sku)
        try:
            base_qty = float(fields.get("quantity") or 0)
        except (TypeError, ValueError):
            base_qty = 0.0

        if not components:
            fields["sku_in_master"] = "false"
            unmapped_skus[sku].append(len(new_rows))
            new_rows.append((platform_slug, ref, fields))
            continue

        total_weight = sum(c["multiplier"] for c in components) if len(components) > 1 else 1
        base_gross = float(fields.get("gross_amount") or 0)
        base_discount = float(fields.get("discount") or 0)

        for idx, component in enumerate(components):
            row_fields = fields if len(components) == 1 else dict(fields)
            row_fields["internal_product_code"] = component["code"]
            row_fields["sku_in_master"] = "true"
            row_fields["quantity"] = base_qty * component["multiplier"]
            if len(components) > 1 and total_weight:
                share = component["multiplier"] / total_weight
                row_fields["gross_amount"] = round(base_gross * share, 2)
                row_fields["discount"] = round(base_discount * share, 2)
            row_ref = ref if len(components) == 1 else f"{ref}#{idx}"
            new_rows.append((platform_slug, row_ref, row_fields))

    return new_rows, unmapped_skus


def apply_conditional_rules(rows, rules, ledger_config):
    """Returns [(platform_slug, ref, fields, escalations, applied_rule_ids), ...]."""
    results = []
    for platform_slug, ref, fields in rows:
        resolved, escalations, applied = tally_rules.evaluate_rules(fields, rules, ledger_config)
        fields.update(resolved)
        results.append((platform_slug, ref, fields, escalations, applied))
    return results


# Column Mapping Sheet's own literal formula for Party Name: "Amazon
# FBA" + Godown, except these two Godown values which name a specific
# Amazon Seller Central/Flex account instead of a physical FBA
# warehouse - not a configurable Rule (like the Godown invoice-prefix
# inference next to _infer_godown_from_invoice, this is a fixed,
# always-applicable PRD formula, not a business preference PIL would
# ever want to turn off or edit per platform).
_PARTY_NAME_GODOWN_ALIASES = {
    "IN": "Amazon Seller Central",
    "VZPL": "Amazon Seller Flex",
}

# PIL's confirmed convention: a warehouse code always gets a hyphen
# between its letters and trailing digits in Party Name ("BLR7" ->
# "BLR-7") - this only reformats the string used to build Party Name,
# it never touches fields["godown"] itself, which must stay the
# platform's own Warehouse ID verbatim (see FORBIDDEN_ACTION_FIELDS).
_PARTY_NAME_GODOWN_HYPHEN_PATTERN = re.compile(r"^([A-Za-z]+)(\d+)$")


def _hyphenate_godown_for_party_name(godown):
    m = _PARTY_NAME_GODOWN_HYPHEN_PATTERN.match(godown)
    return f"{m.group(1)}-{m.group(2)}" if m else godown


def compute_totals(fields):
    """Pure arithmetic (plus the same kind of text-template fill as
    narration below it) - no AI call in this step or anywhere else in
    the deterministic path.

    GST is computed one of two ways, chosen per row by which source
    fields are actually present - not a global platform setting, so a
    platform that reports rates on some lines and a lump sum on others
    (or is migrated from one convention to the other) doesn't need a
    code change either way:

    - Rate-based (preferred): the platform reports its own
      cgst_rate/sgst_rate/utgst_rate/igst_rate per line (Amazon's MTR
      does). GST% = sum of the four rates; each tax value = taxable
      value x its own rate/100 - confirmed against the prototype's own
      sample rows (e.g. a Rate=98.59, Qty=3 row with IGST=53.24 and
      GST%=18 checks out as 295.77 x 18% = 53.24). UTGST has no
      dedicated Tally output column, so it's folded into the SGST
      bucket - UTGST is the Union-Territory equivalent of SGST (paired
      with CGST the same way SGST is), not IGST. Flagged as an
      assumption worth confirming with PIL rather than silently final.
    - Lump-sum split (fallback): only tax_amount_reported is present,
      no rate columns - the older tax_split rule group decides whether
      it's a CGST+SGST or IGST row. Kept only for a future platform
      that doesn't expose per-line rates at all.
    """
    def num(key):
        try:
            return float(fields.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    quantity = num("quantity")
    unit_price = num("unit_price")
    gross_raw = fields.get("gross_amount")
    gross_amount = num("gross_amount") if gross_raw not in (None, "") else quantity * unit_price
    discount = num("discount")
    # Deliberately not clamped to a minimum of 0 - a Refund line reports a
    # genuinely negative gross_amount (a credit), and zeroing it out would
    # silently turn a real refund into a no-op row instead of leaving it
    # correctly negative (or visibly wrong, if it shouldn't be there at
    # all - a business-logic question, not something to paper over here).
    taxable_value = gross_amount - discount
    fields["taxable_value"] = round(taxable_value, 2)
    fields["rate"] = round(taxable_value / quantity, 2) if quantity else 0.0

    if tally_rules.has_rate_columns(fields):
        # Amazon's MTR reports these as fractions (0.05 = 5%), not whole
        # percentages - confirmed against real PIL data (e.g. Rate 437.14 x
        # Qty 1 x 0.05 = 21.857, matching the real IGST value exactly). Tax
        # value is the rate applied directly, with no /100; GST% is scaled
        # up by 100 only for display, since that's a whole-number percent
        # in both the prototype and PIL's own real Tally output.
        cgst_rate, sgst_rate, utgst_rate, igst_rate = num("cgst_rate"), num("sgst_rate"), num("utgst_rate"), num("igst_rate")
        cgst = round(taxable_value * cgst_rate, 2)
        sgst = round(taxable_value * (sgst_rate + utgst_rate), 2)
        igst = round(taxable_value * igst_rate, 2)
        gst_percent = round((cgst_rate + sgst_rate + utgst_rate + igst_rate) * 100, 4)
    else:
        reported_tax = num("tax_amount_reported")
        mode = fields.get("tax_split_mode")
        cgst = sgst = igst = 0.0
        if mode == "cgst_sgst":
            cgst = sgst = round(reported_tax / 2, 2)
        elif mode == "igst":
            igst = round(reported_tax, 2)
        gst_percent = round((cgst + sgst + igst) / taxable_value * 100, 4) if taxable_value else 0.0

    fields["cgst_amount"] = cgst
    fields["sgst_amount"] = sgst
    fields["igst_amount"] = igst
    fields["gst_percent"] = gst_percent
    total_tax = round(cgst + sgst + igst, 2)
    fields["total_tax"] = total_tax
    fields["invoice_total"] = round(taxable_value + total_tax, 2)
    if not fields.get("narration"):
        fields["narration"] = f"{fields.get('platform', '')} {fields.get('order_type', '')} order {fields.get('order_id', '')} - {fields.get('internal_product_code') or fields.get('sku', '')}"
    if not fields.get("party_name") and fields.get("platform", "").strip().lower() == "amazon":
        godown = str(fields.get("godown") or "").strip()
        if godown:
            alias = _PARTY_NAME_GODOWN_ALIASES.get(godown.upper())
            fields["party_name"] = alias or f"Amazon FBA {_hyphenate_godown_for_party_name(godown)}"


def validate_and_flag(fields):
    """A failing row is flagged, never zero-filled or dropped silently.
    Returns the list of missing required field names (empty = ok).
    internal_product_code is skipped here when the SKU wasn't found in
    the Master file - that's already covered by its own
    "unmapped_sku" review item, so a row doesn't generate two
    redundant questions about the same root cause."""
    missing = []
    for k in REQUIRED_FOR_OK:
        if fields.get(k):
            continue
        if k == "internal_product_code" and fields.get("sku_in_master") == "false":
            continue
        missing.append(k)
    return missing


def _log_generation(session, generation, status, message):
    session.add(Event(generation_id=generation.id, module="amazon-tally", status=status, message=message))
    session.commit()


def _field_label(key):
    return next((f["label"] for f in CANONICAL_FIELDS if f["key"] == key), key)


def _manual_sku_overrides(session, run):
    """Answered "unmapped_sku" input-stage items, replayed as extra
    master-map entries - this is how a fix typed into that exception
    (before any generation exists to hold an output row) reaches every
    generation built afterward, without a dedicated override table."""
    items = session.query(TallyReviewItem).filter_by(
        run_id=run.id, stage="input", trigger_reason="unmapped_sku", status="answered"
    ).all()
    overrides = {}
    for item in items:
        try:
            detail = json.loads(item.detail or "{}")
            values = json.loads(item.answer_value or "{}")
        except (TypeError, ValueError):
            continue
        sku = detail.get("sku")
        code = values.get("internal_product_code")
        if sku and code:
            try:
                multiplier = float(values.get("qty_multiplier") or 1)
            except (TypeError, ValueError):
                multiplier = 1.0
            overrides[sku] = [{"code": code, "multiplier": multiplier}]
    return overrides


def validate_inputs(session, run):
    """Stage 1 - parses the run's uploaded files and checks them for
    input-level problems (right now: SKUs with no Master-file match),
    producing stage="input" TallyReviewItems. Doesn't create any output
    rows or generations - those come from generate_output() below,
    once every input issue is resolved."""
    run.status = "validating"
    run.error_message = None
    session.commit()

    try:
        files, parsed_by_file = parse_uploads(session, run)
        mappings_index = _mappings_index(session)
        platforms_by_slug = _platforms_by_slug(session)
        ledger_config = _ledger_config_dict(session)
        sku_map = _build_master_map(files, mappings_index, ledger_config, _global_sku_rows(session, run.platform_slug))
        sku_map.update(_manual_sku_overrides(session, run))
        rows = resolve_field_mapping(files, parsed_by_file, mappings_index, platforms_by_slug)

        session.query(TallyReviewItem).filter_by(run_id=run.id, stage="input").delete()
        session.commit()

        sku_row_counts = defaultdict(int)
        for _, _, fields in rows:
            sku = str(fields.get("sku") or "").strip()
            if sku and sku not in sku_map:
                sku_row_counts[sku] += 1

        for sku, count in sku_row_counts.items():
            session.add(TallyReviewItem(
                run_id=run.id, stage="input", severity="error",
                source_label="Sales report", location_label=f'{count} row(s) - column "SKU"',
                title=f'SKU "{sku}" was not found in the Master file',
                body="This SKU has no match in the Master file, so the agent can't resolve it to an internal product code.",
                detail=json.dumps({
                    "sku": sku,
                    "facts": [{"label": "Rows affected", "value": str(count)}],
                    "available_modes": ["fix", "reupload"],
                    "default_mode": "fix",
                    "fix": {"note": "Enter the internal product code for this SKU. If the SKU itself is a combo/pack (e.g. a PK2 or PK3 suffix), also set the quantity it represents - it multiplies this SKU's reported quantity the same way a Master File combo row would.",
                            "fields": [
                                {"key": "internal_product_code", "label": "Internal product code", "value": ""},
                                {"key": "qty_multiplier", "label": "Quantity (e.g. 2 for PK2, 3 for PK3)", "value": "1"},
                            ]},
                    "reupload": {"target_file_role": "master", "note": "Add the missing SKU to the item master and upload it again."},
                }),
                trigger_reason="unmapped_sku",
            ))
        session.commit()

        pending = session.query(TallyReviewItem).filter_by(run_id=run.id, stage="input", status="pending").count()
        run.review_pending_count = pending
        run.status = "needs_review" if pending else "ready"
        run.validated_at = datetime.datetime.utcnow()
        session.commit()
        _log(session, "amazon-tally", "success", f"Validated run #{run.id}: {len(rows)} row(s), {pending} open input issue(s).")
    except Exception as e:
        run.status = "failed"
        run.error_message = str(e)
        session.commit()
        _log(session, "amazon-tally", "error", f"Validation failed for run #{run.id}: {e}")
        raise


def available_slices(session, run):
    """Distinct order_type/location (Godown) values seen across the
    run's mapped sales rows, for the frontend's "what to build" picker -
    computed on demand, not stored."""
    files, parsed_by_file = parse_uploads(session, run)
    mappings_index = _mappings_index(session)
    platforms_by_slug = _platforms_by_slug(session)
    rows = resolve_field_mapping(files, parsed_by_file, mappings_index, platforms_by_slug)
    order_types = sorted({str(f.get("order_type") or "").strip() for _, _, f in rows if str(f.get("order_type") or "").strip()})
    locations = sorted({str(f.get("godown") or "").strip() for _, _, f in rows if str(f.get("godown") or "").strip()})
    return order_types, locations


def generate_output(session, run, generation):
    """Stage 2 - the deterministic pipeline (row filter -> SKU mapping/
    combo unbundling -> rules -> totals -> validate), filtered to this
    generation's (order_type, location) slice, producing stage="final"
    TallyReviewItems and TallyOutputRows scoped to this generation
    only - a sibling generation for a different slice of the same run
    is untouched. Logs one Event per step (generation_id set) so the
    frontend can animate an agent-run log while this executes."""
    generation.status = "processing"
    generation.error_message = None
    session.commit()

    try:
        files, parsed_by_file = parse_uploads(session, run)
        mappings_index = _mappings_index(session)
        platforms_by_slug = _platforms_by_slug(session)
        rules = _rule_dicts(session)
        ledger_config = _ledger_config_dict(session)
        sku_map = _build_master_map(files, mappings_index, ledger_config, _global_sku_rows(session, run.platform_slug))
        sku_map.update(_manual_sku_overrides(session, run))
        batch_queues, batch_location_aware = _build_batch_queues(files, mappings_index)
        rows = resolve_field_mapping(files, parsed_by_file, mappings_index, platforms_by_slug)
        _log_generation(session, generation, "info", f"Read the uploaded reports - {len(rows)} row(s) mapped")

        before_dedupe = len(rows)
        rows = _dedupe_rows(rows)
        if len(rows) < before_dedupe:
            _log_generation(session, generation, "info", f"Removed {before_dedupe - len(rows)} exact duplicate row(s)")

        # Must happen before the location filter below - a row whose
        # Godown only exists because of this inference still needs to
        # land in the right (order_type, location) slice.
        godown_inferred = sum(1 for _, _, f in rows if _infer_godown_from_invoice(f))
        if godown_inferred:
            _log_generation(session, generation, "info", f"Inferred Godown from Invoice Number for {godown_inferred} row(s) with a blank Warehouse ID")

        if generation.order_type:
            rows = [(p, ref, f) for p, ref, f in rows if (f.get("order_type") or "") == generation.order_type]
        if generation.location:
            rows = [(p, ref, f) for p, ref, f in rows if (f.get("godown") or "") == generation.location]
        slice_label = " · ".join(x for x in (generation.order_type, generation.location) if x) or "all rows"
        _log_generation(session, generation, "info", f"Filtered to {slice_label} - {len(rows)} row(s)")

        if len(rows) > MAX_ROWS:
            raise ValueError(f"{len(rows)} rows exceeds the {MAX_ROWS}-row limit for a single generation - narrow the slice or split the upload.")

        rows = [(p, ref, f) for p, ref, f in rows if tally_rules.passes_row_filter(f, rules, ledger_config)]
        _log_generation(session, generation, "info", f"Applied the row filter - {len(rows)} row(s) remain")

        rows, unmapped_skus = resolve_sku_mapping(rows, sku_map)
        before_combo_dedupe = len(rows)
        rows = _dedupe_rows(rows)
        combo_dupes_removed = before_combo_dedupe - len(rows)
        _log_generation(session, generation, "info",
            f"Resolved SKUs against the Master file and unbundled combo packs"
            + (f" - removed {combo_dupes_removed} duplicate row(s) introduced by the fan-out" if combo_dupes_removed else ""))

        if batch_queues:
            before_batch = len(rows)
            rows = apply_batch_allocation(rows, batch_queues, batch_location_aware)
            split_count = len(rows) - before_batch
            _log_generation(session, generation, "info",
                f"Allocated Batch No. / MFG / EXP dates FIFO from the Batch Summary"
                + (f" - {split_count} row(s) split across more than one batch" if split_count else ""))

        results = apply_conditional_rules(rows, rules, ledger_config)
        _log_generation(session, generation, "info", "Applied configured rules (tax split, voucher type, ledger mapping)")

        session.query(TallyOutputRow).filter_by(generation_id=generation.id).delete()
        session.query(TallyReviewItem).filter_by(generation_id=generation.id).delete()
        session.commit()

        row_objs = []
        for platform_slug, ref, fields, escalations, applied in results:
            compute_totals(fields)
            missing = validate_and_flag(fields)

            status = "ok"
            if fields.get("sku_in_master") == "false" or (fields.get("batch_allocation_status") and not _batch_data_exempt(fields)):
                status = "flagged"
            if escalations:
                status = "escalated"
            elif missing:
                status = "flagged"

            row = TallyOutputRow(
                run_id=run.id, generation_id=generation.id, source_file_role=platform_slug, source_row_ref=ref,
                order_id=fields.get("order_id"), sku=fields.get("sku"),
                data=json.dumps(fields), status=status,
                applied_rule_ids=",".join(str(i) for i in applied),
            )
            session.add(row)
            row_objs.append((row, escalations, missing, fields))
        session.commit()
        _log_generation(session, generation, "info", "Computed taxable value, GST, and totals for every row")

        for row, escalations, missing, fields in row_objs:
            # Shared context every final-stage item shows, on top of
            # whatever's specific to it - so a user unfamiliar with the
            # pipeline can see the whole shape of the row (not just an
            # Order ID) without opening the raw sheet.
            base_facts = [
                {"label": "Order ID", "value": row.order_id or ""},
                {"label": "SKU", "value": row.sku or ""},
                {"label": "Product", "value": fields.get("internal_product_code", "") or ""},
                {"label": "Platform", "value": (fields.get("platform") or "").title()},
                {"label": "Order Type", "value": fields.get("order_type", "") or ""},
                {"label": "Transaction Type", "value": fields.get("transaction_type", "") or ""},
            ]
            for esc in escalations:
                if esc["trigger_reason"] == "no_matching_rule":
                    consequence = "This row is on hold and won't be part of the downloaded sheet until it's resolved."
                    action = f'Fix it directly below, or add a rule for this case in Agent Management → Rules & mapping so it (and every future row like it) resolves on its own.'
                    body = f"{esc['message']} {consequence} {action}"
                else:
                    body = (f'Flagged by the rule "{esc.get("rule_name") or "an escalation rule"}" set up in '
                            f'Agent Management. {esc["message"]}')
                session.add(TallyReviewItem(
                    run_id=run.id, generation_id=generation.id, stage="final",
                    severity="warning" if esc["trigger_reason"] == "rule_escalation" else "error",
                    affected_row_ids=str(row.id),
                    source_label="Tally sheet", location_label=f"Order {row.order_id or ''}",
                    title=esc["message"],
                    body=body,
                    detail=json.dumps({
                        "facts": base_facts,
                        "available_modes": ["fix"],
                        "default_mode": "fix",
                        "fix": {"note": "Enter a note or override value to resolve this row.",
                                "fields": [{"key": "manual_review_note", "label": "Note / resolution", "value": ""}]},
                    }),
                    rule_id=esc.get("rule_id"), rule_name=esc.get("rule_name"),
                    trigger_reason=esc["trigger_reason"],
                ))
            if missing:
                field_names = ', '.join(_field_label(k) for k in missing)
                session.add(TallyReviewItem(
                    run_id=run.id, generation_id=generation.id, stage="final", severity="error",
                    affected_row_ids=str(row.id),
                    source_label="Tally sheet", location_label=f"Order {row.order_id or ''}",
                    title=f"Missing required field(s): {field_names}",
                    body=(f"Order {row.order_id or ''} is missing {field_names}. None of the uploaded files, "
                          f"rules, or the Master file produced a value for it, so this row can't post to Tally "
                          f"until it's filled in below."),
                    detail=json.dumps({
                        "facts": base_facts,
                        "available_modes": ["fix"],
                        "default_mode": "fix",
                        "fix": {"note": "Enter the missing value(s) for this row.",
                                "fields": [{"key": k, "label": _field_label(k), "value": ""} for k in missing]},
                    }),
                    trigger_reason="missing_required_field",
                ))
            batch_status = fields.get("batch_allocation_status")
            if batch_status and not _batch_data_exempt(fields):
                if batch_status == "no_batch_data":
                    title = f"No Batch Summary data for \"{fields.get('internal_product_code', '')}\" at \"{fields.get('godown', '')}\""
                    body = ("This product/location combination doesn't appear anywhere in the uploaded Batch Summary, "
                            "so the agent has nothing to allocate a batch from. Batch no./MFG/EXP were left blank on "
                            "this row - either enter them manually below, or check the Batch Summary actually covers "
                            "this product at this Godown and re-upload it.")
                else:
                    title = f"Batch Summary doesn't cover the full quantity for \"{fields.get('internal_product_code', '')}\" at \"{fields.get('godown', '')}\""
                    body = (f"The batches on file for this product/location only cover part of the "
                            f"{fields.get('quantity', '')} unit(s) sold on this row - the rest ran out. Batch no./MFG/EXP "
                            f"were left blank for the shortfall; enter them manually below, or update the Batch Summary "
                            f"with the missing batch(es) and rebuild.")
                session.add(TallyReviewItem(
                    run_id=run.id, generation_id=generation.id, stage="final", severity="warning",
                    affected_row_ids=str(row.id),
                    source_label="Tally sheet", location_label=f"Order {row.order_id or ''}",
                    title=title, body=body,
                    detail=json.dumps({
                        "facts": [
                            {"label": "Order ID", "value": row.order_id or ""},
                            {"label": "Product", "value": fields.get("internal_product_code", "")},
                            {"label": "Godown", "value": fields.get("godown", "")},
                            {"label": "Quantity on this row", "value": str(fields.get("quantity", ""))},
                            {"label": "Platform", "value": (fields.get("platform") or "").title()},
                            {"label": "Order Type", "value": fields.get("order_type", "") or ""},
                        ],
                        "available_modes": ["fix"],
                        "default_mode": "fix",
                        "fix": {"note": "Enter Batch No. / MFG Dt. / EXP Dt. for this row manually, or update the Batch Summary and rebuild.",
                                "fields": [
                                    {"key": "batch_no", "label": "Batch No.", "value": ""},
                                    {"key": "mfg_date", "label": "MFG Date", "value": ""},
                                    {"key": "exp_date", "label": "EXP Date", "value": ""},
                                ]},
                    }),
                    trigger_reason="batch_" + batch_status,
                ))
        session.commit()
        _log_generation(session, generation, "info", "Checked every row for missing required fields")

        finalize_generation(session, generation)
        _log_generation(session, generation, "success", f"Done - wrote {len(row_objs)} row(s)")
    except Exception as e:
        generation.status = "failed"
        generation.error_message = str(e)
        session.commit()
        _log_generation(session, generation, "error", f"Generation failed: {e}")
        raise


def finalize_generation(session, generation):
    rows = session.query(TallyOutputRow).filter_by(generation_id=generation.id).all()
    pending = session.query(TallyReviewItem).filter_by(generation_id=generation.id, status="pending").count()
    generation.total_rows = len(rows)
    generation.flagged_rows = sum(1 for r in rows if r.status in ("flagged", "escalated"))
    generation.review_pending_count = pending
    generation.status = "needs_review" if pending > 0 else "ready"
    generation.processed_at = datetime.datetime.utcnow()
    session.commit()


def finalize_run_validation(session, run):
    pending = session.query(TallyReviewItem).filter_by(run_id=run.id, stage="input", status="pending").count()
    run.review_pending_count = pending
    run.status = "needs_review" if pending else "ready"
    session.commit()


def apply_review_fix(session, run, item, values, user=None):
    """"Fix here" mode - applies the submitted field values directly.
    For a stage="final" item this rewrites its affected output rows
    (re-evaluating rules/totals for just those rows, never a full
    regenerate) the same way the pipeline always has; a row stays
    "flagged" if another pending item still blocks it. For a
    stage="input" item (no output rows exist yet) the values are
    simply recorded on the item - _manual_sku_overrides() replays them
    into the Master map the next time generate_output() runs."""
    row_ids = [int(i) for i in (item.affected_row_ids or "").split(",") if i.strip()]
    if row_ids:
        rules = _rule_dicts(session)
        ledger_config = _ledger_config_dict(session)
        other_pending = session.query(TallyReviewItem).filter(
            TallyReviewItem.generation_id == item.generation_id,
            TallyReviewItem.status == "pending",
            TallyReviewItem.id != item.id,
        ).all()

        for row_id in row_ids:
            row = session.query(TallyOutputRow).filter_by(id=row_id, run_id=run.id).first()
            if not row:
                continue
            fields = json.loads(row.data)
            fields.update(values)

            resolved, escalations, applied = tally_rules.evaluate_rules(fields, rules, ledger_config)
            fields.update(resolved)
            compute_totals(fields)
            missing = validate_and_flag(fields)

            row.data = json.dumps(fields)
            row.applied_rule_ids = ",".join(str(i) for i in applied)
            still_blocked = any(str(row_id) in (pi.affected_row_ids or "").split(",") for pi in other_pending)
            row.status = "flagged" if (missing or still_blocked) else "resolved"
            row.is_manual_override = True
            session.commit()

    item.status = "answered"
    item.resolution_mode = "fix"
    item.answer_value = json.dumps(values)
    item.answered_at = datetime.datetime.utcnow()
    item.answered_by = user
    session.commit()

    if item.stage == "input":
        finalize_run_validation(session, run)
    elif item.generation_id:
        generation = session.query(TallyGeneration).filter_by(id=item.generation_id).first()
        if generation:
            finalize_generation(session, generation)


def approve_review_suggestion(session, run, item, user=None):
    """"AI suggestion" mode - applies detail.suggestion.apply_values if
    the item's suggestion carries one (a structured field:value map a
    future trigger_reason could set alongside its human-readable
    "changes" list); otherwise there's nothing mechanical to apply and
    this just records the approval."""
    detail = json.loads(item.detail or "{}")
    apply_values = ((detail.get("suggestion") or {}).get("apply_values")) or {}
    if apply_values:
        apply_review_fix(session, run, item, apply_values, user=user)
        item.resolution_mode = "suggest"
        session.commit()
        return
    item.status = "answered"
    item.resolution_mode = "suggest"
    item.answer_value = json.dumps((detail.get("suggestion") or {}).get("summary") or "Suggestion approved")
    item.answered_at = datetime.datetime.utcnow()
    item.answered_by = user
    session.commit()
    if item.stage == "input":
        finalize_run_validation(session, run)
    elif item.generation_id:
        generation = session.query(TallyGeneration).filter_by(id=item.generation_id).first()
        if generation:
            finalize_generation(session, generation)


def reject_review_suggestion(session, item, user=None):
    """Leaves the item pending - the frontend nudges the person toward
    "Fix here" or "Re-upload sheet" instead, same as the prototype."""
    item.status = "pending"
    item.resolution_mode = None
    item.answered_by = None
    item.answered_at = None
    session.commit()


def defer_review_item(session, run, item, user=None):
    """"Decide later" - explicitly acknowledges an exception without
    fixing it. The affected row(s) are left exactly as they are -
    whatever's blank stays blank, row.status is untouched (still
    "flagged"/"escalated") - so the gap is still visible in the Excel
    output and still correctly excluded from the Tally XML export,
    which only takes "ok"/"resolved" rows. This only clears the
    review queue's "pending" gate for this one item, so the rest of
    the sheet isn't blocked on a column nobody's ready to answer yet."""
    item.status = "answered"
    item.resolution_mode = "deferred"
    item.answer_value = json.dumps({"note": "Decided later - left unresolved on purpose"})
    item.answered_at = datetime.datetime.utcnow()
    item.answered_by = user
    session.commit()

    if item.stage == "input":
        finalize_run_validation(session, run)
    elif item.generation_id:
        generation = session.query(TallyGeneration).filter_by(id=item.generation_id).first()
        if generation:
            finalize_generation(session, generation)
