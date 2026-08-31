"""
The two output generators - the only place either output format is
constructed. Both take plain data structures (no DB/session access)
and are built fresh on every call, never cached, so a download always
reflects the latest overrides/answers.
"""
import io
import re
import datetime
import zipfile
import xml.etree.ElementTree as ET
import pandas as pd


def _sale_or_return(transaction_type):
    """PIL's own real Tally output is organized into separate "Sale"
    and "Return" files per platform/location - never one mixed file -
    so the downloaded output mirrors that instead of one flat sheet.
    A Refund is the only "Return" case; everything else that reaches
    this file (Shipment, FreeReplacement) is an outward dispatch and
    goes in "Sale" - a FreeReplacement is a real Return-goods-out
    event even though its value is 0, not a return."""
    return "Return" if (transaction_type or "").strip().lower() == "refund" else "Sale"


# A Return file carries two extra columns a Sale file doesn't - the
# Credit Note issued against the refund. Prepended ahead of the
# regular Sample Tally Format columns, matching PIL's own real Return
# output layout exactly (confirmed against a real "Amazon Return B2B"
# file). Sale files never had a Credit Note, so they don't get these.
RETURN_EXTRA_COLUMNS = ["CN No", "CN Date"]


def _columns_for(sale_or_return, sample_columns):
    if sale_or_return == "Return":
        return RETURN_EXTRA_COLUMNS + list(sample_columns)
    return list(sample_columns)


def _group_rows(sample_columns, column_to_field, rows):
    """Buckets rows into {(order_type, Sale/Return, godown): [records]},
    each record already shaped to that group's columns (Return groups
    get RETURN_EXTRA_COLUMNS prepended, Sale groups don't) + Status -
    one group becomes one file in build_excel_files. The Tally XML
    export stays a single file (Tally's own bulk-voucher-import format
    has no per-file grouping concept), so it doesn't use this."""
    groups = {}
    for row in rows:
        data = row.get("data") or {}
        sale_or_return = _sale_or_return(data.get("transaction_type"))
        key = (data.get("order_type") or "", sale_or_return, data.get("godown") or "")
        record = {}
        for col in _columns_for(sale_or_return, sample_columns):
            field = column_to_field.get(col)
            record[col] = data.get(field, "") if field else ""
        record["Status"] = row.get("status", "")
        groups.setdefault(key, []).append(record)
    if not groups:
        groups[("", "Sale", "")] = []
    return groups


_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _file_name(platform_name, order_type, sale_or_return, godown, period_label):
    """Matches PIL's own real file naming - e.g. "Amazon Sale B2B BLR7
    Apr 2026.xlsx" - so a file downloaded from this app drops straight
    into the same folder structure PIL already uses."""
    parts = [platform_name or "Platform", sale_or_return, order_type or "Other", godown or "No Location"]
    if period_label:
        parts.append(period_label)
    name = " ".join(p.strip() for p in parts if p and p.strip())
    name = _INVALID_FILENAME_CHARS.sub("-", name)
    return f"{name}.xlsx"


