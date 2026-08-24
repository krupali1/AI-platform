"""
Plain, no-AI parsers for two fixed-shape spreadsheets a user uploads
directly at Step 1: a column-mapping spec (Tally column / source
sheet / source column, e.g. the PRD's own table) and a rules sheet
(one row per IF/THEN rule). Both are pattern-matched against known,
literal formats - nothing here ever calls a model or guesses; a row
that doesn't match a known shape is skipped and reported back in
"flagged" rather than silently applied or silently dropped.
"""
import re

from tally_rules import RULE_GROUPS, CONDITION_OPERATORS, ACTION_TYPES, FORBIDDEN_ACTION_FIELDS

# Fields the pipeline computes itself - never the target of an INPUT
# (sales/master) mapping, only ever an OUTPUT (sample_tally) one. Same
# set used by the AI-assisted mapper in tally_rules.py, kept here too
# since this module intentionally has no dependency on that one (it
# must work with zero AI configured).
COMPUTED_FIELD_KEYS = {
    "platform", "order_type", "internal_product_code", "sku_in_master", "taxable_value",
    "rate", "gst_percent", "cgst_amount", "sgst_amount", "igst_amount", "total_tax",
    "invoice_total", "voucher_type", "sales_ledger", "party_ledger", "party_name", "narration",
}

# "Tally sheet" output-column label (normalized) -> canonical field key.
# Deliberately doesn't cover batch/MFG/EXP columns - FIFO batch
# allocation isn't built, so those stay unrecognized on purpose and
# surface in "flagged" rather than pointing anywhere.
_TALLY_COLUMN_ALIASES = {
    "orderid": "order_id",
    "order_date": "order_date", "orderdate": "order_date",
    "transactiontype": "transaction_type",
    "sku": "sku", "asin": "asin",
    "productdescription": "product_description",
    "billing_pincode": "billing_pincode", "billingpincode": "billing_pincode",
    "billing_city": "billing_city", "billingcity": "billing_city",
    "billing_state": "billing_state", "billingstate": "billing_state",
    "courier": "courier",
    "invoiceno": "invoice_number", "invoicenumber": "invoice_number",
    "productname": "internal_product_code",
    "godown": "godown",
    "hsncode": "hsn_code", "hsnsac": "hsn_code", "hsn/sac": "hsn_code",
    "qty": "quantity", "quantity": "quantity",
    "units": "units",
    "rate": "rate",
    "gst%": "gst_percent", "gstpercent": "gst_percent",
    "fright": "freight", "freight": "freight",
    "discount": "discount",
    "cgst": "cgst_amount", "sgst": "sgst_amount", "igst": "igst_amount",
    "partyname": "party_name",
    "buyergstin": "buyer_gstin",
    "shiptostate": "ship_to_state", "shiptopincode": "ship_to_pincode",
    "unitprice": "unit_price", "grossamount": "gross_amount",
    "shippingcharge": "shipping_charge",
}


def _norm(s):
    return re.sub(r"[\s_.\-]+", "", str(s or "")).strip().lower()


def _find_col(columns, *needles):
    """First column whose normalized name contains every needle."""
    for c in columns:
        n = _norm(c)
        if all(needle in n for needle in needles):
            return c
    return None


def _extract_constant(text):
    """'Text value = "Amazon", by default' / 'Integer value = 0, by
    default' -> the literal. None if the text isn't a constant
    declaration."""
    m = re.search(r'(?:text|integer)\s*value\s*=\s*[\'"“]?([^\'"”,]+)[\'"”]?', text, re.I)
    if m:
        return m.group(1).strip()
    return None


def _extract_order_type_branches(text):
    """'If B2B: Bill to postal code\\nIf B2C: Ship to Postal code' ->
    {"B2B": "Bill to postal code", "B2C": "Ship to Postal code"}.
    Empty dict if the text isn't order-type-branched."""
    matches = re.findall(r'if\s+(b2b|b2c)\s*:\s*([^\n\r]+?)(?=\s*(?:if\s+b2b|if\s+b2c|$))', text, re.I)
    return {ot.upper(): val.strip() for ot, val in matches}


