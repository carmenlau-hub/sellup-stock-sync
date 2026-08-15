"""Orchestration: turn the four uploads into locked matches, review queues and
the quantity assignments that get written into the SellUp template.

The pipeline is a pure function of its inputs plus the reviewer decisions held
in session state. Re-running it after a decision changes is cheap and always
produces a consistent picture, which is what keeps deduplication honest: a SKU
marked ``Linked`` becomes part of the locked map on the very next run and can
never resurface in the New Masterlist SKUs queue.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import config
from .inventory import QuantityAssignment, SellUpInventory, SellUpRow
from .pos import PosMasterlist, PosRow
from .seed import SeedMapping


@dataclass
class LockedMatch:
    """One SellUp listing/condition pair whose stock is computed from POS."""

    sellup: SellUpRow
    slot: str
    pos_rows: list[PosRow]
    target_stock: int

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
        """Linked rows need a SellUp SKU before they count as complete."""
        if self.decision == config.DECISION_LINKED:
            return bool(self.linked_sku_id)
        return self.is_reviewed


@dataclass
class ValidationIssue:
    """A problem worth surfacing but not necessarily fatal."""

    severity: str   # 'error' | 'warning' | 'info'
    message: str
    detail: str = ""


@dataclass
class PipelineResult:
    """Everything the UI needs to render a run."""

    locked: list[LockedMatch] = field(default_factory=list)
    new_skus: list[NewMasterlistSku] = field(default_factory=list)
    match_review: list[SellUpRow] = field(default_factory=list)
    not_selling: list[PosRow] = field(default_factory=list)
    not_yet: list[PosRow] = field(default_factory=list)
    assignments: list[QuantityAssignment] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    stale_pos_ids: set[str] = field(default_factory=set)
    unknown_sellup_skus: set[str] = field(default_factory=set)
    delisted_count: int = 0
    # The complete SKU -> POS ID map, including links with no live POS row.
    # Locked Matches only covers what was synced today, so this is what makes
    # the exported registry a lossless record of the matching history.
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
        """The download guardrail: no errors, and every new SKU dealt with."""
        return not self.errors and self.unreviewed_count == 0

    def metrics(self) -> dict[str, int]:
        return {
            "locked_updated": len(self.locked),
            "new_skus_detected": len(self.new_skus),
            "requiring_review": self.unreviewed_count,
            "not_selling": len(self.not_selling),
            "not_yet": len(self.not_yet),
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


def build_locked_matches(
    pos: PosMasterlist,
    inventory: SellUpInventory,
    links: dict[str, list[str]],
    buffer: int = 0,
) -> tuple[list[LockedMatch], set[str], set[str], list[ValidationIssue]]:
    """Compute target stock for every confirmed SellUp link.

    ``links`` maps a SellUp SKU ID to the POS Stock Type IDs feeding it. POS
    rows are grouped by condition slot so a single listing can receive a
    Not Activated, an Activated and an Excellent quantity independently.
    """
    pos_by_id = pos.by_id()
    sellup_by_sku = inventory.by_sku()

    locked: list[LockedMatch] = []
    stale_pos_ids: set[str] = set()
    unknown_skus: set[str] = set()
    issues: list[ValidationIssue] = []

    for sku_id, pos_ids in links.items():
        sellup_row = sellup_by_sku.get(sku_id)
        if sellup_row is None:
            unknown_skus.add(sku_id)
            continue

        # Group the linked POS rows by the column they feed.
        by_slot: dict[str, list[PosRow]] = defaultdict(list)
        for pos_id in pos_ids:
            pos_row = pos_by_id.get(pos_id)
            if pos_row is None:
                stale_pos_ids.add(pos_id)
                continue
            by_slot[pos_row.slot].append(pos_row)

        for slot, rows in by_slot.items():
            raw_total = sum(r.available_qty for r in rows)
            locked.append(
                LockedMatch(
                    sellup=sellup_row,
                    slot=slot,
                    pos_rows=sorted(rows, key=lambda r: r.stock_type_id),
                    target_stock=apply_buffer(raw_total, buffer),
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
    linked_pos_ids: set[str],
    classified_pos_ids: set[str],
) -> list[PosRow]:
    """POS rows with positive stock that are not accounted for anywhere.

    A row qualifies when it has stock above zero and does not appear in Locked
    Matches, Not Selling in SellUp, or Not on SellUp Yet.
    """
    accounted = linked_pos_ids | classified_pos_ids
    return [
        row
        for row in pos.with_stock()
        if row.stock_type_id not in accounted
    ]


def build_assignments(
    locked: list[LockedMatch],
    buffer: int = 0,
) -> list[QuantityAssignment]:
    """Turn locked matches into concrete cell writes.

    When two locked matches target the same cell -- which can happen if a POS
    row is linked to a listing twice -- the larger quantity wins and the clash
    is reported by :func:`detect_assignment_clashes`.
    """
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
    links: dict[str, list[str]],
) -> tuple[list[QuantityAssignment], int]:
    """Delist confirmed listings whose POS source has run dry.

    A SKU that Carmen has already linked, but which receives no quantity this
    run -- because its POS row sold out and dropped off the report, or because
    that condition has no stock left -- would otherwise keep its old quantity
    on SellUp and oversell. Those cells are explicitly set to 0.

    Only SKUs present in the confirmed link map are touched, and only slots
    that currently advertise positive stock. Listings that were never linked
    are left blank so SellUp skips them entirely.
    """
    receiving: set[tuple[str, str]] = {(m.sellup.sku_id, m.slot) for m in locked}
    sellup_by_sku = inventory.by_sku()

    extra: list[QuantityAssignment] = []
    for sku_id in links:
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
    return extra, len(extra)


def detect_assignment_clashes(locked: list[LockedMatch]) -> list[ValidationIssue]:
    """Flag POS rows whose stock feeds more than one SellUp listing.

    This is the main double-count risk: one physical pool of handsets shown as
    available under two different SKUs.
    """
    pos_to_targets: dict[str, set[str]] = defaultdict(set)
    for match in locked:
        for pos_row in match.pos_rows:
            pos_to_targets[pos_row.stock_type_id].add(f"{match.sellup.sku_id}/{match.slot}")

    shared = {k: v for k, v in pos_to_targets.items() if len(v) > 1}
    if not shared:
        return []

    sample = "; ".join(
        f"POS {pid} -> {', '.join(sorted(targets))}"
        for pid, targets in list(shared.items())[:10]
    )
    return [
        ValidationIssue(
            "warning",
            f"{len(shared)} POS row(s) feed more than one SellUp listing. Their "
            "stock is reported in full against each listing, which can oversell.",
            sample,
        )
    ]


def run_pipeline(
    pos: PosMasterlist,
    inventory: SellUpInventory,
    seed: SeedMapping | None,
    decisions: dict[str, dict] | None = None,
    buffer: int = 0,
    suggestion_index=None,
) -> PipelineResult:
    """Execute a full sync pass.

    ``decisions`` is the session-state map of POS Stock Type ID to
    ``{"decision": str, "linked_sku_id": str, "notes": str}``. Decisions taken
    in the UI are folded into the link map before anything else runs, so a SKU
    linked a moment ago is already locked on this pass.
    """
    result = PipelineResult()
    decisions = decisions or {}

    # 1. Start from the confirmed links carried in from previous runs.
    links: dict[str, list[str]] = {
        sku: list(ids) for sku, ids in (seed.links.items() if seed else ())
    }

    # 2. Fold in this session's decisions. This is the deduplication step.
    classified_pos_ids: set[str] = set()
    classified: dict[str, str] = {}

    for pos_id, entry in decisions.items():
        decision = entry.get("decision", config.DECISION_UNREVIEWED)
        if decision == config.DECISION_LINKED:
            sku_id = (entry.get("linked_sku_id") or "").strip()
            if sku_id:
                bucket = links.setdefault(sku_id, [])
                if pos_id not in bucket:
                    bucket.append(pos_id)
        elif decision in (config.DECISION_NOT_SELLING, config.DECISION_NOT_YET):
            classified_pos_ids.add(pos_id)
            classified[pos_id] = decision

    # Preserve the complete map before anything filters it down.
    result.all_links = {sku: list(ids) for sku, ids in links.items()}
    result.no_pos_source = set(getattr(seed, "not_in_pos", set()) or set())

    # 3. Compute target stock for every link.
    locked, stale, unknown, issues = build_locked_matches(pos, inventory, links, buffer)
    result.locked = locked
    result.stale_pos_ids = stale
    result.unknown_sellup_skus = unknown
    result.issues.extend(issues)
    result.issues.extend(detect_assignment_clashes(locked))

    # 4. Anything with stock and no home becomes a review item.
    linked_pos_ids = {pid for ids in links.values() for pid in ids}
    orphans = detect_new_masterlist_skus(pos, linked_pos_ids, classified_pos_ids)

    for pos_row in orphans:
        entry = decisions.get(pos_row.stock_type_id, {})
        result.new_skus.append(
            NewMasterlistSku(
                pos=pos_row,
                suggestions=(
                    suggestion_index.suggest(pos_row) if suggestion_index else []
                ),
                decision=entry.get("decision", config.DECISION_UNREVIEWED),
                linked_sku_id=entry.get("linked_sku_id", ""),
                notes=entry.get("notes", ""),
            )
        )

    # 5. Route classified POS rows to their tabs.
    pos_by_id = pos.by_id()
    for pos_id, decision in classified.items():
        pos_row = pos_by_id.get(pos_id)
        if pos_row is None:
            continue
        if decision == config.DECISION_NOT_SELLING:
            result.not_selling.append(pos_row)
        else:
            result.not_yet.append(pos_row)

    # 6. SellUp listings previously reviewed as having no POS source.
    if seed:
        sellup_by_sku = inventory.by_sku()
        result.match_review = [
            sellup_by_sku[sku] for sku in sorted(seed.not_in_pos) if sku in sellup_by_sku
        ]

    # 7. Cell writes, plus delisting for confirmed links that ran dry.
    result.assignments = build_assignments(locked, buffer)
    delistings, delisted_count = zero_out_sold_slots(locked, inventory, links)
    result.assignments.extend(delistings)
    result.assignments.sort(key=lambda a: (a.sheet, a.excel_row, a.column))
    result.delisted_count = delisted_count

    if delisted_count:
        result.issues.append(
            ValidationIssue(
                "info",
                f"{delisted_count} previously-linked listing(s) had no POS stock "
                "this run and were set to 0 to delist them.",
            )
        )

    if not result.assignments:
        result.issues.append(
            ValidationIssue(
                "error",
                "No quantities would be written. Check that the seed mapping and "
                "the uploaded inventory refer to the same SellUp SKU IDs.",
            )
        )

    return result
