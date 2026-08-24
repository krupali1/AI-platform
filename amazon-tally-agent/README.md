# Amazon → Tally Automation Agent

Standalone tool that automates turning monthly marketplace sales reports (Amazon, Flipkart, Meesho, Nykaa, Myntra, JioMart, or any platform you add) into Tally-ready accounting entries. Built to match a client-approved prototype's exact flow: **Upload input sheets → Clear exceptions → Generate Tally sheet**, with a human-in-the-loop queue offering three ways to resolve anything the agent can't decide on its own — an AI suggestion to approve, a field to fix directly, or a source file to re-upload.

Independent of any other app in this repository on purpose — its own backend, database, and login — so it can be evaluated and used on its own, and folded into a larger platform later without carrying extra coupling along.

## Run it

```bash
cd amazon-tally-agent
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- Set `AUTH_USERS` (or `APP_USERNAME`/`APP_PASSWORD`) to whatever login you want your team to share.
- Generate a `SESSION_SECRET`: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `ANTHROPIC_API_KEY` is optional — only used by the "Suggest with AI" rule helper in `/admin`. Without it, that button returns a clearly-labeled demo suggestion instead of failing.
- `RESEND_API_KEY` + `RESEND_FROM_EMAIL` are optional — only used by "Email the sheet." Without them, that action returns a clear "not configured" message instead of failing mysteriously.

```bash
cd backend
uvicorn main:app --reload
```

Open `http://localhost:8000`, sign in, pick a platform from the sidebar, and work through the three steps on that one page: **Upload input sheets → Clear exceptions → Generate Tally sheet**. Field mapping, rules, ledger settings, and platform management live at `/admin` ("⚙ Configure Logic" in the sidebar) — a separate area for whoever administers the deterministic pipeline, deliberately kept out of the day-to-day flow the Online Sales team uses.

## How it works

### The Online Sales flow (`/`)

Pick a platform in the sidebar, then a period (month) at the top — everything below is scoped to that one platform + period (a "run").

1. **Upload input sheets** — a Master file (optional if nothing new was added), a Sample Tally sheet (defines the output columns), and any number of Sales Reports, each tagged with a report type (B2C, B2B, or anything you type). Click **Check the sheets** to validate.
2. **Clear exceptions** — anything wrong with the *uploaded* files (right now: a SKU with no Master-file match) shows up here. Each one opens a modal with up to three ways out — **Fix here** (type the value directly), or **Re-upload sheet** (replace the source file, which re-validates automatically) — a third **AI suggestion** mode exists in the data model for a future trigger that proposes a computed fix, though nothing currently generates one.
3. **Generate Tally sheet** — once every input exception is resolved, pick an order type and/or location (Godown) to build a slice for (or leave both as "All" for one combined sheet), and click **Build the Tally sheet**. You can build several slices from the same uploaded/validated data without re-uploading anything — e.g. a B2C sheet and a B2B sheet for the same month. Watch the agent's run log fill in live, then resolve any exceptions found in the *generated* rows (missing required field, no matching rule, an escalation rule you configured) the same way. Once clear, download the Excel sheet (matches your sample sheet's exact columns) or the native Tally XML (only ever includes rows with no pending review item), or email the Excel file straight to your accounts team.

### Configure Logic (`/admin`)

Three parts, all editable with no redeploy, none of it shown in the Online Sales flow above:

- **Field mapping**: which column in each platform's report feeds which field the agent uses, and which field fills which column of your sample sheet. Needs a "reference run" (any run with files already uploaded) to read column names from. A mapping can also be a **constant value** instead of a column (Courier = "Amazon", Units = "Pcs", Freight = 0) for fields that are always the same, not read from the file. Mappings are scoped per platform, and further scoped per **order type** when a platform's B2B and B2C reports genuinely use different columns (Bill-to vs Ship-to pincode/city/state) — a mapping saved with no order type applies to every order type for that platform, so most fields only need mapping once.
- **Rules**: deterministic `IF condition THEN action` rules, grouped into row filter / escalation / tax split / voucher type / ledger mapping. **Row filter** is an include/exclude allow-list (e.g. only process Transaction Type = Shipment, Refund, or Free replacement) — a row that fails it is excluded from the output entirely, never flagged. **Tax split** only matters as a fallback: if a platform's report already carries its own CGST/SGST/UTGST/IGST *rate* columns (Amazon's does, as **fractions** — `0.05` = 5%, confirmed against real data), CGST/SGST/IGST values and GST% are computed straight from those rates and no tax_split rule is needed at all — this is also robust to a genuinely nil-rated line (every rate legitimately 0%, a real occurrence), which still takes the rate-based path rather than misfiring into the fallback. `godown` can never be set by a rule — it always comes straight from the platform's own Warehouse ID column, enforced at the API level. A "Suggest with AI" helper can propose a rule from a plain-language description of an edge case, and "Upload rules document" does the same at document scale. Either way, the AI only ever proposes: you review and save each one yourself.
- **Ledger & Vouchers**: your actual Tally ledger names, voucher types, and home state.
- **Combo/bundle SKUs**: the Master file can map one platform SKU to *more than one* internal product code (add another row with the same SKU) with an optional quantity-multiplier column — a same-product combo (e.g. a 3-pack) becomes one output row with quantity multiplied; a combo of different products fans out into one output row per component. If the Master file's own multiplier column is blank, the multiplier falls back to a pattern match against the SKU itself (default: `PK` followed by a number, any separator — `PK2`, `PK-3`, `PK_4`) — editable via the `combo_sku_pattern` setting in Ledger & Vouchers. Confirmed against a real historical order: SKU `GT_PK2` with no multiplier in the Master file, Amazon-reported quantity 1, correctly unbundles to quantity 2, matching the same order's real historical Tally entry exactly.

