"""Orchestration: turn the uploads into locked matches, a review sheet and the
quantity assignments written into the SellUp template.

Link precedence (Bug 1 fix, 28 Aug 2026)
----------------------------------------
Every ``(masterlist_id, condition)`` pair resolves to **at most one** SellUp
SKU. Sources are applied in strict precedence order and each one *consumes*
the pairs it claims, so a lower-precedence source can never re-create a link
the reviewer has already corrected:

1. reviewer suppression  (``Do Not Link``)   — consumes, links nothing
2. reviewer classification (Not Selling / Not on SellUp Yet) — consumes
3. reviewer links        (``Linked`` + SKU)  — consumes and links
4. crosswalk / carried-over links            — only over unconsumed pairs
5. automatic matching                        — only over what is still free

Before any quantity is written, :func:`assert_exclusive` re-checks the result
and fails the run loudly rather than shipping a double-count. On 28 Aug the
missing step 4 restriction turned 27 real units into 54 advertised.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import config
from .inventory import QuantityAssignment, SellUpInventory, SellUpRow
from .discriminators import Discriminators
from .normalize import UNSELLABLE_KINDS, colour_key, device_kind, squash
from .pos import PosMasterlist, PosRow
from .seed import SeedMapping


class ExclusivityError(Exception):
    """Raised when one masterlist pair would feed two different listings."""


@dataclass
class LockedMatch:
    """One SellUp listing/condition pair whose stock is computed from POS."""

    sellup: SellUpRow
    slot: str
    pos_rows: list[PosRow]
    target_stock: int
    origin: str = config.LINKED_BY_SEED

    @property
    def masterlist_ids(self) -> str:
        return ", ".join(r.stock_type_id for r in self.pos_rows)

    @property
    def masterlist_labels(self) -> str:
        return " ; ".join(f"{r.model}|{r.colour}" for r in self.pos_rows)

    @property
    def masterlist_categories(self) -> str:
        return ", ".join(sorted({r.category for r in self.pos_rows}))

    @property
    def available_quantities(self) -> str:
        return " + ".join(str(r.available_qty) for r in self.pos_rows)


@dataclass
class NewMasterlistSku:
    """A POS row with stock that has no confirmed SellUp link yet."""

    pos: PosRow
    suggestions: list = field(default_factory=list)
    decision: str = config.DECISION_UNREVIEWED
    linked_sku_id: str = ""
    notes: str = ""

    @property
    def is_reviewed(self) -> bool:
        return self.decision in config.TERMINAL_DECISIONS

    @property
    def is_actionable(self) -> bool:
        if self.decision == config.DECISION_LINKED:
            return bool(self.linked_sku_id)
        return self.is_reviewed


@dataclass
class OrphanListing:
    """A SellUp listing holding stock that no POS row feeds."""

    sellup: SellUpRow
    slot: str
    current_qty: object
    was_linked: bool


@dataclass
class ValidationIssue:
    """A problem worth surfacing but not necessarily fatal."""

    severity: str   # 'error' | 'warning' | 'info'
    message: str
    detail: str = ""


@dataclass
class PipelineResult:
    """Everything the UI and the registry writer need for a run."""

    locked: list[LockedMatch] = field(default_factory=list)
    new_skus: list[NewMasterlistSku] = field(default_factory=list)
    match_review: list[OrphanListing] = field(default_factory=list)
    not_selling: list[PosRow] = field(default_factory=list)
    not_yet: list[PosRow] = field(default_factory=list)
    suppressed: list[PosRow] = field(default_factory=list)
    assignments: list[QuantityAssignment] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    stale_pos_ids: set[str] = field(default_factory=set)
    unknown_sellup_skus: set[str] = field(default_factory=set)
    delisted_count: int = 0
    auto_linked_count: int = 0
    auto_classified_count: int = 0
    reviewer_linked_count: int = 0
    displaced_count: int = 0
    quarantined: dict[str, str] = field(default_factory=dict)
    all_links: dict[str, list[str]] = field(default_factory=dict)
    no_pos_source: set[str] = field(default_factory=set)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def unreviewed_count(self) -> int:
        return sum(1 for s in self.new_skus if not s.is_actionable)

    @property
    def export_ready(self) -> bool:
        return not self.errors

    def metrics(self) -> dict[str, int]:
        return {
            "locked_updated": len(self.locked),
            "auto_linked": self.auto_linked_count,
            "reviewer_linked": self.reviewer_linked_count,
            "displaced": self.displaced_count,
            "new_skus_detected": len(self.new_skus),
            "requiring_review": self.unreviewed_count,
            "not_selling": len(self.not_selling),
            "not_yet": len(self.not_yet),
            "suppressed": len(self.suppressed),
            "orphan_listings": len(self.match_review),
            "quarantined": len(self.quarantined),
            "validation_errors": len(self.errors) + len(self.warnings),
            "cells_to_write": len(self.assignments),
            "units_synced": sum(m.target_stock for m in self.locked),
            "delisted": self.delisted_count,
        }


def apply_buffer(quantity: int, buffer: int) -> int:
    """Anti-oversell buffer: suppress thinly-spread stock at or below ``buffer``."""
    if buffer <= 0:
        return quantity
    return 0 if quantity <= buffer else quantity


# --------------------------------------------------------------------------
# Exclusivity
# --------------------------------------------------------------------------

def _listing_identity(row: SellUpRow) -> tuple:
    """What makes two SellUp listings genuinely interchangeable.

    The model is compared through :class:`Discriminators` as well as by its
    squashed text. ``squash`` strips ``+`` as punctuation, so on its own it
    would report ``Galaxy S26`` and ``Galaxy S26+`` as the same listing — the
    very collapse behind Bug 2 — and quietly wave through a real double-count.
    """
    marks = Discriminators.of(row.model)
    return (
        row.sheet,
        squash(row.model),
        marks.variant,
        marks.number,
        marks.network,
        row.spec.storage_gb,
        row.spec.ram_gb,
        row.spec.network,
        row.spec.case_size_mm,
        colour_key(row.colour),
    )


def assert_exclusive(
    ml_to_skus: dict[str, list[str]],
    inventory: SellUpInventory,
) -> dict[str, str]:
    """Find masterlist IDs that would feed more than one distinct listing.

    Returns ``{masterlist_id: explanation}`` for each conflict.

    Two exemptions, both deliberate:

    * SellUp genuinely carries duplicate listings for the same product — the
      known case is ML 25563 against SKU-000080928 and SKU-000080930. A pair
      is only a conflict when the listings describe *different* products.
    * A SKU absent from the uploaded inventory cannot receive a write, so it
      cannot double-count and is ignored rather than reported.
    """
    by_sku = inventory.by_sku()
    conflicts: dict[str, str] = {}

    for ml_id, skus in sorted(ml_to_skus.items()):
        present = [s for s in dict.fromkeys(skus) if s in by_sku]
        if len(present) < 2:
            continue

        identities = {_listing_identity(by_sku[s]) for s in present}
        if len(identities) > 1:
            described = "; ".join(f"{s} ({by_sku[s].display})" for s in present)
            conflicts[ml_id] = (
                f"Masterlist ID {ml_id} would feed {len(present)} different "
                f"listings: {described}"
            )

    return conflicts


def build_locked_matches(
    pos: PosMasterlist,
    inventory: SellUpInventory,
    links: dict[str, list[str]],
    buffer: int = 0,
    origins: dict[str, str] | None = None,
) -> tuple[list[LockedMatch], set[str], set[str], list[ValidationIssue]]:
    """Compute target stock for every confirmed SellUp link."""
    pos_by_id = pos.by_id()
    sellup_by_sku = inventory.by_sku()
    origins = origins or {}

    locked: list[LockedMatch] = []
    stale_pos_ids: set[str] = set()
    unknown_skus: set[str] = set()
    issues: list[ValidationIssue] = []

    for sku_id, pos_ids in links.items():
        sellup_row = sellup_by_sku.get(sku_id)
        if sellup_row is None:
            if pos_ids:
                unknown_skus.add(sku_id)
            continue

        by_slot: dict[str, list[PosRow]] = defaultdict(list)
        for pos_id in pos_ids:
            pos_row = pos_by_id.get(pos_id)
            if pos_row is None:
                stale_pos_ids.add(pos_id)
                continue
            by_slot[pos_row.slot].append(pos_row)

        for slot, rows in by_slot.items():
            raw_total = sum(r.available_qty for r in rows)
            origin = config.LINKED_BY_SEED
            for row in rows:
                if origins.get(row.stock_type_id):
                    origin = origins[row.stock_type_id]
                    break
            locked.append(
                LockedMatch(
                    sellup=sellup_row,
                    slot=slot,
                    pos_rows=sorted(rows, key=lambda r: r.stock_type_id),
                    target_stock=apply_buffer(raw_total, buffer),
                    origin=origin,
                )
            )

    locked.sort(key=lambda m: (m.sellup.sheet, m.sellup.sku_id, m.slot))

    if unknown_skus:
        issues.append(
            ValidationIssue(
                "warning",
                f"{len(unknown_skus)} linked SellUp SKU(s) are not in the uploaded "
                "inventory file and were skipped.",
                ", ".join(sorted(unknown_skus)[:20]),
            )
        )
    if stale_pos_ids:
        issues.append(
            ValidationIssue(
                "info",
                f"{len(stale_pos_ids)} linked POS Stock Type ID(s) no longer appear "
                "in today's stock report. Those listings fall back to 0.",
                ", ".join(sorted(stale_pos_ids)[:20]),
            )
        )

    return locked, stale_pos_ids, unknown_skus, issues


def detect_new_masterlist_skus(
    pos: PosMasterlist,
    consumed: set[str],
) -> list[PosRow]:
    """POS rows with positive stock that nothing has claimed yet."""
    return [row for row in pos.with_stock() if row.stock_type_id not in consumed]


def build_assignments(locked: list[LockedMatch]) -> list[QuantityAssignment]:
    """Turn locked matches into concrete cell writes."""
    best: dict[tuple[str, int, str], QuantityAssignment] = {}

    for match in locked:
        key = (match.sellup.sheet, match.sellup.excel_row, match.slot)
        assignment = QuantityAssignment(
            sheet=match.sellup.sheet,
            excel_row=match.sellup.excel_row,
            slot=match.slot,
            quantity=match.target_stock,
            sku_id=match.sellup.sku_id,
        )
        existing = best.get(key)
        if existing is None or assignment.quantity > existing.quantity:
            best[key] = assignment

    return sorted(best.values(), key=lambda a: (a.sheet, a.excel_row, a.column))


def _positive(value: object) -> bool:
    """True when a SellUp Qty cell currently advertises stock."""
    if value is None or value == "":
        return False
    try:
        return float(str(value).strip()) > 0
    except (TypeError, ValueError):
        return False


def zero_out_sold_slots(
    locked: list[LockedMatch],
    inventory: SellUpInventory,
    ever_linked: set[str],
) -> tuple[list[QuantityAssignment], list[OrphanListing]]:
    """Delist listings whose POS source has gone.

    ``ever_linked`` must include every SKU that has *ever* held a link —
    including one a reviewer has just moved stock away from. Iterating only
    over today's link map was the Bug 1 knock-on: a displaced listing dropped
    out of the map entirely, so it was skipped and kept its stale quantity.
    On 28 Aug that left 14 listings advertising stock that had moved.
    """
    receiving: set[tuple[str, str]] = {(m.sellup.sku_id, m.slot) for m in locked}
    sellup_by_sku = inventory.by_sku()

    extra: list[QuantityAssignment] = []
    orphans: list[OrphanListing] = []

    for sku_id in sorted(ever_linked):
        row = sellup_by_sku.get(sku_id)
        if row is None:
            continue
        for slot in config.ALL_SLOTS:
            if (sku_id, slot) in receiving:
                continue
            if not _positive(row.current_qty.get(slot)):
                continue
            extra.append(
                QuantityAssignment(
                    sheet=row.sheet,
                    excel_row=row.excel_row,
                    slot=slot,
                    quantity=0,
                    sku_id=sku_id,
                )
            )
            orphans.append(
                OrphanListing(
                    sellup=row,
                    slot=slot,
                    current_qty=row.current_qty.get(slot),
                    was_linked=True,
                )
            )
    return extra, orphans


def find_unsourced_listings(
    inventory: SellUpInventory,
    ever_linked: set[str],
    no_pos_source: set[str],
) -> list[OrphanListing]:
    """SellUp listings holding stock that no POS row has ever fed.

    Bug 3: listings like the Honor X7a trio (SKU-000064877/8/9) held a unit
    each on SellUp but appeared on no tab of the workbook, so there was no way
    for a reviewer to reach them. Any listing advertising stock with no link
    now surfaces on the Match Review tab.
    """
    out: list[OrphanListing] = []
    for row in inventory.rows:
        if row.sku_id in ever_linked:
            continue
        for slot in config.ALL_SLOTS:
            if _positive(row.current_qty.get(slot)):
                out.append(
                    OrphanListing(
                        sellup=row,
                        slot=slot,
                        current_qty=row.current_qty.get(slot),
                        was_linked=row.sku_id in no_pos_source,
                    )
                )
    return out


def auto_resolve(
    orphans: list[PosRow],
    suggestion_index,
    min_score: int = config.AUTO_LINK_MIN_SCORE,
    classify_unsellable: bool = config.AUTO_CLASSIFY_UNSELLABLE,
) -> tuple[dict[str, str], dict[str, str], dict[str, list]]:
    """Decide the easy rows without asking."""
    links: dict[str, str] = {}
    classifications: dict[str, str] = {}
    suggestions: dict[str, list] = {}

    for pos_row in orphans:
        pos_id = pos_row.stock_type_id

        if classify_unsellable and device_kind(pos_row.brand, pos_row.model) in UNSELLABLE_KINDS:
            classifications[pos_id] = config.DECISION_NOT_SELLING
            suggestions[pos_id] = []
            continue

        ranked = suggestion_index.suggest(pos_row) if suggestion_index else []
        suggestions[pos_id] = ranked
        if ranked and ranked[0].score >= min_score:
            links[pos_id] = ranked[0].sellup.sku_id

    return links, classifications, suggestions


def run_pipeline(
    pos: PosMasterlist,
    inventory: SellUpInventory,
    seed: SeedMapping | None,
    decisions: dict[str, dict] | None = None,
    buffer: int = 0,
    suggestion_index=None,
    auto_link: bool = True,
    auto_link_min_score: int = config.AUTO_LINK_MIN_SCORE,
    auto_classify: bool = config.AUTO_CLASSIFY_UNSELLABLE,
    strict_exclusivity: bool = config.STRICT_EXCLUSIVITY,
) -> PipelineResult:
    """Execute a full sync pass.

    ``strict_exclusivity`` follows the spec literally and aborts the run
    when any masterlist ID would feed two different listings. The default
    quarantines those IDs instead: their stock is written to neither
    listing and they go to the review sheet, so a bad crosswalk row cannot
    double-count but also cannot block an otherwise good run.
    """
    result = PipelineResult()
    decisions = decisions or {}
    pos_by_id = pos.by_id()

    # ml_id -> SKU, built in precedence order. `consumed` is the guard that
    # stops a lower-precedence source re-creating a corrected link.
    ml_to_sku: dict[str, str] = {}
    consumed: set[str] = set()
    origins: dict[str, str] = {}
    classified: dict[str, str] = {}

    # -- 1 & 2. Reviewer suppression and classification -------------------
    for pos_id, entry in decisions.items():
        decision = entry.get("decision", config.DECISION_UNREVIEWED)
        if decision == config.DECISION_DO_NOT_LINK:
            consumed.add(pos_id)
            row = pos_by_id.get(pos_id)
            if row is not None:
                result.suppressed.append(row)
        elif decision in (config.DECISION_NOT_SELLING, config.DECISION_NOT_YET):
            consumed.add(pos_id)
            classified[pos_id] = decision

    # -- 3. Reviewer links ------------------------------------------------
    for pos_id, entry in decisions.items():
        if entry.get("decision") != config.DECISION_LINKED:
            continue
        sku_id = (entry.get("linked_sku_id") or "").strip()
        if not sku_id or pos_id in consumed:
            continue
        ml_to_sku[pos_id] = sku_id
        consumed.add(pos_id)
        origins[pos_id] = config.LINKED_BY_REVIEWER
    result.reviewer_linked_count = len(ml_to_sku)

    # -- 4. Crosswalk, over unconsumed pairs only -------------------------
    # This restriction is the Bug 1 fix. Previously the crosswalk ran over
    # everything and its link survived alongside the reviewer's.
    displaced: dict[str, tuple[str, str]] = {}
    for sku_id, pos_ids in (seed.links.items() if seed else ()):
        for pos_id in pos_ids:
            if pos_id in consumed:
                if ml_to_sku.get(pos_id) not in (None, sku_id):
                    displaced[pos_id] = (sku_id, ml_to_sku[pos_id])
                continue
            ml_to_sku.setdefault(pos_id, sku_id)
            consumed.add(pos_id)
    result.displaced_count = len(displaced)

    # -- 5. Automatic matching, over what is still free -------------------
    free = detect_new_masterlist_skus(pos, consumed)
    auto_links, auto_classified, suggestions = (
        auto_resolve(free, suggestion_index, auto_link_min_score, auto_classify)
        if (auto_link or auto_classify)
        else ({}, {}, {r.stock_type_id: [] for r in free})
    )

    if auto_link:
        for pos_id, sku_id in auto_links.items():
            if pos_id in consumed:
                continue
            ml_to_sku[pos_id] = sku_id
            consumed.add(pos_id)
            origins[pos_id] = config.LINKED_BY_AUTO
        result.auto_linked_count = len(auto_links)
    if auto_classify:
        for pos_id, decision in auto_classified.items():
            if pos_id in consumed:
                continue
            consumed.add(pos_id)
            classified[pos_id] = decision
        result.auto_classified_count = len(auto_classified)

    # -- Exclusivity assertion -------------------------------------------
    ml_to_skus: dict[str, list[str]] = {k: [v] for k, v in ml_to_sku.items()}
    # Genuine SellUp duplicates are carried through from the crosswalk, where
    # one masterlist ID legitimately feeds two identical listings.
    for sku_id, pos_ids in (seed.links.items() if seed else ()):
        for pos_id in pos_ids:
            if pos_id in ml_to_skus and sku_id not in ml_to_skus[pos_id]:
                if origins.get(pos_id) in (None, config.LINKED_BY_SEED):
                    ml_to_skus[pos_id].append(sku_id)

    conflicts = assert_exclusive(ml_to_skus, inventory)
    if conflicts:
        if strict_exclusivity:
            result.issues.append(
                ValidationIssue(
                    "error",
                    f"{len(conflicts)} masterlist ID(s) would feed more than one "
                    "listing. No quantities have been written.",
                    "\n".join(list(conflicts.values())[:20]),
                )
            )
            return result

        # Quarantine: drop the ambiguous links so neither listing receives the
        # stock, and route the ID to the review sheet for a human to settle.
        # Nothing is written twice, and one bad crosswalk row does not block a
        # run of 1,100 good ones. A reviewer link resolves it permanently,
        # because reviewer links consume the pair before the crosswalk runs.
        for ml_id in conflicts:
            ml_to_skus.pop(ml_id, None)
            ml_to_sku.pop(ml_id, None)
            consumed.discard(ml_id)
        result.quarantined = dict(conflicts)
        result.issues.append(
            ValidationIssue(
                "warning",
                f"{len(conflicts)} masterlist ID(s) point at two different listings "
                "in the crosswalk. Their stock was written to neither — set the "
                "correct SKU on the review sheet to settle it.",
                "\n".join(list(conflicts.values())[:20]),
            )
        )

    # -- Invert to SKU -> [ml ids] and compute stock ----------------------
    links: dict[str, list[str]] = {}
    for ml_id, skus in ml_to_skus.items():
        for sku_id in skus:
            links.setdefault(sku_id, []).append(ml_id)

    result.all_links = {sku: list(ids) for sku, ids in links.items()}
    result.no_pos_source = set(getattr(seed, "not_in_pos", set()) or set())

    locked, stale, unknown, issues = build_locked_matches(
        pos, inventory, links, buffer, origins
    )
    result.locked = locked
    result.stale_pos_ids = stale
    result.unknown_sellup_skus = unknown
    result.issues.extend(issues)

    # -- Review sheet -----------------------------------------------------
    for pos_row in detect_new_masterlist_skus(pos, consumed):
        entry = decisions.get(pos_row.stock_type_id, {})
        result.new_skus.append(
            NewMasterlistSku(
                pos=pos_row,
                suggestions=suggestions.get(pos_row.stock_type_id, []),
                decision=entry.get("decision", config.DECISION_UNREVIEWED),
                linked_sku_id=entry.get("linked_sku_id", ""),
                notes=entry.get("notes", ""),
            )
        )

    for pos_id, decision in classified.items():
        row = pos_by_id.get(pos_id)
        if row is None:
            continue
        (result.not_selling if decision == config.DECISION_NOT_SELLING
         else result.not_yet).append(row)

    # -- Writes, delisting, orphans ---------------------------------------
    ever_linked: set[str] = set(links)
    if seed:
        ever_linked |= set(seed.links)
    ever_linked |= {
        (e.get("linked_sku_id") or "").strip()
        for e in decisions.values()
        if (e.get("linked_sku_id") or "").strip()
    }

    result.assignments = build_assignments(locked)
    delistings, delisted_orphans = zero_out_sold_slots(locked, inventory, ever_linked)
    result.assignments.extend(delistings)
    result.assignments.sort(key=lambda a: (a.sheet, a.excel_row, a.column))
    result.delisted_count = len(delistings)

    result.match_review = delisted_orphans + find_unsourced_listings(
        inventory, ever_linked, result.no_pos_source
    )

    # -- Reporting --------------------------------------------------------
    if result.reviewer_linked_count:
        result.issues.append(
            ValidationIssue(
                "info",
                f"{result.reviewer_linked_count} reviewer link(s) applied. These take "
                "precedence over the crosswalk.",
            )
        )
    if displaced:
        result.issues.append(
            ValidationIssue(
                "info",
                f"{len(displaced)} crosswalk link(s) were overridden by a reviewer "
                "link and did not survive into Locked Matches.",
                "; ".join(
                    f"ML {ml}: crosswalk {old} -> reviewer {new}"
                    for ml, (old, new) in list(displaced.items())[:15]
                ),
            )
        )
    if result.auto_linked_count:
        result.issues.append(
            ValidationIssue(
                "info",
                f"{result.auto_linked_count} SKU(s) were linked automatically on a "
                f"score of {auto_link_min_score} or above, marked "
                f"'{config.LINKED_BY_AUTO}' in Locked Matches.",
            )
        )
    if result.auto_classified_count:
        result.issues.append(
            ValidationIssue(
                "info",
                f"{result.auto_classified_count} row(s) were filed under 'Not Selling "
                "in SellUp' automatically (laptops, chargers, styluses).",
            )
        )
    if result.delisted_count:
        result.issues.append(
            ValidationIssue(
                "info",
                f"{result.delisted_count} listing(s) lost their POS source and were "
                "set to 0 to delist them.",
            )
        )
    unsourced = [o for o in result.match_review if not o.was_linked]
    if unsourced:
        result.issues.append(
            ValidationIssue(
                "warning",
                f"{len(unsourced)} SellUp listing(s) hold stock but have no POS "
                "source. They are on the Match Review tab and were left untouched.",
                ", ".join(sorted({o.sellup.sku_id for o in unsourced})[:20]),
            )
        )

    if not result.assignments:
        result.issues.append(
            ValidationIssue(
                "error",
                "No quantities would be written. Check that the registry and the "
                "uploaded inventory refer to the same SellUp SKU IDs.",
            )
        )

    return result
