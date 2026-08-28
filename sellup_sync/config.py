"""Central configuration for the SellUp stock sync tool.

Every magic string, column index and business rule lives here so that a change
in the SellUp template or the POS export only needs editing in one place.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# SellUp bulk inventory template
# --------------------------------------------------------------------------
SELLUP_SHEETS: tuple[str, ...] = ("Smartphones", "Tablets", "Smartwatches", "Audio")

SELLUP_HEADER_ROW = 3
SELLUP_FIRST_DATA_ROW = 4

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

WRITABLE_COLUMNS: frozenset[int] = frozenset({COL_NA_QTY, COL_A_QTY, COL_EXC_QTY})

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
# POS masterlist
# --------------------------------------------------------------------------
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
EXPORT_TOKENS: frozenset[str] = frozenset(
    {"JP", "TH", "TW", "HK", "CN", "KR", "MY", "VN", "US"}
)

US_CHARGER_PHRASES: tuple[str, ...] = (
    "W US 80W CHARGER",
    "W/US CHARGER",
    "W US CHARGER",
)

FREEBIE_TOKENS: frozenset[str] = frozenset({"FREEBIE", "FREEBIES"})

CHANNEL_PRIMARY = "PRIMARY"
CHANNEL_TELCO = "TELCO"
EXCLUDED_CHANNELS: frozenset[str] = frozenset({CHANNEL_TELCO})

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
    "How linked",
)

NEW_SKUS_HEADERS: tuple[str, ...] = (
    "#",
    "Masterlist Stock Type ID",
    "Category",
    "Brand",
    "Model",
    "Color",
    "Available Qty",
    "Condition",
    "Suggested SellUp SKU ID",
    "Suggested SellUp Listing",
    "Confidence",
    "Score",
    "Why",
    "Alternative 2",
    "Alternative 3",
    "Link to SellUp SKU ID",
    "Reviewer Decision",
    "Notes",
)

NEW_SKUS_INPUT_COLUMNS: tuple[str, ...] = (
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
    "Status",
    "Corrected Masterlist ID",
    "Reviewer Decision",
    "Notes",
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

LINK_HISTORY_HEADERS: tuple[str, ...] = (
    "SellUp SKU ID",
    "LOCKED Masterlist ID(s)",
    "SellUp Model",
    "Status",
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
# Bug 3: lets the reviewer say "this must not be linked to anything", which
# suppresses both the crosswalk and automatic matching for that row.
DECISION_DO_NOT_LINK = "Do Not Link"

DECISION_OPTIONS: tuple[str, ...] = (
    DECISION_UNREVIEWED,
    DECISION_LINKED,
    DECISION_NOT_SELLING,
    DECISION_NOT_YET,
    DECISION_DO_NOT_LINK,
)

TERMINAL_DECISIONS: frozenset[str] = frozenset(
    {DECISION_LINKED, DECISION_NOT_SELLING, DECISION_NOT_YET, DECISION_DO_NOT_LINK}
)

# --------------------------------------------------------------------------
# Automatic linking
# --------------------------------------------------------------------------
# When True, any masterlist ID feeding two different listings aborts the run
# (the spec's literal 'fail loudly'). When False the offending IDs are
# quarantined instead: written to neither listing and sent to the review
# sheet, so one bad crosswalk row cannot block 1,100 good ones.
STRICT_EXCLUSIVITY = False

AUTO_LINK_MIN_SCORE = 100
AUTO_CLASSIFY_UNSELLABLE = True

LINKED_BY_SEED = "carried over"
LINKED_BY_AUTO = "auto-linked"
LINKED_BY_REVIEWER = "reviewed"

# --------------------------------------------------------------------------
# Output behaviour
# --------------------------------------------------------------------------
BLANK_MEANS_SKIP = True

DEFAULT_OVERSELL_BUFFER = 0
MAX_OVERSELL_BUFFER = 5

# --------------------------------------------------------------------------
# Workbook styling -- mirrors the existing Shopee Match Review registry
# --------------------------------------------------------------------------
COLOR_NAVY = "1F3864"
COLOR_ORANGE = "F4B183"
COLOR_YELLOW = "FFD966"
COLOR_GREEN = "C6E0B4"
COLOR_WHITE = "FFFFFF"
COLOR_BLACK = "000000"

BASE_FONT_SIZE = 10
TITLE_FONT_SIZE = 15