## What's not handled yet (known scope limits, not bugs)

- Only order-level sales report rows become output rows in this version — an inventory/stock-style report can still be uploaded under "Additional Files" but isn't consumed by the pipeline yet.
- Batch-wise FIFO allocation (Batch no / MFG Dt. / EXP Dt.) and the Amazon Stock Transfer Report aren't modeled yet — deliberately deferred; the PRD itself flags STR as needing more clarification from the client before building it.
- A built-in platform (Amazon, Flipkart, Meesho, Nykaa, Myntra, JioMart) can't be deleted, and a custom one can't be deleted while it still has runs against it — remove those first.
- UTGST is folded into the SGST bucket on output (there's no separate Tally column for it) on the assumption that UTGST plays the same role for Union Territories that SGST plays for states — a reasonable assumption, not yet confirmed with the client.
- **Refund handling is unresolved and needs the accounts team's input.** A Refund transaction currently gets a correctly-signed negative row (not silently zeroed - that was a real bug, now fixed) and stays in the row-filter's default allow-list per the PRD. But a real cross-check against an actual refund found the client's own historical Tally output has *no row at all* for it - so "negative row" may not be the right answer. Don't trust Refund rows in the output until this is confirmed.
- The "AI suggestion" resolution mode is fully wired up (data model, endpoints, modal UI) but nothing currently generates a suggestion — no trigger_reason in this build computes one. Fix and Re-upload are the two modes actually exercised today.
- A genuinely new kind of logic (a different country's tax regime, say) needs a small code change in `backend/tally_rules.py`/`backend/tally_pipeline.py`; a new *platform*, by contrast, is pure configuration (add it from the sidebar, map its columns in `/admin`) — no code change at all.

## Validated against real data

This has been run against a real month of PIL's actual Amazon MTR B2C/B2B exports (~1,400 rows) and real Master file — not just synthetic test data. That caught and fixed three real bugs a synthetic test never would have: rate columns being fractions rather than whole percentages, a nil-rated (0% GST) product being misdetected as missing rate data, and Refund rows being silently zeroed instead of correctly signed. It also confirmed the combo-unbundling fallback (SKU-suffix pattern) against a specific real order whose historical Tally entry it now reproduces exactly.

## Try it in 5 minutes

`sample-data/` has four ready-to-upload files so you can see the whole flow without building test data by hand: `amazon_b2c_sample.csv`, `amazon_b2b_sample.csv`, `master_sample.csv`, `sample_tally_sheet.csv`. They're designed to exercise the interesting paths: one row with a SKU not in the Master file (triggers an exception), one "Cancelled" row (gets silently filtered out once you add a row filter rule), and both intra-state (CGST+SGST) and inter-state (IGST) rows. Rate columns are fractions (`0.09` = 9%), matching real Amazon MTR data.

1. Sign in, pick **Amazon** in the sidebar, create a period.
2. Upload `master_sample.csv` as the Master File, `sample_tally_sheet.csv` as the Sample Tally Sheet, then add two Sales Reports: `amazon_b2c_sample.csv` tagged **B2C** and `amazon_b2b_sample.csv` tagged **B2B**.
3. Go to `/admin` → **Field Mapping**, pick this run as the reference, and map (source column → field): `Order ID`→order_id, `Order Date`→order_date, `Transaction Type`→transaction_type, `SKU`→sku, `Qty`→quantity, `Unit Price`→unit_price, `Warehouse ID`→godown, `Cgst Rate`/`Sgst Rate`/`Utgst Rate`/`Igst Rate`→their matching fields. For the Master file: `Platform SKU`→master_sku_column, `Internal Code`→master_code_column.
4. Still in `/admin` → **Rules**, add two rules so the mandatory groups resolve: `voucher_type` group, condition `order_id exists`, action `use_voucher_type` = `Sales`; `ledger_mapping` group, condition `order_id exists`, action `use_ledger`, field `sales_ledger`, value `Sales`. Optionally add a `row_filter` rule (`transaction_type equals Shipment`, action `include_row`) to see the "Cancelled" row get excluded.
5. Back at `/`, click **Check the sheets** — you'll see one exception for the unmapped SKU. Open it and try **Fix here** (type any product code) to resolve it.
6. Click **Build the Tally sheet** (leave order type/location as "All" the first time), watch the agent log, then download the Excel or Tally XML output.

## Security note

`AUTH_USERS`/`APP_USERNAME`+`APP_PASSWORD` is intentionally simple — one shared login for a small internal team, not a general user system. Don't expose this deployment to the public internet without at least that configured, and set `COOKIE_SECURE=true` once it's served over HTTPS.