def _looks_like_master_lookup(text):
    return bool(re.search(r'tally\s*product', text, re.I)) or bool(re.search(r"map(ped)?\s+from", text, re.I))


def _looks_like_conditional(text):
    return bool(re.search(r'\bexception\b|\bif\s+\w|\bdepend', text, re.I))


def _source_role_from_label(label):
    n = _norm(label)
    if not n or n == "-":
        return None
    if "master" in n or "tallyproduct" in n:
        return "master"
    if "batch" in n:
        return None  # FIFO batch allocation isn't built - never a valid role here
    return "sales"


def parse_column_mapping_sheet(parsed, sheet_name=None):
    """parsed: the dict from tally_parsing.parse_excel_file(). Returns
    {"mappings": [...], "flagged": [...]}. Each mapping dict has the
    same shape POST /api/tally-field-mappings expects (target_field,
    source_file_role, order_type, source_column_name, constant_value)."""
    sheet_name = sheet_name or (list(parsed.keys())[0] if parsed else None)
    mappings, flagged = [], []
    if not sheet_name or sheet_name not in parsed:
        return {"mappings": mappings, "flagged": [{"label": "File", "reason": "Could not read a sheet from this file."}]}

    df = parsed[sheet_name]
    columns = list(df.columns)
    target_col = _find_col(columns, "tally", "sheet") or _find_col(columns, "tally", "column") or _find_col(columns, "output", "column")
    source_sheet_col = _find_col(columns, "source", "sheet")
    source_col = _find_col(columns, "exact", "column") or _find_col(columns, "source", "column") or _find_col(columns, "logic")
    remarks_col = _find_col(columns, "except") or _find_col(columns, "remark")

    if not target_col or not source_col:
        flagged.append({"label": "Column headers not recognized", "reason": f'Expected columns like "Tally sheet", "Source sheet", "Exact column name" - found: {", ".join(columns)}'})
        return {"mappings": mappings, "flagged": flagged}

    for _, row in df.iterrows():
        tally_label = str(row.get(target_col, "")).strip()
        if not tally_label:
            continue
        source_sheet_label = str(row.get(source_sheet_col, "")).strip() if source_sheet_col else ""
        source_text = str(row.get(source_col, "")).strip()

        key = _TALLY_COLUMN_ALIASES.get(_norm(tally_label))
        if not key:
            flagged.append({"label": f'"{tally_label}"', "reason": "Not a recognized Tally sheet column (or it's a Batch/FIFO field, not built yet) - map it manually in /admin if needed."})
            continue

        # Output side: this canonical field fills this exact output column.
        mappings.append({"target_field": key, "source_file_role": "sample_tally", "order_type": "", "source_column_name": tally_label, "constant_value": ""})

        if key in COMPUTED_FIELD_KEYS:
            continue  # computed by the pipeline - no input column to map from

        if _looks_like_master_lookup(source_text):
            flagged.append({"label": f'"{tally_label}"', "reason": "Comes from your Master File (SKU → product code) lookup, not a direct column - already handled by that upload."})
            continue

        constant = _extract_constant(source_text)
        role = _source_role_from_label(source_sheet_label)
        if constant is not None:
            mappings.append({"target_field": key, "source_file_role": role or "sales", "order_type": "", "source_column_name": "", "constant_value": constant})
        else:
            branches = _extract_order_type_branches(source_text)
            if branches and role:
                for order_type, col_name in branches.items():
                    mappings.append({"target_field": key, "source_file_role": role, "order_type": order_type, "source_column_name": col_name, "constant_value": ""})
            elif role and source_text and _looks_like_conditional(source_text):
                # Conditional logic this parser can't safely turn into a
                # literal column (e.g. "If no combo: X / If combo: rules
                # apply") - flagged rather than guessed at, so it never
                # silently becomes a wrong mapping.
                flagged.append({"label": f'"{tally_label}" has a condition', "reason": f'"{source_text.strip()[:160]}" - this is conditional logic, not a plain column mapping. Add it as a Rule instead; not applied here.'})
            elif role and source_text:
                mappings.append({"target_field": key, "source_file_role": role, "order_type": "", "source_column_name": source_text, "constant_value": ""})

        if remarks_col:
            remarks_text = str(row.get(remarks_col, "")).strip()
            if remarks_text and remarks_text != "-" and _looks_like_conditional(remarks_text):
                flagged.append({"label": f'"{tally_label}" has a noted exception', "reason": f'"{remarks_text[:160]}" - this is a conditional rule, not applied automatically. Add it as a Rule if you need it enforced.'})

    return {"mappings": mappings, "flagged": flagged}


