"""SellUp Stock Bulk Update — Streamlit front end.

Mirrors the layout of the existing Shopee stock sync tool: uploads in the
sidebar, a metrics dashboard across the top, then tabs for the registry.

Run locally:      streamlit run app.py
Deployed on:      Streamlit Community Cloud
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import streamlit as st

from sellup_sync import config
from sellup_sync.inventory import (
    InventoryParseError,
    diff_against_source,
    load_inventory,
    write_quantities,
)
from sellup_sync.matching import SuggestionIndex
from sellup_sync.normalize import UNSELLABLE_KINDS, device_kind
from sellup_sync.pipeline import run_pipeline
from sellup_sync.pos import PosParseError, load_pos_masterlist, summarise_pos
from sellup_sync.registry import (
    RegistryParseError,
    build_registry_workbook,
    load_registry,
)
from sellup_sync.seed import SeedParseError, load_seed_mapping, summarise_seed

st.set_page_config(
    page_title="SellUp Stock Bulk Update",
    page_icon="📦",
    layout="wide",
)

# --------------------------------------------------------------------------
# Styling
# --------------------------------------------------------------------------
st.markdown(
    """
    <style>
      .block-container { padding-top: 2rem; }
      div[data-testid="stMetricValue"] { font-size: 1.6rem; }
      .stDownloadButton button { width: 100%; }
      .pill {
        display:inline-block; padding:2px 10px; border-radius:10px;
        font-size:0.75rem; font-weight:600; margin-right:6px;
      }
      .pill-high { background:#d4edda; color:#155724; }
      .pill-med  { background:#fff3cd; color:#856404; }
      .pill-low  { background:#f8d7da; color:#721c24; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------
def _init_state() -> None:
    st.session_state.setdefault("decisions", {})
    st.session_state.setdefault("buffer", config.DEFAULT_OVERSELL_BUFFER)


_init_state()


def record_decision(pos_id: str, decision: str, sku_id: str = "", notes: str = "") -> None:
    """Persist a reviewer decision.

    Storing decisions in session state is what makes deduplication work: the
    next pipeline run folds every ``Linked`` entry into the locked map, so the
    SKU cannot reappear in the New Masterlist SKUs queue.
    """
    if decision in config.TERMINAL_DECISIONS:
        st.session_state["decisions"][pos_id] = {
            "decision": decision,
            "linked_sku_id": sku_id,
            "notes": notes,
        }
    else:
        st.session_state["decisions"].pop(pos_id, None)


# --------------------------------------------------------------------------
# Cached loaders
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _load_pos(raw: bytes):
    return load_pos_masterlist(io.BytesIO(raw))


@st.cache_data(show_spinner=False)
def _load_inventory(raw: bytes):
    return load_inventory(raw)


@st.cache_data(show_spinner=False)
def _load_seed(raw: bytes):
    return load_seed_mapping(io.BytesIO(raw))


@st.cache_data(show_spinner=False)
def _load_registry(raw: bytes):
    return load_registry(io.BytesIO(raw))


# --------------------------------------------------------------------------
# Sidebar — uploads
# --------------------------------------------------------------------------
st.sidebar.title("📦 SellUp Stock Sync")
st.sidebar.caption("Mister Mobile · bulk stock update")

st.sidebar.subheader("1. Required files")
pos_file = st.sidebar.file_uploader(
    "POS Masterlist", type=["xlsx", "xlsm", "csv"],
    help="stock_report_DD-MM-YYYY.xlsx exported from POS.",
)
inventory_file = st.sidebar.file_uploader(
    "SellUp Bulk Inventory Template", type=["xlsx"],
    help="INVENTORIES_*.xlsx downloaded from the SellUp dealer portal.",
)

st.sidebar.subheader("2. Link history")
seed_file = st.sidebar.file_uploader(
    "SellUp Stock Data (previous matches)", type=["xlsx"],
    help="Three-column sheet: POS Stock Type ID | SellUp Variation ID | Name.",
)
registry_file = st.sidebar.file_uploader(
    "SellUp SKU Registry (optional)", type=["xlsx"],
    help="A registry exported by this tool on a previous run.",
)

st.sidebar.subheader("3. Settings")
st.session_state["buffer"] = st.sidebar.slider(
    "Anti-oversell buffer",
    0, config.MAX_OVERSELL_BUFFER, st.session_state["buffer"],
    help="Quantities at or below this number are written as 0. "
         "Thinly-spread single units are the most common oversell cause. "
         "0 disables the buffer.",
)

if st.sidebar.button("Reset all decisions", use_container_width=True):
    st.session_state["decisions"] = {}
    st.rerun()

st.sidebar.caption(
    f"{len(st.session_state['decisions'])} decision(s) held in this session."
)


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
st.title("SellUp Stock Bulk Update")
st.caption(
    "Writes POS Available Quantity into columns **G** (New · Not Activated), "
    "**I** (New · Activated) and **K** (Used · Excellent). "
    "Every other cell in the template is left exactly as uploaded."
)

if not pos_file or not inventory_file:
    st.info(
        "Upload the **POS Masterlist** and the **SellUp Bulk Inventory Template** "
        "in the sidebar to begin. Adding **SellUp Stock Data** carries your "
        "existing matches across so you only review what is genuinely new."
    )
    st.stop()


# --------------------------------------------------------------------------
# Pre-flight validation
# --------------------------------------------------------------------------
errors: list[str] = []
pos_data = inventory_data = seed_data = registry_data = None

with st.status("Validating uploads…", expanded=False) as status:
    try:
        pos_data = _load_pos(pos_file.getvalue())
        st.write(f"POS masterlist: {len(pos_data.rows):,} sellable rows")
    except PosParseError as exc:
        errors.append(f"**POS Masterlist**\n\n{exc}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        errors.append(f"**POS Masterlist** could not be read: {exc}")

    try:
        inventory_data = _load_inventory(inventory_file.getvalue())
        st.write(f"SellUp inventory: {len(inventory_data.rows):,} listings")
    except InventoryParseError as exc:
        errors.append(f"**SellUp Inventory Template**\n\n{exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"**SellUp Inventory Template** could not be read: {exc}")

    if seed_file is not None:
        try:
            seed_data = _load_seed(seed_file.getvalue())
            st.write(f"Seed mapping: {len(seed_data.links):,} linked SKUs")
        except SeedParseError as exc:
            errors.append(f"**SellUp Stock Data**\n\n{exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"**SellUp Stock Data** could not be read: {exc}")

    if registry_file is not None:
        try:
            registry_data = _load_registry(registry_file.getvalue())
            st.write(f"Registry: {len(registry_data.links):,} locked SKUs")
        except RegistryParseError as exc:
            errors.append(f"**SellUp SKU Registry**\n\n{exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"**SellUp SKU Registry** could not be read: {exc}")

    status.update(
        label="Validation failed" if errors else "Uploads validated",
        state="error" if errors else "complete",
    )

if errors:
    st.error("#### Cannot continue — fix the file(s) below and re-upload.")
    for message in errors:
        st.error(message)
    st.stop()


# --------------------------------------------------------------------------
# Merge link sources and run the pipeline
# --------------------------------------------------------------------------
merged_links: dict[str, list[str]] = {}
merged_not_in_pos: set[str] = set()

for source in (seed_data, registry_data):
    if source is None:
        continue
    for sku, ids in source.links.items():
        bucket = merged_links.setdefault(sku, [])
        for pid in ids:
            if pid not in bucket:
                bucket.append(pid)

if seed_data is not None:
    merged_not_in_pos |= seed_data.not_in_pos
if registry_data is not None:
    merged_not_in_pos |= registry_data.no_pos_source


class _Links:
    """Adapter presenting the merged sources with the SeedMapping interface."""

    def __init__(self, links, not_in_pos):
        self.links = links
        self.not_in_pos = not_in_pos


# Decisions carried in from a registry are seeded once, then the live session
# state takes precedence so the reviewer can change their mind.
decisions = dict(getattr(registry_data, "decisions", {}) or {})
decisions.update(st.session_state["decisions"])


@st.cache_resource(show_spinner=False)
def _suggestion_index(_inventory, key: str):
    return SuggestionIndex(_inventory)


index = _suggestion_index(inventory_data, inventory_file.name)

with st.spinner("Matching stock…"):
    result = run_pipeline(
        pos=pos_data,
        inventory=inventory_data,
        seed=_Links(merged_links, merged_not_in_pos),
        decisions=decisions,
        buffer=st.session_state["buffer"],
        suggestion_index=index,
    )

metrics = result.metrics()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
st.subheader("Run summary")
row1 = st.columns(5)
row1[0].metric("Locked matches updated", f"{metrics['locked_updated']:,}")
row1[1].metric("New masterlist SKUs", f"{metrics['new_skus_detected']:,}")
row1[2].metric(
    "Requiring review",
    f"{metrics['requiring_review']:,}",
    delta=None if metrics["requiring_review"] == 0 else "blocks export",
    delta_color="inverse",
)
row1[3].metric("Not selling in SellUp", f"{metrics['not_selling']:,}")
row1[4].metric("Not on SellUp yet", f"{metrics['not_yet']:,}")

row2 = st.columns(5)
row2[0].metric("Quantity cells to write", f"{metrics['cells_to_write']:,}")
row2[1].metric("Units synced", f"{metrics['units_synced']:,}")
row2[2].metric("Listings delisted (set 0)", f"{metrics['delisted']:,}")
row2[3].metric("Errors", f"{len(result.errors):,}")
row2[4].metric("Warnings", f"{len(result.warnings):,}")

for issue in result.issues:
    if issue.severity == "error":
        st.error(f"**{issue.message}**\n\n{issue.detail}" if issue.detail else issue.message)
    elif issue.severity == "warning":
        with st.expander(f"⚠️ {issue.message}"):
            st.write(issue.detail or "No further detail.")
    else:
        with st.expander(f"ℹ️ {issue.message}"):
            st.write(issue.detail or "No further detail.")


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
tab_review, tab_locked, tab_classified, tab_diag = st.tabs(
    [
        f"🔍 Review queue ({result.unreviewed_count})",
        f"🔒 Locked matches ({len(result.locked)})",
        f"🗂️ Classified ({len(result.not_selling) + len(result.not_yet)})",
        "🧪 Diagnostics",
    ]
)


# ---- Review queue --------------------------------------------------------
with tab_review:
    if not result.new_skus:
        st.success("Nothing to review. Every POS row with stock is accounted for.")
    else:
        st.caption(
            "Each row is a POS masterlist SKU with stock that has no confirmed "
            "SellUp link. Pick a suggestion or classify the row. The export stays "
            "locked until this queue is empty."
        )

        helper = st.columns([2, 2, 3])
        if helper[0].button(
            "Accept all high-confidence", use_container_width=True,
            help="Links every row whose best suggestion scores 100 or above.",
        ):
            applied = 0
            for item in result.new_skus:
                if item.is_actionable or not item.suggestions:
                    continue
                best = item.suggestions[0]
                if best.confidence == "High":
                    record_decision(
                        item.pos.stock_type_id, config.DECISION_LINKED,
                        best.sellup.sku_id, f"auto-accepted (score {best.score})",
                    )
                    applied += 1
            st.toast(f"Linked {applied} high-confidence match(es).")
            st.rerun()

        if helper[1].button(
            "Classify non-SellUp items", use_container_width=True,
            help="Marks laptops, chargers and other hardware SellUp does not "
                 "list as 'Not Selling in SellUp'.",
        ):
            applied = 0
            for item in result.new_skus:
                if item.is_actionable:
                    continue
                kind = device_kind(item.pos.brand, item.pos.model)
                if kind in UNSELLABLE_KINDS:
                    record_decision(
                        item.pos.stock_type_id, config.DECISION_NOT_SELLING,
                        notes=f"auto-classified ({kind})",
                    )
                    applied += 1
            st.toast(f"Classified {applied} item(s).")
            st.rerun()

        only_pending = helper[2].checkbox("Show only unreviewed", value=True)

        visible = [s for s in result.new_skus if not (only_pending and s.is_actionable)]
        st.caption(f"Showing {len(visible):,} of {len(result.new_skus):,} rows.")

        page_size = 40
        pages = max(1, (len(visible) + page_size - 1) // page_size)
        page = st.number_input(
            "Page", min_value=1, max_value=pages, value=1, step=1
        ) if pages > 1 else 1
        window = visible[(page - 1) * page_size: page * page_size]

        rows = []
        option_map: dict[str, dict[str, str]] = {}

        for item in window:
            pos_row = item.pos
            options = {"— leave unreviewed —": ""}
            for suggestion in item.suggestions:
                label = (
                    f"[{suggestion.confidence} {suggestion.score}] "
                    f"{suggestion.sellup.sku_id} · {suggestion.sellup.display}"
                )
                options[label] = suggestion.sellup.sku_id
            option_map[pos_row.stock_type_id] = options

            current = "— leave unreviewed —"
            if item.decision == config.DECISION_LINKED and item.linked_sku_id:
                for label, sku in options.items():
                    if sku == item.linked_sku_id:
                        current = label
                        break
                else:
                    current = f"(manual) {item.linked_sku_id}"
                    options[current] = item.linked_sku_id

            best = item.suggestions[0] if item.suggestions else None
            rows.append(
                {
                    "POS ID": pos_row.stock_type_id,
                    "ML Category": pos_row.category,
                    "Brand": pos_row.brand,
                    "ML Model(s)|Color": f"{pos_row.model} | {pos_row.colour}",
                    "Qty": pos_row.available_qty,
                    "Condition": pos_row.slot,
                    "SellUp Sheet": best.sellup.sheet if best else "",
                    "Storage": best.sellup.storage_label if best else "",
                    "Connectivity": best.sellup.connectivity_label if best else "",
                    "SellUp Colour": best.sellup.colour if best else "",
                    "Link to SellUp SKU": current,
                    "Reviewer Decision": item.decision or config.DECISION_UNREVIEWED,
                    "Notes": item.notes,
                }
            )

        frame = pd.DataFrame(rows)

        all_options = sorted({label for m in option_map.values() for label in m})
        edited = st.data_editor(
            frame,
            use_container_width=True,
            hide_index=True,
            height=520,
            column_config={
                "POS ID": st.column_config.TextColumn(disabled=True, width="small"),
                "ML Category": st.column_config.TextColumn(disabled=True, width="small"),
                "Brand": st.column_config.TextColumn(disabled=True, width="small"),
                "ML Model(s)|Color": st.column_config.TextColumn(
                    disabled=True, width="large"
                ),
                "Qty": st.column_config.NumberColumn(disabled=True, width="small"),
                "Condition": st.column_config.TextColumn(disabled=True, width="medium"),
                "SellUp Sheet": st.column_config.TextColumn(disabled=True, width="small"),
                "Storage": st.column_config.TextColumn(disabled=True, width="small"),
                "Connectivity": st.column_config.TextColumn(disabled=True, width="small"),
                "SellUp Colour": st.column_config.TextColumn(disabled=True, width="small"),
                "Link to SellUp SKU": st.column_config.SelectboxColumn(
                    options=all_options, width="large",
                    help="Suggestions are ranked by score. Picking one sets the "
                         "decision to Linked automatically.",
                ),
                "Reviewer Decision": st.column_config.SelectboxColumn(
                    options=list(config.DECISION_OPTIONS), width="medium"
                ),
                "Notes": st.column_config.TextColumn(width="medium"),
            },
            key=f"review_editor_p{page}",
        )

        if st.button("Apply decisions", type="primary", use_container_width=True):
            applied = 0
            for record in edited.to_dict("records"):
                pos_id = str(record["POS ID"])
                label = record.get("Link to SellUp SKU") or ""
                sku_id = option_map.get(pos_id, {}).get(label, "")
                decision = record.get("Reviewer Decision") or ""
                notes = record.get("Notes") or ""

                # Choosing a suggestion implies the row is linked.
                if sku_id and decision != config.DECISION_LINKED:
                    decision = config.DECISION_LINKED

                if decision == config.DECISION_LINKED and not sku_id:
                    st.warning(
                        f"POS {pos_id} is marked Linked but has no SellUp SKU "
                        "selected — skipped."
                    )
                    continue

                record_decision(pos_id, decision, sku_id, notes)
                if decision:
                    applied += 1

            st.toast(f"Applied {applied} decision(s).")
            st.rerun()


# ---- Locked matches ------------------------------------------------------
with tab_locked:
    if not result.locked:
        st.warning("No locked matches. Upload SellUp Stock Data to carry links over.")
    else:
        locked_frame = pd.DataFrame(
            [
                {
                    "SellUp Sheet": m.sellup.sheet,
                    "SellUp SKU ID": m.sellup.sku_id,
                    "SellUp Model": m.sellup.model,
                    "Storage": m.sellup.storage_label,
                    "Connectivity": m.sellup.connectivity_label,
                    "SellUp Colour": m.sellup.colour,
                    "Condition": m.slot,
                    "LOCKED Masterlist ID(s)": m.masterlist_ids,
                    "ML Category": m.masterlist_categories,
                    "ML Model(s)|Color": m.masterlist_labels,
                    "ML Available Qty": m.available_quantities,
                    "Target Stock": m.target_stock,
                    "# SKUs": len(m.pos_rows),
                }
                for m in result.locked
            ]
        )
        left, right = st.columns([3, 1])
        query = left.text_input("Filter", placeholder="SKU, model or colour…")
        slot_filter = right.selectbox("Condition", ["All", *config.ALL_SLOTS])

        view = locked_frame
        if query:
            mask = view.apply(
                lambda r: query.lower() in " ".join(map(str, r.values)).lower(), axis=1
            )
            view = view[mask]
        if slot_filter != "All":
            view = view[view["Condition"] == slot_filter]

        st.caption(f"{len(view):,} of {len(locked_frame):,} locked matches.")
        st.dataframe(view, use_container_width=True, hide_index=True, height=520)


# ---- Classified ----------------------------------------------------------
with tab_classified:
    left, right = st.columns(2)
    with left:
        st.markdown(f"#### Not Selling in SellUp ({len(result.not_selling):,})")
        if result.not_selling:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Masterlist Stock Type ID": r.stock_type_id,
                            "Category": r.category,
                            "Brand": r.brand,
                            "Model": r.model,
                            "Color": r.colour,
                            "Available Qty": r.available_qty,
                        }
                        for r in result.not_selling
                    ]
                ),
                use_container_width=True, hide_index=True, height=380,
            )
        else:
            st.caption("Nothing classified yet.")

    with right:
        st.markdown(f"#### Not on SellUp Yet ({len(result.not_yet):,})")
        if result.not_yet:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Masterlist Stock Type ID": r.stock_type_id,
                            "Category": r.category,
                            "Brand": r.brand,
                            "Model": r.model,
                            "Color": r.colour,
                            "Available Qty": r.available_qty,
                        }
                        for r in result.not_yet
                    ]
                ),
                use_container_width=True, hide_index=True, height=380,
            )
        else:
            st.caption("Nothing classified yet.")

    st.markdown(f"#### Match Review — no POS source ({len(result.match_review):,})")
    st.caption(
        "SellUp listings previously reviewed as having no POS counterpart. "
        "Their Seller Stock is left untouched."
    )
    if result.match_review:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "SellUp Sheet": r.sheet,
                        "SellUp SKU ID": r.sku_id,
                        "SellUp Model": r.model,
                        "Storage": r.storage_label,
                        "Connectivity": r.connectivity_label,
                        "SellUp Colour": r.colour,
                    }
                    for r in result.match_review
                ]
            ),
            use_container_width=True, hide_index=True, height=300,
        )


