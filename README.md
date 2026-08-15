# SellUp Stock Bulk Update

Streamlit tool that turns a Mister Mobile POS stock report into a SellUp
dealer bulk-upload file, without touching anything in the template except the
three quantity columns.

Companion to the Shopee stock sync tool, and built to the same shape.

---

## What it does

1. Reads the POS masterlist export and takes **Column F, Available Quantity** —
   never the gross Quantity column, never a sum of the branch columns.
2. Applies the SKU links you have already confirmed, carried in from
   `SellUp Stock Data.xlsx` or from a registry this tool exported earlier.
3. Writes the resulting quantities into the SellUp template:

   | POS row | Goes to | Column |
   |---|---|---|
   | `New`, model ends in `NA` or has no activation token | New (Not Activated) Qty | **G** |
   | `New`, model ends in `A` | New (Activated) Qty | **I** |
   | `Used` | Excellent Qty | **K** |

4. Anything with stock and no confirmed link lands in a review queue. The
   download stays locked until that queue is empty.

Only Apple phones carry the `NA` / `A` tokens. Every other brand has no
activation distinction, so its stock goes to column G and column I is left
alone.

### One listing, three quantities

A single SellUp SKU is often fed by several POS rows at once. `SKU-000074155`
is a real example from the data:

```
POS 31242  New   17 PRO MAX 256GB NA  SILVER   21  ->  column G
POS 31243  New   17 PRO MAX 256GB A   SILVER    1  ->  column I
POS 31628  Used  17 PRO MAX 256GB     SILVER    1  ->  column K
```

Linked POS rows are grouped by condition and summed **within** a condition.
They are never summed across conditions — that would advertise one handset as
available in three states at once.

---

## Template integrity

The generated file has to be uploadable to SellUp as-is, so the writer edits
the sheet XML directly instead of re-saving the workbook.

This matters. Loading the SellUp template with openpyxl and saving it converts
every empty-string cell into a truly empty one — about **28,000 cells** change
in a file where only a few hundred should. Both forms are blank to SellUp, but
"leave everything else untouched" is a hard requirement, so the tool takes the
stricter route.

Two checks run before the download button appears:

1. every zip part other than the worksheets must be **byte-identical** —
   `sharedStrings.xml`, `styles.xml`, `theme1.xml`, `workbook.xml` all unchanged
2. within the worksheets, every differing cell must sit in column G, I or K on
   a data row

On the real files that is 547 changed cells and nothing else. If either check
fails the file is not offered for download.

Running the tool twice on its own output writes zero cells and produces a
byte-identical file.

---

## Rows excluded from the sync

| Rule | Why |
|---|---|
| `TELCO` channel stock | SellUp has no telco listings; PRIMARY and TELCO are separate pools and are never combined |
| Export sets (`JP`, `TH`, `TW`, `HK`, `CN`, `KR`, `MY`, `VN`, `US`) | parallel imports, not sold here |
| `FREEBIE` / `FREEBIES` | giveaway units |

The OnePlus "bundled with a US charger" phrase is stripped before the export
test, so `ONE PLUS 15R 256GB/12 5G W US 80W CHARGER` is treated as normal
stock rather than a US export set.

Apple and Used devices are **kept** — unlike Shopee or Lazada, SellUp trades in
both.

On the 15-08-2026 report that leaves 1,237 sellable rows out of 1,318, with 81
excluded: 30 export sets, 29 TELCO, 22 freebies. Every exclusion is listed in
the Diagnostics tab.

---

## Files it takes

| Upload | Required | What it is |
|---|---|---|
| POS Masterlist | yes | `stock_report_DD-MM-YYYY.xlsx` |
| SellUp Bulk Inventory Template | yes | `INVENTORIES_*.xlsx` from the dealer portal |
| SellUp Stock Data | recommended | your existing `POS ID / SellUp SKU / Name` sheet |
| SellUp SKU Registry | optional | a registry exported by this tool previously |

Without the third or fourth file everything with stock lands in the review
queue, so upload one of them.

---

## Files it gives back

**`INVENTORIES_UPDATED_*.xlsx`** — upload this to SellUp.

