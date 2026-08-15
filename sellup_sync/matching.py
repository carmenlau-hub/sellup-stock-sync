"""Suggestion engine for POS rows that carry no confirmed SellUp link.

Automatic linking is deliberately **not** performed. Confirmed links come only
from the seed mapping or from Carmen ticking a row in the UI; everything this
module produces is a ranked suggestion for a human to accept or reject.

Scoring is transparent rather than clever: each agreeing attribute adds a
fixed weight, so a suggestion's score can be explained in one sentence in the
review table.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .inventory import SellUpInventory, SellUpRow
from .normalize import (
    SHEET_TO_KIND,
    UNSELLABLE_KINDS,
    apply_model_synonyms,
    colour_key,
    colours_match,
    device_kind,
    normalise_colour,
    squash,
    strip_family_prefix,
)
from .pos import PosRow

# Attribute weights. Storage and colour dominate because they are the two
# fields that most often distinguish otherwise identical listings.
W_MAKER = 20
W_STORAGE = 25
W_COLOUR = 25
W_NETWORK = 10
W_RAM = 8
W_CASE_SIZE = 8
W_MODEL_SIMILARITY = 34  # scaled by the 0..1 similarity ratio

# A suggestion below this score is not worth showing.
MIN_SCORE = 45

# Storage and colour agreeing is not enough on its own: a 512GB Space Grey
# MacBook would otherwise look like a 512GB Space Grey iPad. The model names
# themselves have to be recognisably related before a pair is offered.
MIN_MODEL_SIMILARITY = 0.55

MAX_SUGGESTIONS = 5


@dataclass
class Suggestion:
    """One candidate SellUp listing for a POS row."""

    sellup: SellUpRow
    score: int
    reasons: list[str]

    @property
    def confidence(self) -> str:
        if self.score >= 100:
            return "High"
        if self.score >= 75:
            return "Medium"
        return "Low"


def _without_colour(base: str, colour: str) -> str:
    """Remove the row's own colour words from a model base.

    POS repeats the finish inside the model string for watches
    ("SE 3 MIDNIGHT ALUMINIUM"), while SellUp keeps colour in its own column
    ("Watch SE 3 Aluminium"). Colour is scored separately, so leaving it in
    the base would penalise a correct pair twice.
    """
    colour_words = set(normalise_colour(colour).split())
    if not colour_words:
        return base
    kept = [w for w in base.split() if w not in colour_words]
    return " ".join(kept) if kept else base


def _model_similarity(pos_row: PosRow, sellup_row: SellUpRow) -> float:
    """Fuzzy ratio between the two model bases, family prefix removed.

    POS writes ``"S25 ULTRA"`` where SellUp writes ``"Galaxy S25 Ultra"``, so
    the marketing family word is stripped before comparing.
    """
    pos_base = _without_colour(pos_row.spec.base, pos_row.colour)
    sellup_base = _without_colour(
        strip_family_prefix(sellup_row.spec.base), sellup_row.colour
    )
    a = squash(apply_model_synonyms(pos_base))
    b = squash(apply_model_synonyms(sellup_base))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Containment is common: POS "17 PRO MAX" inside SellUp "IPHONE 17 PRO MAX".
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        return 0.85 + 0.15 * (len(shorter) / len(longer))
    return SequenceMatcher(None, a, b).ratio()


def score_pair(pos_row: PosRow, sellup_row: SellUpRow) -> Suggestion | None:
    """Score one POS row against one SellUp listing."""
    score = 0
    reasons: list[str] = []

    if pos_row.maker != sellup_row.maker:
        # Different manufacturer is an immediate disqualification.
        return None
    score += W_MAKER

    # A POS row has to be the same kind of device as the worksheet it would
    # land on. This is what keeps laptops, chargers and Apple Pencils out of
    # the phone and tablet sheets entirely.
    pos_kind = device_kind(pos_row.brand, pos_row.model)
    if pos_kind in UNSELLABLE_KINDS:
        return None
    if pos_kind != SHEET_TO_KIND.get(sellup_row.sheet, ""):
        return None

    ps, ss = pos_row.spec, sellup_row.spec

    # Storage must agree when both sides state it; a mismatch disqualifies.
    if ps.storage_gb is not None and ss.storage_gb is not None:
        if ps.storage_gb != ss.storage_gb:
            return None
        score += W_STORAGE
        reasons.append(f"storage {ps.storage_label}")

    # RAM is often absent on the SellUp side, so disagreement only costs points.
    if ps.ram_gb is not None and ss.ram_gb is not None:
        if ps.ram_gb == ss.ram_gb:
            score += W_RAM
            reasons.append(f"RAM {ps.ram_gb}GB")
        else:
            return None

    # Watch case size behaves like storage.
    if ps.case_size_mm is not None and ss.case_size_mm is not None:
        if ps.case_size_mm != ss.case_size_mm:
            return None
        score += W_CASE_SIZE
        reasons.append(f"{ps.case_size_mm}mm")

    if ps.network and ss.network:
        if ps.network == ss.network:
            score += W_NETWORK
            reasons.append(ps.network)
        else:
            return None

    if colours_match(pos_row.colour, sellup_row.colour):
        score += W_COLOUR
        reasons.append(f"colour {normalise_colour(pos_row.colour).title()}")
    elif colour_key(pos_row.colour) and colour_key(sellup_row.colour):
        # Colour disagreement is survivable but heavily penalised, because
        # colour names differ legitimately between the two systems.
        score -= 15
        reasons.append(
            f"colour differs: POS '{pos_row.colour}' vs SellUp '{sellup_row.colour}'"
        )

    similarity = _model_similarity(pos_row, sellup_row)
    if similarity < MIN_MODEL_SIMILARITY:
        return None
    score += int(W_MODEL_SIMILARITY * similarity)
    if similarity >= 0.99:
        reasons.append("model name exact")
    elif similarity >= 0.8:
        reasons.append("model name close")

    if score < MIN_SCORE:
        return None

    return Suggestion(sellup=sellup_row, score=score, reasons=reasons)


class SuggestionIndex:
    """Pre-bucketed SellUp rows so suggestion lookup stays fast.

    A linear scan of ~6,750 listings for each of ~1,300 POS rows is 8.8M
    comparisons and takes far too long inside a Streamlit rerun. Bucketing by
    (manufacturer, storage) cuts that to a few dozen candidates per POS row.
    """

    def __init__(self, inventory: SellUpInventory, exclude_skus: set[str] | None = None):
        self._buckets: dict[tuple, list[SellUpRow]] = {}
        self._by_maker: dict[str, list[SellUpRow]] = {}
        excluded = exclude_skus or set()

        for row in inventory.rows:
            if row.sku_id in excluded:
                continue
            self._buckets.setdefault((row.maker, row.spec.storage_gb), []).append(row)
            self._by_maker.setdefault(row.maker, []).append(row)

    def candidates(self, pos_row: PosRow) -> list[SellUpRow]:
        """Plausible SellUp rows for a POS row, before scoring."""
        key = (pos_row.maker, pos_row.spec.storage_gb)
        found = list(self._buckets.get(key, ()))
        # Listings that omit storage entirely (audio) still need considering.
        if pos_row.spec.storage_gb is not None:
            found += self._buckets.get((pos_row.maker, None), ())
        elif not found:
            found = list(self._by_maker.get(pos_row.maker, ()))
        return found

    def suggest(self, pos_row: PosRow, limit: int = MAX_SUGGESTIONS) -> list[Suggestion]:
        """Ranked suggestions for a POS row, best first."""
        scored: list[Suggestion] = []
        for candidate in self.candidates(pos_row):
            suggestion = score_pair(pos_row, candidate)
            if suggestion is not None:
                scored.append(suggestion)
        scored.sort(key=lambda s: (-s.score, s.sellup.sku_id))
        return scored[:limit]