# ---- Diagnostics ---------------------------------------------------------
with tab_diag:
    st.markdown("#### POS masterlist")
    st.json(summarise_pos(pos_data))

    if seed_data is not None:
        st.markdown("#### Seed mapping")
        st.json(summarise_seed(seed_data))

    st.markdown("#### Excluded POS rows")
    st.caption(
        "Rows deliberately kept out of the SellUp sync: TELCO stock, export "
        "sets and freebies."
    )
    if pos_data.exclusions:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Stock Type ID": e.stock_type_id,
                        "Brand": e.brand,
                        "Model": e.model,
                        "Color": e.colour,
                        "Available Qty": e.available_qty,
                        "Reason": e.reason,
                    }
                    for e in pos_data.exclusions
                ]
            ),
            use_container_width=True, hide_index=True, height=320,
        )


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
st.divider()
st.subheader("Export")

if not result.export_ready:
    reasons = []
    if result.errors:
        reasons.append(f"{len(result.errors)} validation error(s)")
    if result.unreviewed_count:
        reasons.append(f"{result.unreviewed_count} SKU(s) still to review")
    st.warning(
        "**Download locked.** " + " · ".join(reasons) + ". "
        "Clear the review queue to enable the export."
    )
    st.progress(
        1 - (result.unreviewed_count / max(len(result.new_skus), 1)),
        text=f"{len(result.new_skus) - result.unreviewed_count:,} of "
             f"{len(result.new_skus):,} reviewed",
    )
else:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    with st.spinner("Building files…"):
        produced, write_report = write_quantities(inventory_data, result.assignments)
        violations = diff_against_source(inventory_data.source_bytes, produced)

    if violations:
        st.error(
            "**Template integrity check failed.** The generated file differs "
            "outside columns G, I and K, so it has not been offered for "
            "download. Please report this."
        )
        st.code("\n".join(violations))
    else:
        st.success(
            f"Template integrity verified — {write_report.cells_written:,} cell(s) "
            f"changed, all within columns G, I and K. "
            f"{write_report.cells_unchanged:,} already correct."
        )
        registry_bytes = build_registry_workbook(
            result, datetime.now().strftime("%d-%m-%Y %H:%M")
        )
        left, right = st.columns(2)
        left.download_button(
            "⬇️ SellUp inventory (upload this)",
            data=produced,
            file_name=f"INVENTORIES_UPDATED_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        right.download_button(
            "⬇️ SellUp SKU Registry (keep this)",
            data=registry_bytes,
            file_name=f"SellUp_Match_Review_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.caption(
            "Upload the first file to SellUp. Keep the registry and feed it back "
            "in next time so today's decisions are remembered."
        )