**`SellUp_Match_Review_*.xlsx`** — keep this and feed it back next time. Six
tabs, styled to match the Shopee registry:

- `Summary` — the run's counts
- `Locked Matches` — what was synced, with the POS IDs behind each figure
- `New Masterlist SKUs` — the review queue, with a decision dropdown
- `Match Review` — SellUp listings with no POS source
- `Not Selling in SellUp` / `Not on SellUp Yet` — your classifications
- `Link History` — the complete SKU map

`Link History` exists because `Locked Matches` only lists SKUs that had live
POS stock that day. Without it, re-importing the registry would silently drop
the ~1,000 links whose POS row happened to be out of stock. With it the
round-trip is lossless and the registry can replace `SellUp Stock Data`
entirely.

---

## Delisting and overselling

A listing you have already linked, whose POS stock has run dry, is set to **0**
so SellUp delists it. Listings that were never linked are left blank, which
SellUp skips — so nothing is delisted by accident.

The sidebar has an anti-oversell buffer. Setting it to 2 writes 0 for any
quantity of 2 or below. Stock spread thinly across branches is the usual cause
of overselling, and the buffer is applied after Column F is read, never before.

The tool also warns when one POS row feeds more than one SellUp listing, since
that reports the same physical stock twice.

---

## Review queue

Suggestions are ranked, never applied automatically. Scoring is additive and
each suggestion shows its reasons.

Two guards stop nonsense pairings:

- **Device kind.** A POS row must be the same kind of hardware as the worksheet
  it would land on. Laptops, chargers and Apple Pencils have no SellUp sheet,
  so they are never suggested. Without this a 512GB Space Grey MacBook matches
  a 512GB Space Grey iPad on storage and colour alone.
- **Model similarity floor.** Storage and colour agreeing is not enough; the
  model names have to be recognisably related.

Colour comparison expands abbreviations (`Awes.Lilac` → `AWESOME LILAC`),
accepts spelling variants (`Space Gray` / `Space Grey`) and word reordering
(`TITANIUM BLACK` / `Black Titanium`), but will not let a single shared word
match — `BLACK` never matches `TITANIUM BLACK`.

Two helpers speed the queue up: **Accept all high-confidence** links every row
scoring 100+, and **Classify non-SellUp items** files laptops and accessories
under Not Selling. Both are reversible with **Reset all decisions**.

On the 15-08-2026 data: 432 rows to review, 319 with suggestions, 222 of those
high-confidence.

---

## Running it

```bash
git clone https://github.com/<you>/sellup-stock-sync.git
cd sellup-stock-sync
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

### Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (public, or private with Streamlit granted access).
2. On [share.streamlit.io](https://share.streamlit.io) choose **New app**.
3. Point it at the repo, branch `main`, main file `app.py`.
4. Deploy. Dependencies come from `requirements.txt`.

No secrets or API keys are needed — everything happens in the uploaded files.

`.gitignore` excludes `*.xlsx` and `*.csv` so real stock data cannot be
committed by accident.

---

## Tests

```bash
python -m pytest tests/ -v
```

50 tests covering the column mapping, POS model parsing, the `NA` / `A` split,
export and freebie exclusions, PRIMARY vs TELCO, colour matching, device-kind
classification, and the XML editor's guarantees.

The three worth knowing about:

- `test_writable_columns_are_g_i_k` — pins the output columns
- `test_pos_reads_column_f` — pins the input column
- `test_only_edited_sheet_parts_differ` — pins template integrity

---

## Layout

```
app.py                      Streamlit UI
sellup_sync/
  config.py                 columns, rules, styling — change things here
  normalize.py              text, spec, colour and device-kind normalisation
  pos.py                    POS reader and exclusion rules
  inventory.py              SellUp template reader/writer + integrity check
  seed.py                   SellUp Stock Data reader
  registry.py               registry read/write, Shopee-matching styling
  matching.py               suggestion engine
  pipeline.py               orchestration
  xlsx_editor.py            surgical XML cell editing
tests/
```

Column indices, exclusion rules and the styling palette all live in
`config.py`. If SellUp changes its template, that is the file to edit — the
header validation will fail loudly first rather than writing to wrong columns.
