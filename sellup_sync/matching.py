"""Suggestion engine for POS rows that carry no confirmed SellUp link.

Two stages, in this order:

1. **Hard discriminators.** Variant suffix, model number and network suffix
   must agree. A disagreement means *not a candidate* — score 0, not a lower
   score. This is the 28 Aug fix: previously these were scored, so colour,
   storage and RAM agreeing could outvote the one token that distinguished
   two products.
2. **Scoring.** Only pairs that survive stage 1 are scored, and the score is
   additive and explainable so it can be justified in the review sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .discriminators import Discriminators, conflict
from .inventory import SellUpInventory, SellUpRow
from .normalize import (
    SHEET_TO_KIND,
    UNSELLABLE_KINDS,
    apply_model_synonyms,
    colour_is_narrowing,
    colour_key,
    colours_match,
    device_kind,
    normalise_colour,
    squash,
    strip_family_prefix,
)
from .pos import PosRow

# Attribute weights.
W_MAKER = 20
W_STORAGE = 25
W_COLOUR = 25
W_NETWORK = 10
W_RAM = 8
W_CASE_SIZE = 8
W_MODEL_SIMILARITY = 34  # scaled by the 0..1 similarity ratio

# Penalty when the colour names genuinely differ. A softer penalty applies
# when one name is a shortening of the other ("IVORY WHITE" vs "White"),
# because that is SellUp house style rather than a different finish.
P_COLOUR_DIFFERS = 15
P_COLOUR_NARROWING = 4

MIN_SCORE = 45
MIN_MODEL_SIMILARITY = 0.55
MAX_SUGGESTIONS = 5


@dataclass
class Suggestion:
    """One candidate SellUp listing for a POS row."""

    sellup: SellUpRow
    score: int
    reasons: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if self.score >= 100:
            return "High"
        if self.score >= 75:
            return "Medium"
        return "Low"


@dataclass
class Rejection:
    """A candidate that a hard discriminator ruled out, kept for auditing."""

    sellup: SellUpRow
    reason: str


def _without_colour(base: str, colour: str) -> str:
    """Remove the row's own colour words from a model base."""
    colour_words = set(normalise_colour(colour).split())
    if not colour_words:
        return base
    kept = [w for w in base.split() if w not in colour_words]
    return " ".join(kept) if kept else base


def _model_similarity(pos_row: PosRow, sellup_row: SellUpRow) -> float:
    """Fuzzy ratio between the two model bases, family prefix removed."""
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
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        return 0.85 + 0.15 * (len(shorter) / len(longer))
    return SequenceMatcher(None, a, b).ratio()


def discriminator_conflict(pos_row: PosRow, sellup_row: SellUpRow) -> str | None:
    """Why this pair cannot be the same product, or ``None``.

    Both sides are read from the **raw model strings**, before the POS model
    code and channel tag are stripped, so nothing that distinguishes the two
    products has been discarded yet.
    """
    return conflict(
        Discriminators.of(pos_row.model), Discriminators.of(sellup_row.model)
    )


def score_pair(pos_row: PosRow, sellup_row: SellUpRow) -> Suggestion | None:
    """Score one POS row against one SellUp listing."""
    if pos_row.maker != sellup_row.maker:
        return None

    # A POS row has to be the same kind of device as the worksheet it would
    # land on. Keeps laptops, chargers and styluses out entirely.
    pos_kind = device_kind(pos_row.brand, pos_row.model)
    if pos_kind in UNSELLABLE_KINDS:
        return None
    if pos_kind != SHEET_TO_KIND.get(sellup_row.sheet, ""):
        return None

    # Hard discriminators -- these are never traded off against colour.
    if discriminator_conflict(pos_row, sellup_row) is not None:
        return None

    score = W_MAKER
    reasons: list[str] = []
    ps, ss = pos_row.spec, sellup_row.spec

    if ps.storage_gb is not None and ss.storage_gb is not None:
        if ps.storage_gb != ss.storage_gb:
            return None
        score += W_STORAGE
        reasons.append(f"storage {ps.storage_label}")

    if ps.ram_gb is not None and ss.ram_gb is not None:
        if ps.ram_gb != ss.ram_gb:
            return None
        score += W_RAM
        reasons.append(f"RAM {ps.ram_gb}GB")

    if ps.case_size_mm is not None and ss.case_size_mm is not None:
        if ps.case_size_mm != ss.case_size_mm:
            return None
        score += W_CASE_SIZE
        reasons.append(f"{ps.case_size_mm}mm")

    if ps.network and ss.network:
        if ps.network != ss.network:
            return None
        score += W_NETWORK
        reasons.append(ps.network)

    if colours_match(pos_row.colour, sellup_row.colour):
        score += W_COLOUR
        reasons.append(f"colour {normalise_colour(pos_row.colour).title()}")
    elif colour_key(pos_row.colour) and colour_key(sellup_row.colour):
        narrowing = colour_is_narrowing(pos_row.colour, sellup_row.colour)
        score -= P_COLOUR_NARROWING if narrowing else P_COLOUR_DIFFERS
        reasons.append(
            ("colour shortened: " if narrowing else "COLOUR DIFFERS: ")
            + f"POS '{pos_row.colour}' vs SellUp '{sellup_row.colour}'"
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
    """Pre-bucketed SellUp rows so suggestion lookup stays fast."""

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

    def rejections(self, pos_row: PosRow, limit: int = 5) -> list[Rejection]:
        """Candidates a hard discriminator ruled out, for the audit report."""
        out: list[Rejection] = []
        for candidate in self.candidates(pos_row):
            if pos_row.maker != candidate.maker:
                continue
            reason = discriminator_conflict(pos_row, candidate)
            if reason is not None:
                out.append(Rejection(sellup=candidate, reason=reason))
            if len(out) >= limit:
                break
        return out


def audit_link(pos_row: PosRow, sellup_row: SellUpRow) -> str | None:
    """Check an existing lock. Returns a reason when it should not stand.

    Used to re-examine links carried in from the crosswalk, which is how the
    16 wrong locks were found. A link is reported when a hard discriminator
    disagrees, never merely because its score is low.
    """
    if pos_row.maker != sellup_row.maker:
        return f"different manufacturer: {pos_row.maker} vs {sellup_row.maker}"
    reason = discriminator_conflict(pos_row, sellup_row)
    if reason is not None:
        return reason
    ps, ss = pos_row.spec, sellup_row.spec
    if ps.storage_gb is not None and ss.storage_gb is not None and ps.storage_gb != ss.storage_gb:
        return f"storage {ps.storage_label} vs {ss.storage_label}"
    if ps.ram_gb is not None and ss.ram_gb is not None and ps.ram_gb != ss.ram_gb:
        return f"RAM {ps.ram_gb}GB vs {ss.ram_gb}GB"
    return None