def parse_rules_sheet(parsed, sheet_name=None):
    """parsed: the dict from tally_parsing.parse_excel_file(). Expected
    columns: Rule Group, Condition Field, Condition Operator, Condition
    Value, Action Type, Action Field, Action Value, Description (any
    reasonable spacing/casing). Returns {"rules": [...], "flagged": [...]}
    - each rule dict has the same shape POST /api/tally-rules expects."""
    sheet_name = sheet_name or (list(parsed.keys())[0] if parsed else None)
    rules, flagged = [], []
    if not sheet_name or sheet_name not in parsed:
        return {"rules": rules, "flagged": [{"label": "File", "reason": "Could not read a sheet from this file."}]}

    df = parsed[sheet_name]
    columns = list(df.columns)
    col_group = _find_col(columns, "rule", "group") or _find_col(columns, "group")
    col_cfield = _find_col(columns, "condition", "field")
    col_cop = _find_col(columns, "condition", "operator")
    col_cval = _find_col(columns, "condition", "value")
    col_atype = _find_col(columns, "action", "type")
    col_afield = _find_col(columns, "action", "field")
    col_aval = _find_col(columns, "action", "value")
    col_desc = _find_col(columns, "description")

    required = {"Rule Group": col_group, "Condition Field": col_cfield, "Condition Operator": col_cop, "Action Type": col_atype}
    missing = [label for label, col in required.items() if not col]
    if missing:
        flagged.append({"label": "Column headers not recognized", "reason": f'Missing expected column(s): {", ".join(missing)}. Found: {", ".join(columns)}'})
        return {"rules": rules, "flagged": flagged}

    for i, row in df.iterrows():
        rule_group = str(row.get(col_group, "")).strip().lower().replace(" ", "_")
        condition_field = str(row.get(col_cfield, "")).strip()
        condition_operator = str(row.get(col_cop, "")).strip().lower()
        condition_value = str(row.get(col_cval, "")).strip() if col_cval else ""
        action_type = str(row.get(col_atype, "")).strip().lower().replace(" ", "_")
        action_field = str(row.get(col_afield, "")).strip() if col_afield else ""
        action_value = str(row.get(col_aval, "")).strip() if col_aval else ""
        description = str(row.get(col_desc, "")).strip() if col_desc else ""

        if not rule_group and not condition_field and not action_type:
            continue  # blank row

        row_label = f"Row {i + 1}" + (f' ("{description}")' if description else "")
        if rule_group not in RULE_GROUPS:
            flagged.append({"label": row_label, "reason": f'Unknown rule group "{rule_group}" - expected one of: {", ".join(RULE_GROUPS)}'})
            continue
        if condition_operator not in CONDITION_OPERATORS:
            flagged.append({"label": row_label, "reason": f'Unknown condition operator "{condition_operator}" - expected one of: {", ".join(CONDITION_OPERATORS)}'})
            continue
        if action_type not in ACTION_TYPES:
            flagged.append({"label": row_label, "reason": f'Unknown action type "{action_type}" - expected one of: {", ".join(ACTION_TYPES)}'})
            continue
        if action_field in FORBIDDEN_ACTION_FIELDS:
            flagged.append({"label": row_label, "reason": f'"{action_field}" can never be set by a rule - it always comes verbatim from the platform\'s own Warehouse ID column.'})
            continue

        rules.append({
            "rule_group": rule_group, "condition_field": condition_field, "condition_operator": condition_operator,
            "condition_value": condition_value, "action_type": action_type, "action_field": action_field,
            "action_value": action_value, "description": description,
        })

    return {"rules": rules, "flagged": flagged}