def build_excel_files(sample_columns, column_to_field, rows, platform_name="", period_label=""):
    """Returns a ZIP archive (bytes) containing one standalone .xlsx
    per (order_type, Sale/Return, godown) group - genuinely separate
    files, not sheets in one workbook, matching how PIL's own Tally
    output already exists as separate files per platform/location/
    B2B-or-B2C/Sale-or-Return rather than one mixed file. Every row of
    every status is still included somewhere, with an appended Status
    column - nothing is silently hidden, unlike the Tally XML export
    which structurally excludes unresolved rows. Files within the zip
    are named to sort in a predictable order: order type, then Sale
    before Return, then location alphabetically."""
    groups = _group_rows(sample_columns, column_to_field, rows)
    used_names = set()
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in sorted(groups, key=lambda k: (k[0], k[1] != "Sale", k[2])):
            order_type, sale_or_return, godown = key
            file_name = _file_name(platform_name, order_type, sale_or_return, godown, period_label)
            if file_name in used_names:
                stem, dot, ext = file_name.rpartition(".")
                n = 2
                while f"{stem} ({n}).{ext}" in used_names:
                    n += 1
                file_name = f"{stem} ({n}).{ext}"
            used_names.add(file_name)

            columns = _columns_for(sale_or_return, sample_columns) + ["Status"]
            df = pd.DataFrame(groups[key], columns=columns)
            xlsx_buf = io.BytesIO()
            with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Tally Import")
            zf.writestr(file_name, xlsx_buf.getvalue())
    return zip_buf.getvalue()


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _ledger_entry(parent, ledger_name, amount, is_deemed_positive):
    entry = _sub(parent, "ALLLEDGERENTRIES.LIST")
    _sub(entry, "LEDGERNAME", ledger_name or "")
    _sub(entry, "ISDEEMEDPOSITIVE", "Yes" if is_deemed_positive else "No")
    _sub(entry, "AMOUNT", f"{amount:.2f}" if is_deemed_positive else f"-{abs(amount):.2f}")


def build_tally_xml(rows, ledger_config):
    """rows: list of {"data": {...}, "status": "..."}. Only "ok" /
    "resolved" rows are ever included - a flagged/escalated row cannot
    reach this file, which is the concrete enforcement of "never guess
    a financial number." Standard Tally Prime voucher-import envelope,
    one VOUCHER per row."""
    envelope = ET.Element("ENVELOPE")
    header = _sub(envelope, "HEADER")
    _sub(header, "TALLYREQUEST", "Import Data")
    body = _sub(envelope, "BODY")
    import_data = _sub(body, "IMPORTDATA")
    request_desc = _sub(import_data, "REQUESTDESC")
    _sub(request_desc, "REPORTNAME", "Vouchers")
    request_data = _sub(import_data, "REQUESTDATA")

    for row in rows:
        if row.get("status") not in ("ok", "resolved"):
            continue
        data = row.get("data") or {}

        message = _sub(request_data, "TALLYMESSAGE")
        voucher = _sub(message, "VOUCHER")
        voucher.set("VCHTYPE", data.get("voucher_type") or "Sales")
        voucher.set("ACTION", "Create")

        date_val = data.get("order_date") or ""
        tally_date = _to_tally_date(date_val)
        _sub(voucher, "DATE", tally_date)
        _sub(voucher, "VOUCHERTYPENAME", data.get("voucher_type") or "Sales")
        _sub(voucher, "VOUCHERNUMBER", data.get("order_id") or "")
        _sub(voucher, "PARTYLEDGERNAME", data.get("party_ledger") or "")
        _sub(voucher, "NARRATION", data.get("narration") or f"Amazon order {data.get('order_id', '')}")

        taxable_value = _num(data.get("taxable_value"))
        _ledger_entry(voucher, data.get("party_ledger"), taxable_value + _num(data.get("total_tax")), True)
        _ledger_entry(voucher, data.get("sales_ledger"), taxable_value, False)

        tax_mode = data.get("tax_split_mode")
        if tax_mode == "cgst_sgst":
            _ledger_entry(voucher, ledger_config.get("cgst_ledger"), _num(data.get("cgst_amount")), False)
            _ledger_entry(voucher, ledger_config.get("sgst_ledger"), _num(data.get("sgst_amount")), False)
        elif tax_mode == "igst":
            _ledger_entry(voucher, ledger_config.get("igst_ledger"), _num(data.get("igst_amount")), False)

    xml_bytes = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    return xml_bytes


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_tally_date(value):
    """Tally expects YYYYMMDD. Falls back to today if the source date
    doesn't parse, rather than emitting an invalid/blank date."""
    if value:
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.datetime.strptime(str(value).strip(), fmt).strftime("%Y%m%d")
            except ValueError:
                continue
    return datetime.date.today().strftime("%Y%m%d")
