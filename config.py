"""Central configuration for the SellUp stock sync tool.

Every magic string, column index and business rule lives here so that a change
in the SellUp template or the POS export only needs editing in one place.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# SellUp bulk inventory template
# --------------------------------------------------------------------------
# The template ships four worksheets. Row 1 = title, row 2 = instructions,
# row 3 = header, data begins on row 4.
SELLUP_SHEETS: tuple[str, ...] = ("Smartphones", "Tablets", "Smartwatches", "Audio")

SELLUP_HEADER_ROW = 3
SELLUP_FIRST_DATA_ROW = 4

# 1-based column indices in the SellUp template.
COL_SKU_ID = 1       # A
COL_BRAND = 2        # B
COL_MODEL = 3        # C
COL_SPECS = 4        # D
COL_COLOR = 5        # E
COL_NA_PRICE = 6     # F
COL_NA_QTY = 7       # G  <- Brand New (Not Activated)   [WRITE]
COL_A_PRICE = 8      # H
COL_A_QTY = 9        # I  <- Brand New (Activated)       [WRITE]
COL_EXC_PRICE = 10   # J
COL_EXC_QTY = 11     # K  <- Used, Excellent condition    [WRITE]
COL_GOOD_PRICE = 12  # L
COL_GOOD_QTY = 13    # M
COL_FAIR_PRICE = 14  # N
COL_FAIR_QTY = 15    # O

# The ONLY columns this application is ever permitted to write to.
WRITABLE_COLUMNS: frozenset[int] = frozenset({COL_NA_QTY, COL_A_QTY, COL_EXC_QTY})

# Header text expected on row 3, used for pre-flight validation. Newlines in the
# real file are normalised to single spaces before comparison.
EXPECTED_SELLUP_HEADERS: tuple[str, ...] = (
    "SKU ID",
    "Brand",
    "Model",
    "Specs",
    "Color",
    "New (Not Activated) Price",
    "New (Not Activated) Qty",
    "New (Activated) Price",
    "New (Activated) Qty",
    "Excellent Price",
    "Excellent Qty",
    "Good Price",
    "Good Qty",
    "Fair Price",
    "Fair Qty",
)

# --------------------------------------------------------------------------
# Condition slots
# --------------------------------------------------------------------------
# A "slot" is the (SellUp SKU ID, condition) pair that receives a quantity.
# One SellUp SKU row can own up to three independent slots.
SLOT_NEW_NA = "New (Not Activated)"
SLOT_NEW_A = "New (Activated)"
SLOT_USED_EXCELLENT = "Excellent"

SLOT_TO_COLUMN: dict[str, int] = {
    SLOT_NEW_NA: COL_NA_QTY,
    SLOT_NEW_A: COL_A_QTY,
    SLOT_USED_EXCELLENT: COL_EXC_QTY,
}

ALL_SLOTS: tuple[str, ...] = (SLOT_NEW_NA, SLOT_NEW_A, SLOT_USED_EXCELLENT)

# --------------------------------------------------------------------------
# POS masterlist (stock_report_DD-MM-YYYY.xlsx)
# --------------------------------------------------------------------------
# Rows 1-2 form a two-level header; data starts on row 3.
POS_HEADER_ROW = 1
POS_SUBHEADER_ROW = 2
POS_FIRST_DATA_ROW = 3

POS_COL_STOCK_TYPE_ID = 1   # A
POS_COL_CATEGORY = 2        # B  'New' | 'Used'
POS_COL_BRAND = 3           # C
POS_COL_MODEL = 4           # D
POS_COL_COLOR = 5           # E
POS_COL_AVAILABLE_QTY = 6   # F  <- the ONLY stock figure we ever read
POS_COL_QUANTITY = 7        # G  gross on-hand, deliberately unused
POS_COL_RESERVED = 8        # H

POS_REQUIRED_HEADERS: tuple[str, ...] = (
    "Stock Type ID",
    "Category",
    "Brand",
    "Model",
    "Color",
)

# --------------------------------------------------------------------------
# Business rules
# --------------------------------------------------------------------------
# Export-set region tokens. A POS row whose model contains any of these as a
# whole word is a parallel-import set and is never sold on SellUp.
EXPORT_TOKENS: frozenset[str] = frozenset(
    {"JP", "TH", "TW", "HK", "CN", "KR", "MY", "VN", "US"}
)

# OnePlus sets bundled with a US charger legitimately contain the token "US".
# This phrase is stripped before the export-token test runs.
US_CHARGER_PHRASES: tuple[str, ...] = (
    "W US 80W CHARGER",
    "W/US CHARGER",
    "W US CHARGER",
)

# Models containing these words are giveaway units, not sellable stock.
FREEBIE_TOKENS: frozenset[str] = frozenset({"FREEBIE", "FREEBIES"})

# Channel suffixes. SellUp has no telco listings, so TELCO stock is excluded
# and PRIMARY stock is used on its own -- the two pools are never summed.
CHANNEL_PRIMARY = "PRIMARY"
CHANNEL_TELCO = "TELCO"
EXCLUDED_CHANNELS: frozenset[str] = frozenset({CHANNEL_TELCO})

# Activation tokens appearing at the end of a POS model string. Only Apple
# phones carry these; everything else is treated as Not Activated.
ACTIVATION_NOT_ACTIVATED = "NA"
ACTIVATION_ACTIVATED = "A"
DEFAULT_ACTIVATION = ACTIVATION_NOT_ACTIVATED

# --------------------------------------------------------------------------
# SKU registry workbook
# --------------------------------------------------------------------------
SHEET_SUMMARY = "Summary"
SHEET_LOCKED = "Locked Matches"
SHEET_NEW_SKUS = "New Masterlist SKUs"
SHEET_MATCH_REVIEW = "Match Review"
SHEET_NOT_SELLING = "Not Selling in SellUp"
SHEET_NOT_YET = "Not on SellUp Yet"
SHEET_LINK_HISTORY = "Link History"

# Tabs that MUST be present in an uploaded registry.
REQUIRED_REGISTRY_SHEETS: tuple[str, ...] = (
    SHEET_LOCKED,
    SHEET_NEW_SKUS,
    SHEET_MATCH_REVIEW,
    SHEET_NOT_SELLING,
    SHEET_NOT_YET,
)

LOCKED_HEADERS: tuple[str, ...] = (
    "#",
    "SellUp Sheet",
    "SellUp SKU ID",
    "SellUp Model",
    "Storage",
    "Connectivity",
    "SellUp Colour",
    "Condition",
    "LOCKED Masterlist ID(s)",
    "ML Category",
    "ML Model(s)|Color",
    "ML Available Qty",
    "Target Stock",
    "# SKUs",
)

NEW_SKUS_HEADERS: tuple[str, ...] = (
    "#",
    "Masterlist Stock Type ID",
    "Category",
    "Brand",
    "Model",
    "Color",
    "Available Qty",
    "Link to SellUp SKU ID",
    "Reviewer Decision",
    "Notes",
)

MATCH_REVIEW_HEADERS: tuple[str, ...] = (
    "#",
    "SellUp Sheet",
    "SellUp SKU ID",
    "SellUp Model",
    "Storage",
    "Connectivity",
    "SellUp Colour",
    "Condition",
    "Current Seller Stock",
    "Corrected Masterlist ID",
    "Reviewer Decision",
    "Notes",
)

# The complete SKU-to-POS link map, including links whose POS row happens to
# be out of stock today. Locked Matches only shows what was synced this run,
# so without this sheet a registry round-trip would silently lose history.
LINK_HISTORY_HEADERS: tuple[str, ...] = (
    "SellUp SKU ID",
    "LOCKED Masterlist ID(s)",
    "SellUp Model",
    "Status",
)

UNSOLD_HEADERS: tuple[str, ...] = (
    "#",
    "Masterlist Stock Type ID",
    "Category",
    "Brand",
    "Model",
    "Color",
    "Available Qty",
)

REGISTRY_HEADERS: dict[str, tuple[str, ...]] = {
    SHEET_LOCKED: LOCKED_HEADERS,
    SHEET_NEW_SKUS: NEW_SKUS_HEADERS,
    SHEET_MATCH_REVIEW: MATCH_REVIEW_HEADERS,
    SHEET_NOT_SELLING: UNSOLD_HEADERS,
    SHEET_NOT_YET: UNSOLD_HEADERS,
    SHEET_LINK_HISTORY: LINK_HISTORY_HEADERS,
}

# --------------------------------------------------------------------------
# Reviewer decisions
# --------------------------------------------------------------------------
DECISION_UNREVIEWED = ""
DECISION_LINKED = "Linked"
DECISION_NOT_SELLING = "Not Selling in SellUp"
DECISION_NOT_YET = "Not on SellUp Yet"

DECISION_OPTIONS: tuple[str, ...] = (
    DECISION_UNREVIEWED,
    DECISION_LINKED,
    DECISION_NOT_SELLING,
    DECISION_NOT_YET,
)

# A row counts as reviewed once it carries any non-blank decision.
TERMINAL_DECISIONS: frozenset[str] = frozenset(
    {DECISION_LINKED, DECISION_NOT_SELLING, DECISION_NOT_YET}
)

# --------------------------------------------------------------------------
# Output behaviour
# --------------------------------------------------------------------------
# SellUp skips a listing entirely when its Qty cell is blank, so unmatched
# rows are left untouched rather than zeroed. Locked rows whose POS stock has
# fallen to zero DO get an explicit 0 so they are delisted.
BLANK_MEANS_SKIP = True

# Anti-oversell buffer: any computed quantity at or below this threshold is
# written as 0. Default 0 = disabled, exposed as a sidebar slider.
DEFAULT_OVERSELL_BUFFER = 0
MAX_OVERSELL_BUFFER = 5

# --------------------------------------------------------------------------
# Workbook styling -- mirrors the existing Shopee Match Review registry
# --------------------------------------------------------------------------
COLOR_NAVY = "1F3864"        # index / decision headers, white bold text
COLOR_ORANGE = "F4B183"      # platform-side (SellUp) columns
COLOR_YELLOW = "FFD966"      # masterlist-side (POS) columns
COLOR_WHITE = "FFFFFF"
COLOR_BLACK = "000000"

BASE_FONT_SIZE = 10
TITLE_FONT_SIZE = 15
