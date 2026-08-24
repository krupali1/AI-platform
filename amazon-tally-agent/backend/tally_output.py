"""
The two output generators - the only place either output format is
constructed. Both take plain data structures (no DB/session access)
and are built fresh on every call, never cached, so a download always
reflects the latest overrides/answers.
"""
import io
import datetime
import xml.etree.ElementTree as ET
import pandas as pd


def build_excel_output(sample_columns, column_to_field, rows):
    """sample_columns: ordered list of the uploaded sample sheet's
    column headers. column_to_field: {sample_column_name: canonical_field}
    from TallyFieldMapping (source_file_role == "sample_tally"). rows:
    list of {"data": {...}, "status": "..."}.

    Every row of every status is included, with an appended Status
    column - nothing is silently hidden from this file, unlike the
    Tally XML export which structurally excludes unresolved rows."""
    records = []
    for row in rows:
        data = row.get("data") or {}
        record = {}
        for col in sample_columns:
            field = column_to_field.get(col)
            record[col] = data.get(field, "") if field else ""
        record["Status"] = row.get("status", "")
        records.append(record)

    columns = list(sample_columns) + ["Status"]
    df = pd.DataFrame(records, columns=columns)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Tally Import")
    return buf.getvalue()


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
