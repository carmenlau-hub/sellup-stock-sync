"""SellUp Stock Bulk Update — Streamlit front end.

Upload, download, done. The tool links everything it is confident about,
files the obvious non-SellUp hardware, and hands back two files: the SellUp
inventory to upload, and a Match Review workbook for anything it could not
decide. The reviewing happens in Excel, not in this browser window.

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
from sellup_sync.pipeline import run_pipeline
from sellup_sync.pos import PosParseError, load_pos_masterlist, summarise_pos
from sellup_sync.registry import (
    RegistryParseError,
    build_registry_workbook,
    load_registry,
)
from sellup_sync.seed import SeedParseError, load_seed_mapping, summarise_seed

st.set_page_config(
    page_title="SellUp Stock Sync Tool",
    page_icon="📦",
    layout="wide",
)

st.markdown(
    """
    <style>
      .block-container { padding-top: 2.5rem; }
      div[data-testid="stMetricValue"] { font-size: 1.6rem; }
      .stDownloadButton button { width: 100%; height: 3.2rem; font-weight: 600; }
      .mm-banner {
        background: #1F3864; color: #FFFFFF; font-size: 1.9rem; font-weight: 700;
        padding: 18px 26px; border-radius: 6px 6px 0 0; letter-spacing: -0.01em;
      }
      .mm-subbanner {
        background: #12233F; color: #FFD966; font-size: 0.95rem;
        padding: 11px 26px; border-radius: 0 0 6px 6px; margin-bottom: 18px;
      }
      .mm-subbanner b { color: #FFFFFF; }
    </style>
    """,
    unsafe_allow_html=True,
)


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
st.sidebar.header("1 · Upload files")

pos_file = st.sidebar.file_uploader(
    "POS Masterlist (stock report)",
    type=["xlsx", "xlsm", "csv"],
    help="stock_report_DD-MM-YYYY.xlsx exported from POS.",
)
inventory_file = st.sidebar.file_uploader(
    "SellUp Bulk Inventory Template",
    type=["xlsx"],
    help="INVENTORIES_*.xlsx downloaded from the SellUp dealer portal.",
)
registry_file = st.sidebar.file_uploader(
    "SellUp SKU Registry",
    type=["xlsx"],
    help="SellUp_Match_Review_*.xlsx — the file this tool gives you at the end "
         "of every run. Leave empty on your first run.",
)

with st.sidebar.expander("Optional: seed mapping file"):
    st.caption(
        "Only needed on your first run, before you have a SKU Registry. "
        "This is your `SellUp Stock Data` sheet with the columns "
        "`POS Stock Type ID | SellUp Variation ID | SellUp Variation Name`."
    )
    seed_file = st.file_uploader(
        "SellUp Stock Data", type=["xlsx"], label_visibility="collapsed"
    )

st.sidebar.header("2 · Settings")

buffer = st.sidebar.slider(
    "Anti-oversell buffer",
    0,
    config.MAX_OVERSELL_BUFFER,
    config.DEFAULT_OVERSELL_BUFFER,
    help="Quantities at or below this number are written as 0. Thinly-spread "
         "single units are the most common cause of overselling. 0 disables it.",
)

with st.sidebar.expander("Automatic matching"):
    auto_link = st.checkbox(
        "Link confident matches automatically", value=True,
        help="Applies a suggestion when the manufacturer, storage, colour and "
             "model name all agree. Everything auto-linked is marked in the "
             "'How linked' column of the Locked Matches tab.",
    )
    auto_link_min_score = st.slider(
        "Confidence needed", 75, 130, config.AUTO_LINK_MIN_SCORE,
        help="Higher means fewer automatic links and more rows to review by "
             "hand. 100 requires storage, colour and model name to all agree.",
        disabled=not auto_link,
    )
    auto_classify = st.checkbox(
        "File laptops and accessories automatically", value=True,
        help="SellUp has no worksheet for MacBooks, chargers or styluses, so "
             "these go straight to 'Not Selling in SellUp'.",
    )


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
st.markdown(
    """
    <div class="mm-banner">📦 SellUp Stock Sync Tool</div>
    <div class="mm-subbanner">
      <b>Mister Mobile Singapore</b> &nbsp;·&nbsp;
      POS Masterlist → SellUp dealer bulk stock update
    </div>
    """,
    unsafe_allow_html=True,
)

st.caption(
    "Writes POS Available Quantity into columns **G** (New · Not Activated), "
    "**I** (New · Activated) and **K** (Used · Excellent). Every other cell in "
    "the template is left exactly as uploaded."
)

if not pos_file or not inventory_file:
    st.info(
        "⬅️ Upload the **POS Masterlist** and the **SellUp Bulk Inventory "
        "Template** to begin. Your files never leave this session."
    )
    st.markdown(
        """
        #### How it works

        1. Upload your files on the left.
        2. The tool links everything it is confident about and files the
           obvious non-SellUp hardware on its own.
        3. You get **two downloads straight away** — no clicking through rows
           in this window.

        **SellUp inventory** goes to SellUp. **SellUp SKU Registry** is your
        Match Review workbook: open the *New Masterlist SKUs* tab, fill in the
        green columns for anything the tool could not decide, and upload it
        back next time. Every decision is remembered from then on.

        #### First run?

        You will not have a SKU Registry yet — that is the file this tool
        *gives you*. Open **Optional: seed mapping file** and upload your
        `SellUp Stock Data` sheet instead, so your existing matches carry
        across.
        """
    )
    st.stop()


# --------------------------------------------------------------------------
# Pre-flight validation
# --------------------------------------------------------------------------
errors: list[str] = []
pos_data = inventory_data = seed_data = registry_data = None

with st.status("Reading uploads…", expanded=False) as status:
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
            st.write(f"SKU Registry: {len(registry_data.links):,} locked SKUs")
        except RegistryParseError as exc:
            errors.append(f"**SellUp SKU Registry**\n\n{exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"**SellUp SKU Registry** could not be read: {exc}")

    status.update(
        label="Could not read the uploads" if errors else "Uploads read",
        state="error" if errors else "complete",
    )

if errors:
    st.error("#### Cannot continue — fix the file(s) below and re-upload.")
    for message in errors:
        st.error(message)
    st.stop()

if seed_data is None and registry_data is None:
    st.warning(
        "No **SellUp SKU Registry** or seed mapping uploaded, so nothing is "
        "linked yet and almost every POS row will land in the review workbook. "
        "Add one of them in the sidebar."
    )


# --------------------------------------------------------------------------
# Merge link sources and run
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


@st.cache_resource(show_spinner=False)
def _suggestion_index(_inventory, key: str):
    return SuggestionIndex(_inventory)


index = _suggestion_index(inventory_data, inventory_file.name)

with st.spinner("Matching stock…"):
    result = run_pipeline(
        pos=pos_data,
        inventory=inventory_data,
        seed=_Links(merged_links, merged_not_in_pos),
        decisions=dict(getattr(registry_data, "decisions", {}) or {}),
        buffer=buffer,
        suggestion_index=index,
        auto_link=auto_link,
        auto_link_min_score=auto_link_min_score,
        auto_classify=auto_classify,
    )

metrics = result.metrics()


# --------------------------------------------------------------------------
# Downloads first -- this is what the page is for
# --------------------------------------------------------------------------
st.subheader("Your files")

if result.errors:
    for issue in result.errors:
        st.error(f"**{issue.message}**\n\n{issue.detail}" if issue.detail else issue.message)
    st.stop()

stamp = datetime.now().strftime("%Y%m%d_%H%M")
with st.spinner("Building files…"):
    produced, write_report = write_quantities(inventory_data, result.assignments)
    violations = diff_against_source(inventory_data.source_bytes, produced)

if violations:
    st.error(
        "**Template integrity check failed.** The generated file differs "
        "outside columns G, I and K, so it has not been offered for download."
    )
    st.code("\n".join(violations))
    st.stop()

registry_bytes = build_registry_workbook(
    result, datetime.now().strftime("%d-%m-%Y %H:%M")
)

left, right = st.columns(2)
left.download_button(
    f"⬇️  1. SellUp inventory  ·  {write_report.cells_written:,} quantities",
    data=produced,
    file_name=f"INVENTORIES_UPDATED_{stamp}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    type="primary",
)
right.download_button(
    f"⬇️  2. SellUp SKU Registry  ·  {result.unreviewed_count:,} to review",
    data=registry_bytes,
    file_name=f"SellUp_Match_Review_{stamp}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

st.success(
    f"Template integrity verified — {write_report.cells_written:,} cell(s) "
    f"changed, all within columns G, I and K."
)

if result.unreviewed_count:
    st.info(
        f"**{result.unreviewed_count:,} row(s) still need your judgement.** They are "
        "in file 2, on the **New Masterlist SKUs** tab. Fill in the green "
        "**Link to SellUp SKU ID** or **Reviewer Decision** column, save, and "
        "upload it back as the SKU Registry next time. The tool's best guess "
        "is already in the *Suggested SellUp SKU ID* column — copy it across "
        "if it looks right."
    )
else:
    st.info("Nothing left to review. Every POS row with stock is accounted for.")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
st.divider()
st.subheader("Run summary")

row1 = st.columns(5)
row1[0].metric("Locked matches", f"{metrics['locked_updated']:,}")
row1[1].metric("Linked automatically", f"{metrics['auto_linked']:,}")
row1[2].metric("To review in file 2", f"{metrics['requiring_review']:,}")
row1[3].metric("Not selling in SellUp", f"{metrics['not_selling']:,}")
row1[4].metric("Not on SellUp yet", f"{metrics['not_yet']:,}")

row2 = st.columns(5)
row2[0].metric("Quantity cells written", f"{write_report.cells_written:,}")
row2[1].metric("Units synced", f"{metrics['units_synced']:,}")
row2[2].metric("Listings delisted (set 0)", f"{metrics['delisted']:,}")
row2[3].metric("Already correct", f"{write_report.cells_unchanged:,}")
row2[4].metric("Warnings", f"{len(result.warnings):,}")

for issue in result.issues:
    if issue.severity == "warning":
        with st.expander(f"⚠️ {issue.message}"):
            st.write(issue.detail or "No further detail.")
    elif issue.severity == "info":
        with st.expander(f"ℹ️ {issue.message}"):
            st.write(issue.detail or "No further detail.")


# --------------------------------------------------------------------------
# Read-only tabs
# --------------------------------------------------------------------------
tab_review, tab_locked, tab_classified, tab_diag = st.tabs(
    [
        f"📋 To review ({result.unreviewed_count})",
        f"🔒 Locked matches ({len(result.locked)})",
        f"🗂️ Classified ({len(result.not_selling) + len(result.not_yet)})",
        "🧪 Diagnostics",
    ]
)

with tab_review:
    if not result.new_skus:
        st.success("Nothing to review.")
    else:
        st.caption(
            "A preview of the **New Masterlist SKUs** tab in file 2. Edit it in "
            "Excel, not here — this view is read-only."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "POS ID": s.pos.stock_type_id,
                        "Category": s.pos.category,
                        "Brand": s.pos.brand,
                        "Model": s.pos.model,
                        "Color": s.pos.colour,
                        "Qty": s.pos.available_qty,
                        "Condition": s.pos.slot,
                        "Suggested SellUp SKU ID": (
                            s.suggestions[0].sellup.sku_id if s.suggestions else ""
                        ),
                        "Suggested Listing": (
                            s.suggestions[0].sellup.display if s.suggestions else ""
                        ),
                        "Confidence": (
                            s.suggestions[0].confidence if s.suggestions else "no suggestion"
                        ),
                        "Score": s.suggestions[0].score if s.suggestions else None,
                    }
                    for s in result.new_skus
                ]
            ),
            use_container_width=True,
            hide_index=True,
            height=520,
        )

with tab_locked:
    if not result.locked:
        st.warning(
            "No locked matches. Upload a SellUp SKU Registry or the seed "
            "mapping file to carry your existing links over."
        )
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
                    "How linked": m.origin,
                }
                for m in result.locked
            ]
        )
        left, mid, right = st.columns([3, 1, 1])
        query = left.text_input("Filter", placeholder="SKU, model or colour…")
        slot_filter = mid.selectbox("Condition", ["All", *config.ALL_SLOTS])
        origin_filter = right.selectbox(
            "How linked", ["All", config.LINKED_BY_AUTO, config.LINKED_BY_SEED,
                           config.LINKED_BY_REVIEWER]
        )

        view = locked_frame
        if query:
            mask = view.apply(
                lambda r: query.lower() in " ".join(map(str, r.values)).lower(), axis=1
            )
            view = view[mask]
        if slot_filter != "All":
            view = view[view["Condition"] == slot_filter]
        if origin_filter != "All":
            view = view[view["How linked"] == origin_filter]

        st.caption(
            f"{len(view):,} of {len(locked_frame):,} locked matches. Filter "
            f"**How linked** to `{config.LINKED_BY_AUTO}` to spot-check the "
            "automatic ones."
        )
        st.dataframe(view, use_container_width=True, hide_index=True, height=520)

with tab_classified:
    left, right = st.columns(2)
    for column, title, rows in (
        (left, "Not Selling in SellUp", result.not_selling),
        (right, "Not on SellUp Yet", result.not_yet),
    ):
        with column:
            st.markdown(f"#### {title} ({len(rows):,})")
            if rows:
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
                            for r in rows
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                    height=380,
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
            use_container_width=True,
            hide_index=True,
            height=300,
        )

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
            use_container_width=True,
            hide_index=True,
            height=320,
        )
