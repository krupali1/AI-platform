"""
All pandas/openpyxl file reading lives here - isolates the one heavy
dependency this feature adds, and keeps tally_pipeline.py free of
file-format concerns.
"""
import io
import pandas as pd


def parse_excel_file(blob, content_type=None, filename=None):
    """Returns {sheet_name: DataFrame} for an uploaded .xlsx/.xls/.csv
    blob. A CSV has no sheet name, so it's given the synthetic name
    "Sheet1" to keep every caller dealing with the same shape."""
    buf = io.BytesIO(blob)
    name = (filename or "").lower()
    if name.endswith(".csv") or (content_type and "csv" in content_type):
        df = pd.read_csv(buf, dtype=str, keep_default_na=False)
        return {"Sheet1": df}
    sheets = pd.read_excel(buf, sheet_name=None, dtype=str, keep_default_na=False)
    return sheets


def sheet_names_of(parsed):
    return list(parsed.keys())


def column_names_of(parsed, sheet_name):
    if sheet_name not in parsed:
        return []
    return list(parsed[sheet_name].columns)


def build_master_sku_map(parsed, sheet_name, sku_column, code_column, multiplier_column=None):
    """Platform SKU/ASIN -> a list of {code, multiplier} components, from
    the parsed Master file. A list rather than a single code is what
    makes combo/bundle packs pure master-file configuration: a same-
    product combo (SKU "X_PK3" = 3x the base product) is one master row
    with multiplier 3; a combo of different products is multiple master
    rows sharing the same SKU, one per component - PIL adds a row to
    the master sheet for a new combo, never touches code or a rule.
    Blank/missing SKUs are skipped rather than mapped to an empty
    string, so a lookup miss stays a genuine miss.

    multiplier is None (not defaulted to 1) when the column isn't
    mapped or a row's value is blank/invalid - resolving a blank into
    an actual number is tally_pipeline.py's job (master-file value if
    present, else a SKU-suffix pattern, else 1), not this parsing
    layer's - confirmed against real PIL data that the Master file's
    own multiplier column is usually left blank even for genuine
    combo SKUs, so "blank" and "explicitly 1" have to stay
    distinguishable this far down.

    A SKU whose (code, multiplier) pair repeats identically across more
    than one master row is NOT a combo of identical components - it's
    the same fact stated twice, a real data-entry duplication confirmed
    in a real PIL master file (several SKUs each listed twice with the
    exact same Product Name and a blank Pack Of). Left undeduped, this
    silently doubled the affected orders' output rows, since the combo-
    unbundling logic downstream has no way to tell "genuinely 2
    identical units" apart from "the same row entered twice." A real
    same-product combo is still expressed correctly - as a single row
    with an explicit multiplier, or via the SKU-suffix pattern - so
    deduping exact repeats here never discards a real multi-unit pack."""
    if sheet_name not in parsed:
        return {}
    df = parsed[sheet_name]
    if sku_column not in df.columns or code_column not in df.columns:
        return {}
    has_multiplier_col = multiplier_column and multiplier_column in df.columns
    mapping = {}
    for _, row in df.iterrows():
        sku = str(row[sku_column]).strip()
        code = str(row[code_column]).strip()
        if not sku or not code:
            continue
        multiplier = None
        if has_multiplier_col:
            raw = str(row[multiplier_column]).strip()
            if raw:
                try:
                    multiplier = float(raw)
                except ValueError:
                    multiplier = None
        component = {"code": code, "multiplier": multiplier}
        components = mapping.setdefault(sku, [])
        if component not in components:
            components.append(component)
    return mapping


def rows_of(parsed, sheet_name):
    """Yields each row of a sheet as a plain dict, in file order, with
    a 1-based row index for traceability back to the raw upload."""
    if sheet_name not in parsed:
        return
    df = parsed[sheet_name]
    for idx, row in df.iterrows():
        yield idx + 1, row.to_dict()


DOCUMENT_EXTENSIONS = (".txt", ".md", ".docx", ".pdf", ".xlsx", ".xls", ".csv")


def extract_document_text(blob, content_type=None, filename=None):
    """Plain text out of a rules/conditions document a human wrote in
    whatever format they already had it in - a Word doc, a PDF, a
    plain text/markdown note, or a spreadsheet of edge cases. Used only
    to feed tally_rules.suggest_rules_from_document(); never parsed as
    structured data the way an Amazon/Master/sample file is, so this
    lives here (the file-format module) rather than in tally_rules.py
    (which stays free of format concerns, same reasoning as
    parse_excel_file living here instead of in tally_pipeline.py)."""
    name = (filename or "").lower()

    if name.endswith(".docx"):
        import docx
        document = docx.Document(io.BytesIO(blob))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)

    if name.endswith(".pdf"):
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(blob))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if name.endswith((".xlsx", ".xls", ".csv")):
        parsed = parse_excel_file(blob, content_type, filename)
        lines = []
        for sheet in sheet_names_of(parsed):
            lines.append(f"# {sheet}")
            for _, row in rows_of(parsed, sheet):
                cells = [f"{k}: {v}" for k, v in row.items() if str(v).strip()]
                if cells:
                    lines.append(", ".join(cells))
        return "\n".join(lines)

    # .txt / .md / anything else - treat as plain text
    return blob.decode("utf-8", errors="replace")
