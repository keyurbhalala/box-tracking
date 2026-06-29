from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_NZ = ZoneInfo("Pacific/Auckland")

def _today_nz() -> date:
    """Return today's date in NZ time (auto-handles NZST/NZDT)."""
    return datetime.now(_NZ).date()

import pandas as pd
import plotly.express as px
import streamlit as st

from database import init_db
from exports import to_csv_bytes, to_excel_bytes
from services import (
    METHODS,
    add_group,
    add_store,
    audit_history,
    auto_suggest_mappings,
    create_shipment,
    dashboard_metrics,
    delete_address_book_entry,
    delete_group,
    delete_shipment,
    delete_store,
    delete_store_mapping,
    get_address_book,
    get_courier_bookings,
    get_delivery_details,
    get_delivery_runs,
    get_groups,
    get_shipment,
    get_stores,
    get_store_mappings,
    get_store_mapping,
    get_store_with_address,
    get_unmapped_stores,
    get_warehouse,
    history,
    import_address_book,
    pallet_lookup,
    query_df,
    retry_courier_booking,
    save_courier_booking,
    save_signature,
    set_store_mapping,
    trend_data,
    update_address_book_entry,
    update_group,
    update_shipment,
    update_store,
)


st.set_page_config(
    page_title="Shipment & Pallet Tracking",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)
@st.cache_resource
def _init_db_once():
    init_db()

_init_db_once()

st.markdown(
    """
    <style>
    :root { --brand: #0f766e; }
    [data-testid="stSidebar"] { background: #102a43 !important; }
    [data-testid="stSidebar"] * { color: #f8fafc !important; }
    [data-testid="stMetric"] {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128,128,128,0.2);
        border-radius: 14px;
        padding: 16px;
        box-shadow: 0 4px 18px rgba(0,0,0,.06);
    }
    .hero {
        background: linear-gradient(120deg, #0f766e, #155e75);
        color: white !important; padding: 22px 26px; border-radius: 16px; margin-bottom: 18px;
    }
    .hero h1 { margin: 0; font-size: 1.8rem; color: white !important; }
    .hero p  { margin: 6px 0 0; opacity: .88; color: white !important; }
    .section-card {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128,128,128,0.2);
        border-radius: 14px;
        padding: 18px; margin-bottom: 14px;
    }
    div.stButton > button[kind="primary"] { background: #0f766e; border-color: #0f766e; }
    @media (max-width: 768px) {
        .hero { padding: 16px; }
        .hero h1 { font-size: 1.35rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def hero(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="hero"><h1>{title}</h1><p>{subtitle}</p></div>',
        unsafe_allow_html=True,
    )


def downloads(df: pd.DataFrame, stem: str, summary: dict | None = None) -> None:
    col1, col2, _ = st.columns([1, 1, 3])
    col1.download_button(
        "Download Excel",
        to_excel_bytes(df, "Report", summary),
        file_name=f"{stem}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    col2.download_button(
        "Download CSV",
        to_csv_bytes(df),
        file_name=f"{stem}.csv",
        mime="text/csv",
        use_container_width=True,
    )


def shipment_editor_rows(
    stores: pd.DataFrame,
    existing: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the data editor DataFrame (Store ID hidden, Store, Group, Boxes)."""
    rows = stores[["id", "store_name", "group_name"]].copy()
    rows.columns = ["Store ID", "Store", "Group"]
    rows["Boxes"] = 0
    if existing is not None and not existing.empty:
        box_map = existing.set_index("store_id")["boxes"].to_dict()
        rows["Boxes"] = rows["Store ID"].map(box_map).fillna(0).astype(int)
    return rows


def render_dashboard() -> None:
    hero("Shipment & Pallet Tracking", "Today at a glance")
    metrics = dashboard_metrics()
    cols = st.columns(5)
    labels = [
        ("Total Boxes Today", metrics["boxes_today"]),
        ("Shipments Today", metrics["shipments_today"]),
        ("Pallets This Week", metrics["pallets_week"]),
        ("Couriers This Week", metrics["couriers_week"]),
        ("Deliveries This Week", metrics["deliveries_week"]),
    ]
    for col, (label, value) in zip(cols, labels):
        col.metric(label, f"{value:,}")

    df = trend_data()
    if df.empty:
        st.info("No shipment data yet. Add the first shipment to populate the dashboard.")
        return
    weekly = (
        df.set_index("Date")
        .resample("W-MON", label="left", closed="left")["Boxes"]
        .sum()
        .reset_index()
    )
    monthly = (
        df.assign(Month=df["Date"].dt.to_period("M").dt.to_timestamp())
        .groupby("Month", as_index=False)["Boxes"]
        .sum()
    )
    method = df.groupby("Method", as_index=False)["Boxes"].sum()
    left, right = st.columns(2)
    left.plotly_chart(
        px.line(
            weekly,
            x="Date",
            y="Boxes",
            markers=True,
            title="Boxes Sent by Week",
            color_discrete_sequence=["#0f766e"],
        ),
        use_container_width=True,
    )
    right.plotly_chart(
        px.bar(
            monthly,
            x="Month",
            y="Boxes",
            title="Boxes Sent by Month",
            color_discrete_sequence=["#155e75"],
        ),
        use_container_width=True,
    )
    st.plotly_chart(
        px.pie(
            method,
            names="Method",
            values="Boxes",
            hole=0.55,
            title="Shipment Method Breakdown (Boxes)",
            color_discrete_map={
                "Courier": "#0f766e",
                "Pallet": "#f59e0b",
                "Delivery": "#2563eb",
            },
        ),
        use_container_width=True,
    )


def _build_courier_stores(
    shipment_id: int,
    details: list[dict],
    service_code: str,
) -> list:
    """
    Book a Starshipit consignment for each store and save results.

    Each dict in `details` must carry its own weight/length/width/height so
    different stores can have different package sizes in the same shipment.
    Returns a list of BookingResult objects.
    """
    from starshipit import create_order, Address, Package, BookingResult

    warehouse = get_warehouse()
    if not warehouse or not warehouse.get("address_line1"):
        st.error("Warehouse address not configured. Please set it up in the database.")
        return []

    sender = Address(
        name=warehouse.get("warehouse_name", "Shosha Warehouse"),
        phone=warehouse.get("phone", ""),
        street=warehouse.get("address_line1", ""),
        suburb=warehouse.get("suburb", ""),
        city=warehouse.get("city", ""),
        postcode=warehouse.get("postcode", ""),
        country=warehouse.get("country", "NZ"),
        email=warehouse.get("email", ""),
    )

    results: list = []
    progress = st.progress(0, text="Booking couriers with Starshipit…")

    for i, d in enumerate(details):
        # Uniform fallback dims (used when per_box_dims is absent, and for DB summary)
        w  = float(d.get("weight", 1.0) or 1.0)
        ln = float(d.get("length", 30.0) or 30.0)
        wd = float(d.get("width",  20.0) or 20.0)
        ht = float(d.get("height", 15.0) or 15.0)

        store = get_store_with_address(d["store_id"])

        # Per-box dimensions: supplied as a list of dicts by the new UI.
        # Falls back to the legacy uniform-size fields if not present.
        per_box = d.get("box_dims")          # list[dict] | None

        if not store or not store.get("street"):
            r = BookingResult(
                success=False,
                store_name=d["store_name"],
                boxes=d["boxes"],
                booking_status="Failed",
                error=(
                    "No address book mapping. "
                    "Go to Admin → Address Book to map this store."
                ),
            )
        else:
            recipient = Address(
                name=store.get("contact_name") or store.get("company_name") or d["store_name"],
                company=store.get("company_name") or d["store_name"],
                phone=store.get("phone") or "",
                street=store.get("street") or "",
                suburb=store.get("suburb") or "",
                city=store.get("city") or "",
                postcode=store.get("postcode") or "",
                country=store.get("country_code") or "NZ",
                email=store.get("email") or "",
                building=store.get("building") or "",
            )
            pkg = Package(
                boxes=d["boxes"],
                weight_per_box=w,
                length=ln,
                width=wd,
                height=ht,
                per_box_dims=per_box,
            )
            ref = f"SHP-{shipment_id}-{d['store_id']}"
            r   = create_order(sender, recipient, pkg, ref, service_code)

        # For the DB summary, use first-box dims (or the uniform dims).
        if per_box:
            first = per_box[0]
            w  = float(first.get("weight") or w)
            ln = float(first.get("length") or ln)
            wd = float(first.get("width")  or wd)
            ht = float(first.get("height") or ht)

        save_courier_booking(
            shipment_id, d["store_id"], d["store_name"], d["boxes"],
            w, ln, wd, ht, service_code, r,
        )
        results.append(r)
        progress.progress((i + 1) / len(details), text=f"Booked {d['store_name']}…")

    progress.empty()

    # ── Store label PDFs from create_order (already fetched via /api/orders/shipment) ──
    st.session_state["_store_labels"] = {}
    for r in results:
        if r.success and r.label_pdf:
            st.session_state["_store_labels"][r.consignment_id] = r.label_pdf

    return results


def _print_label_button(pdf_bytes: bytes, key: str) -> None:
    """
    Renders a 'Print Label' button that opens the browser's print dialog.
    Works on Streamlit Cloud — uses JS to embed the PDF and call window.print().
    The user can then select any printer installed on their computer.
    """
    import base64, hashlib
    b64 = base64.b64encode(pdf_bytes).decode()
    # Use a short hash as the iframe/button id so multiple buttons on screen don't clash
    uid = hashlib.md5(key.encode()).hexdigest()[:8]
    html = f"""
    <style>
      #btn_{uid} {{
        background:#1a7f5a; color:#fff; border:none; border-radius:6px;
        padding:6px 14px; font-size:14px; cursor:pointer; width:100%;
        font-family:sans-serif; font-weight:500;
      }}
      #btn_{uid}:hover {{ background:#145f44; }}
    </style>
    <button id="btn_{uid}" onclick="
      var frame = document.getElementById('pdf_{uid}');
      frame.src = 'data:application/pdf;base64,{b64}';
      frame.onload = function() {{
        frame.contentWindow.focus();
        frame.contentWindow.print();
      }};
    ">🖨 Print Label</button>
    <iframe id="pdf_{uid}" style="display:none" width="0" height="0"></iframe>
    """
    st.components.v1.html(html, height=40)


def _render_courier_results(shipment_id: int, results: list) -> None:
    """Display the per-store courier booking results table."""
    from starshipit import tracking_url, SERVICE_OPTIONS

    booked = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    st.divider()
    if booked:
        st.success(
            f"✅ Shipment #{shipment_id} saved. "
            f"{len(booked)} courier booking{'s' if len(booked) != 1 else ''} confirmed with Starshipit."
        )
    if failed:
        st.warning(
            f"⚠️ {len(failed)} store{'s' if len(failed) != 1 else ''} could not be booked. "
            "Use **Retry** in Shipment History to rebook."
        )

    store_labels: dict = st.session_state.get("_store_labels", {})

    for r in results:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 3, 2, 2])
            c1.markdown(f"**{r.store_name}**  \n{r.boxes} box{'es' if r.boxes != 1 else ''}")
            if r.success:
                c2.code(r.tracking_number, language=None)
                c3.markdown(f"{r.carrier}  \n`{r.service_code}`")
                link_col1, link_col2 = c4.columns(2)

                # Prefer freshly-fetched PDF bytes; fall back to label_url from order creation
                pdf_bytes = store_labels.get(r.consignment_id)
                if pdf_bytes:
                    with link_col1:
                        _print_label_button(pdf_bytes, key=f"lbl_{r.consignment_id}")
                elif r.label_url:
                    link_col1.link_button("🖨 Label", r.label_url, use_container_width=True)
                elif r.label_error:
                    # Show the label error inline so it's visible (not just in logs)
                    c4.warning(f"Label failed: {r.label_error[:120]}")

                if r.tracking_number:
                    link_col2.link_button("📦 Track", tracking_url(r.tracking_number), use_container_width=True)
            else:
                c2.error(r.error[:120] if r.error else "Unknown error")
                c3.markdown("—")
                c4.markdown("—")


def render_new_shipment() -> None:
    hero("New Shipment", "Record boxes by store in one quick entry")

    # ── Group mode toggle (outside form — updates UI immediately) ────────────
    group_mode = st.radio(
        "Group selection",
        ["Existing Group", "Custom Group"],
        horizontal=True,
        label_visibility="collapsed",
        help=(
            "Existing Group: pick a pre-defined store group.\n"
            "Custom Group: hand-pick individual stores from any group for a one-off shipment."
        ),
    )

    # ── Store resolution ─────────────────────────────────────────────────────
    if group_mode == "Existing Group":
        groups = get_groups()
        if groups.empty:
            st.warning("Add an active group and stores before recording shipments.")
            return
        group_map = dict(zip(groups["group_name"], groups["id"]))
        selected_group = st.selectbox("Group / Store", list(group_map))
        stores = get_stores(int(group_map[selected_group]))
        _base_key = f"new_editor_{group_map[selected_group]}"

    else:  # Custom Group
        all_stores = get_stores()
        if all_stores.empty:
            st.warning("No active stores found.")
            return
        store_options = all_stores["store_name"].tolist()
        selected_store_names = st.multiselect(
            "Search & select stores",
            options=store_options,
            placeholder="Type to search for stores…",
        )
        if not selected_store_names:
            st.info("Select at least one store to continue.")
            return
        stores = all_stores[all_stores["store_name"].isin(selected_store_names)].copy()
        _base_key = f"new_editor_custom_{'_'.join(sorted(selected_store_names))}"

    # ── Shipment Method (outside form so Courier fields appear immediately) ──
    method = st.selectbox(
        "Shipment Method",
        ["", *METHODS],
        format_func=lambda x: "— Select method —" if x == "" else x,
        key="new_shipment_method",
    )

    from starshipit import SERVICE_OPTIONS, DEFAULT_SERVICE_CODE

    # ══════════════════════════════════════════════════════════════════════════
    # COURIER — fully reactive flow (no form so box count changes immediately
    # show/hide per-box dimension tables without a form submit).
    # ══════════════════════════════════════════════════════════════════════════
    if method == "Courier":
        with st.container(border=True):
            svc_label = st.selectbox(
                "NZ Post Service", list(SERVICE_OPTIONS.keys()), key="pkg_service",
            )
            service_code = SERVICE_OPTIONS[svc_label]

        courier_date = st.date_input("Shipment Date", value=_today_nz(), key="courier_date")
        courier_notes = st.text_area(
            "Notes", placeholder="Optional reference or instructions", key="courier_notes"
        )

        # ── Box count editor ──────────────────────────────────────────────────
        bc_key = f"{_base_key}_courier_boxes"
        box_editor = st.data_editor(
            shipment_editor_rows(stores),
            hide_index=True,
            use_container_width=True,
            disabled=["Store", "Group"],
            column_config={
                "Store ID": None,
                "Boxes": st.column_config.NumberColumn(
                    "Boxes", min_value=0, step=1, format="%d"
                ),
            },
            key=bc_key,
        )
        courier_total = int(
            pd.to_numeric(box_editor["Boxes"], errors="coerce").fillna(0).sum()
        )
        st.caption(f"Shipment total: **{courier_total:,} boxes**")

        # ── Per-store per-box dimension tables ────────────────────────────────
        # Appear automatically as box counts are entered.  Each physical box gets
        # its own row so different sizes can be declared within one store.
        stores_with_boxes = [
            (int(row["Store ID"]), row["Store"], row["Group"], int(row.get("Boxes") or 0))
            for _, row in box_editor.iterrows()
            if int(row.get("Boxes") or 0) > 0
        ]

        dim_editors: dict[int, pd.DataFrame] = {}
        if stores_with_boxes:
            st.markdown("---")
            st.markdown("#### 📦 Box Dimensions")
            st.caption(
                "One row per physical box. Edit weight and dimensions for each box individually. "
                "All boxes for one store go in a single consignment."
            )
            for store_id, store_name, _grp, n_boxes in stores_with_boxes:
                with st.expander(
                    f"📦 {store_name} — {n_boxes} box{'es' if n_boxes > 1 else ''}",
                    expanded=True,
                ):
                    # Key includes store_id + n_boxes so it resets if box count changes.
                    # Session state preserves edits across other reruns.
                    dim_key = f"dims_{bc_key}_{store_id}_{n_boxes}"
                    default_df = pd.DataFrame([
                        {
                            "Box":    f"Box {i + 1}",
                            "Wt (kg)": 1.0,
                            "L (cm)":  30,
                            "W (cm)":  20,
                            "H (cm)":  15,
                        }
                        for i in range(n_boxes)
                    ])
                    dim_editors[store_id] = st.data_editor(
                        default_df,
                        column_config={
                            "Box": st.column_config.TextColumn("Box", disabled=True),
                            "Wt (kg)": st.column_config.NumberColumn(
                                "Wt (kg)", min_value=0.1, max_value=50.0,
                                step=0.1, format="%.1f",
                            ),
                            "L (cm)": st.column_config.NumberColumn(
                                "L (cm)", min_value=1, max_value=300, step=1, format="%d",
                            ),
                            "W (cm)": st.column_config.NumberColumn(
                                "W (cm)", min_value=1, max_value=300, step=1, format="%d",
                            ),
                            "H (cm)": st.column_config.NumberColumn(
                                "H (cm)", min_value=1, max_value=300, step=1, format="%d",
                            ),
                        },
                        key=dim_key,
                        hide_index=True,
                        use_container_width=True,
                    )

        st.markdown("---")
        courier_save = st.button(
            "Save Shipment & Book Courier",
            type="primary",
            use_container_width=True,
            key="save_courier_btn",
            disabled=(courier_total == 0),
        )

        if courier_save:
            details: list[dict] = []
            for _, row in box_editor.iterrows():
                n   = int(row.get("Boxes") or 0)
                sid = int(row["Store ID"])
                d: dict = {
                    "store_id":   sid,
                    "store_name": row["Store"],
                    "group_name": row["Group"],
                    "boxes":      n,
                }
                if n > 0:
                    if sid in dim_editors:
                        d["box_dims"] = [
                            {
                                "weight": float(r.get("Wt (kg)") or 1.0),
                                "length": float(r.get("L (cm)") or 30),
                                "width":  float(r.get("W (cm)") or 20),
                                "height": float(r.get("H (cm)") or 15),
                            }
                            for _, r in dim_editors[sid].iterrows()
                        ]
                    else:
                        # Fallback if dim editor wasn't rendered (shouldn't happen)
                        d["box_dims"] = [
                            {"weight": 1.0, "length": 30, "width": 20, "height": 15}
                        ] * n
                details.append(d)

            try:
                shipment_id, _ = create_shipment(
                    courier_date, method, courier_notes, details
                )
            except Exception as exc:
                st.error(str(exc))
                return

            stores_to_book = [d for d in details if d["boxes"] > 0]
            results = _build_courier_stores(
                shipment_id, stores_to_book, service_code
            )
            if results:
                st.session_state["_courier_results"] = {
                    "shipment_id": shipment_id,
                    "results": results,
                }

    # ══════════════════════════════════════════════════════════════════════════
    # PALLET / DELIVERY — standard form (no per-box dims needed)
    # ══════════════════════════════════════════════════════════════════════════
    elif method in ("Pallet", "Delivery"):
        editor_key = f"{_base_key}_{method}"
        with st.form("new_shipment", clear_on_submit=False):
            shipment_date = st.date_input("Shipment Date", value=_today_nz())
            notes = st.text_area(
                "Notes", placeholder="Optional reference or instructions"
            )
            if stores.empty:
                st.warning("This group has no active stores.")
            editor = st.data_editor(
                shipment_editor_rows(stores),
                hide_index=True,
                use_container_width=True,
                disabled=["Store", "Group"],
                column_config={
                    "Store ID": None,
                    "Boxes": st.column_config.NumberColumn(
                        "Boxes", min_value=0, step=1, format="%d"
                    ),
                },
                key=editor_key,
            )
            total = int(
                pd.to_numeric(editor["Boxes"], errors="coerce").fillna(0).sum()
            )
            st.caption(f"Shipment total: **{total:,} boxes**")
            submitted = st.form_submit_button(
                "Save Shipment", type="primary", use_container_width=True
            )

        if submitted:
            details = [
                {
                    "store_id":   int(row["Store ID"]),
                    "store_name": row["Store"],
                    "group_name": row["Group"],
                    "boxes":      int(row["Boxes"] or 0),
                }
                for _, row in editor.iterrows()
            ]
            try:
                shipment_id, pallet_id = create_shipment(
                    shipment_date, method, notes, details
                )
            except Exception as exc:
                st.error(str(exc))
                return
            message = f"Shipment #{shipment_id} saved with {total:,} boxes."
            if pallet_id:
                message += f"  Pallet ID: **{pallet_id}**"
            st.success(message)
            st.session_state.pop("_courier_results", None)

    # ── Courier booking results panel ─────────────────────────────────────────
    if "_courier_results" in st.session_state:
        state = st.session_state["_courier_results"]
        _render_courier_results(state["shipment_id"], state["results"])


def render_history() -> None:
    hero("Shipment History", "Filter, sort, export, edit, or delete records")
    stores = get_stores(active_only=False)
    groups = get_groups(active_only=False)
    today = _today_nz()
    preset = st.segmented_control(
        "Quick date range",
        ["This Week", "Last 7 Days", "This Month", "Last 30 Days", "Custom"],
        default="Last 30 Days",
    )
    if preset == "This Week":
        start, end = today - timedelta(days=today.weekday()), today
    elif preset == "Last 7 Days":
        start, end = today - timedelta(days=6), today
    elif preset == "This Month":
        start, end = today.replace(day=1), today
    elif preset == "Last 30 Days":
        start, end = today - timedelta(days=29), today
    else:
        dates = st.date_input(
            "Date range", value=(today - timedelta(days=30), today)
        )
        start, end = (dates if len(dates) == 2 else (dates[0], dates[0]))

    f1, f2, f3 = st.columns(3)
    selected_stores = f1.multiselect("Stores", stores["store_name"].tolist())
    selected_groups = f2.multiselect("Groups", groups["group_name"].tolist())
    selected_methods = f3.multiselect("Methods", METHODS)
    store_ids = (
        stores.loc[stores["store_name"].isin(selected_stores), "id"].astype(int).tolist()
    )
    df = history(start, end, store_ids, selected_groups, selected_methods)
    c1, c2, c3 = st.columns(3)
    c1.metric("Boxes", f"{df['Boxes'].sum():,}" if not df.empty else "0")
    c2.metric(
        "Shipments",
        f"{df['shipment_id'].nunique():,}" if not df.empty else "0",
    )
    c3.metric("Stores", f"{df['Store'].nunique():,}" if not df.empty else "0")
    display = df.drop(columns=["shipment_id"]) if not df.empty else df
    st.dataframe(display, hide_index=True, use_container_width=True)
    downloads(display, f"shipment_history_{start}_{end}")

    st.divider()
    st.subheader("Edit or delete a shipment")
    shipment_options = sorted(df["shipment_id"].unique().tolist()) if not df.empty else []
    if not shipment_options:
        st.caption("No shipments in the current filter.")
        return
    selected_id = st.selectbox(
        "Shipment",
        shipment_options,
        format_func=lambda value: f"Shipment #{value}",
    )
    header, current_details = get_shipment(int(selected_id))
    all_stores = get_stores(active_only=False)
    groups = get_groups(active_only=False)
    group_names = ["All Groups"] + sorted(groups["group_name"].tolist())

    # Default to the group the shipment was originally booked under
    default_group = "All Groups"
    if not current_details.empty:
        booked_groups = current_details["group_name"].dropna().unique().tolist()
        if len(booked_groups) == 1 and booked_groups[0] in group_names:
            default_group = booked_groups[0]

    edit_group_filter = st.selectbox(
        "Filter by Group / Store",
        group_names,
        index=group_names.index(default_group),
        key=f"edit_group_filter_{selected_id}",
    )

    if edit_group_filter == "All Groups":
        display_stores = all_stores
    else:
        display_stores = all_stores[all_stores["group_name"] == edit_group_filter]

    with st.form(f"edit_{selected_id}"):
        e1, e2 = st.columns(2)
        edit_date = e1.date_input(
            "Shipment Date", value=pd.Timestamp(header["shipment_date"]).date()
        )
        edit_method = e2.selectbox(
            "Shipment Method", METHODS, index=METHODS.index(header["shipment_method"])
        )
        edit_notes = st.text_area("Notes", value=header["notes"] or "")
        edit_rows = shipment_editor_rows(display_stores, current_details)
        edited = st.data_editor(
            edit_rows,
            hide_index=True,
            use_container_width=True,
            disabled=["Store ID", "Store", "Group"],
            column_config={
                "Store ID": None,
                "Boxes": st.column_config.NumberColumn(
                    "Boxes", min_value=0, step=1, format="%d"
                ),
            },
        )
        save_edit = st.form_submit_button("Save Changes", type="primary")
    if save_edit:
        details = [
            {
                "store_id": int(row["Store ID"]),
                "store_name": row["Store"],
                "group_name": row["Group"],
                "boxes": int(row["Boxes"] or 0),
            }
            for _, row in edited.iterrows()
        ]
        try:
            pallet_id = update_shipment(
                int(selected_id), edit_date, edit_method, edit_notes, details
            )
            suffix = f" Pallet ID: {pallet_id}" if pallet_id else ""
            st.success(f"Shipment #{selected_id} updated.{suffix}")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    # ── Courier bookings for this shipment ────────────────────────────────────
    if header.get("shipment_method") == "Courier":
        cb = get_courier_bookings(int(selected_id))
        if not cb.empty:
            from starshipit import tracking_url
            st.markdown("**Courier Bookings**")
            for _, row in cb.iterrows():
                with st.container(border=True):
                    col1, col2, col3, col4 = st.columns([3, 3, 2, 2])
                    status_icon = "✅" if row["booking_status"] == "Booked" else "❌"
                    col1.markdown(
                        f"{status_icon} **{row['store_name']}**  \n"
                        f"{row['boxes']} box{'es' if row['boxes'] != 1 else ''}"
                    )
                    if row["booking_status"] == "Booked" and row.get("tracking_number"):
                        col2.code(str(row["tracking_number"]), language=None)
                        col3.markdown(
                            f"{row.get('carrier', '')}  \n`{row.get('service_code', '')}`"
                        )
                        lc1, lc2 = col4.columns(2)
                        if row.get("label_url"):
                            lc1.link_button("🖨", row["label_url"], use_container_width=True, help="Print label")
                        lc2.link_button(
                            "📦", tracking_url(str(row["tracking_number"])),
                            use_container_width=True, help="Track parcel",
                        )
                    else:
                        col2.caption(str(row.get("api_error") or "No tracking number")[:100])
                        if col4.button(
                            "↺ Retry",
                            key=f"retry_{row['id']}",
                            type="secondary",
                        ):
                            with st.spinner(f"Retrying {row['store_name']}…"):
                                ok, msg = retry_courier_booking(int(row["id"]))
                            if ok:
                                st.success(f"Rebooked {row['store_name']} — tracking: {msg}")
                                st.rerun()
                            else:
                                st.error(f"Retry failed: {msg}")

    confirm = st.checkbox("I understand this will delete the whole shipment.")
    if st.button(
        "Delete Shipment",
        disabled=not confirm,
        type="secondary",
        use_container_width=False,
    ):
        delete_shipment(int(selected_id))
        st.success(f"Shipment #{selected_id} deleted. The audit record was retained.")
        st.rerun()


def render_store_lookup() -> None:
    hero("Store Lookup", "See shipment history and rolling totals for one store")
    stores = get_stores(active_only=False)
    if stores.empty:
        st.info("No stores available.")
        return
    store_name = st.selectbox("Search store", stores["store_name"].tolist())
    store_id = int(stores.loc[stores["store_name"] == store_name, "id"].iloc[0])
    df = history(store_ids=[store_id])
    if df.empty:
        st.info("No shipments found for this store.")
        return
    df["Date"] = pd.to_datetime(df["Date"])
    today = pd.Timestamp(_today_nz())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Boxes Last 7 Days",
        int(df.loc[df["Date"] >= today - pd.Timedelta(days=6), "Boxes"].sum()),
    )
    c2.metric(
        "Boxes Last 30 Days",
        int(df.loc[df["Date"] >= today - pd.Timedelta(days=29), "Boxes"].sum()),
    )
    c3.metric(
        "Boxes Last 12 Months",
        int(df.loc[df["Date"] >= today - pd.DateOffset(months=12), "Boxes"].sum()),
    )
    c4.metric("Number of Shipments", int(df["shipment_id"].nunique()))
    display = df[["Date", "Boxes", "Method", "Pallet ID", "Notes"]].copy()
    display["Date"] = display["Date"].dt.date
    st.dataframe(display, hide_index=True, use_container_width=True)
    downloads(display, f"store_report_{store_name.replace(' ', '_').lower()}")


def render_group_reporting() -> None:
    hero("Group Reporting", "Volume, store rankings, and shipment trends")
    groups = get_groups(active_only=False)
    if groups.empty:
        st.info("No groups available.")
        return
    selected = st.selectbox("Group", groups["group_name"].tolist())
    df = history(groups=[selected])
    if df.empty:
        st.info("No shipments found for this group.")
        return
    df["Date"] = pd.to_datetime(df["Date"])
    c1, c2 = st.columns(2)
    c1.metric("Total Boxes Sent", f"{df['Boxes'].sum():,}")
    c2.metric("Number of Shipments", f"{df['shipment_id'].nunique():,}")
    top = (
        df.groupby("Store", as_index=False)["Boxes"]
        .sum()
        .sort_values("Boxes", ascending=False)
    )
    weekly = (
        df.set_index("Date")
        .resample("W-MON", label="left", closed="left")["Boxes"]
        .sum()
        .reset_index()
    )
    monthly = (
        df.assign(Month=df["Date"].dt.to_period("M").dt.to_timestamp())
        .groupby("Month", as_index=False)["Boxes"]
        .sum()
    )
    left, right = st.columns(2)
    left.plotly_chart(
        px.bar(
            top,
            x="Boxes",
            y="Store",
            orientation="h",
            title="Top Stores by Volume",
            color_discrete_sequence=["#0f766e"],
        ),
        use_container_width=True,
    )
    right.plotly_chart(
        px.line(
            weekly,
            x="Date",
            y="Boxes",
            markers=True,
            title="Weekly Trend",
            color_discrete_sequence=["#155e75"],
        ),
        use_container_width=True,
    )
    st.plotly_chart(
        px.bar(
            monthly,
            x="Month",
            y="Boxes",
            title="Monthly Trend",
            color_discrete_sequence=["#f59e0b"],
        ),
        use_container_width=True,
    )
    display = df.drop(columns=["shipment_id"])
    downloads(
        display,
        f"group_report_{selected.replace(' ', '_').lower()}",
        {
            "Group": selected,
            "Total Boxes": int(df["Boxes"].sum()),
            "Shipments": int(df["shipment_id"].nunique()),
        },
    )


def render_pallet_search() -> None:
    hero("Pallet Search", "Find every store and box linked to a pallet")
    pallet_id = st.text_input("Pallet ID", placeholder="PAL-20260624-001")
    if not pallet_id:
        st.caption("Enter a pallet ID to search.")
        return
    details, header = pallet_lookup(pallet_id)
    if not header:
        st.warning("Pallet not found.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Pallet ID", header["pallet_id"])
    c2.metric("Shipment Date", header["shipment_date"])
    c3.metric("Total Boxes", f"{details['Boxes'].sum():,}")
    if header["notes"]:
        st.info(header["notes"])
    st.dataframe(details, hide_index=True, use_container_width=True)
    downloads(
        details,
        f"pallet_report_{header['pallet_id']}",
        {
            "Pallet ID": header["pallet_id"],
            "Shipment Date": header["shipment_date"],
            "Total Boxes": int(details["Boxes"].sum()),
        },
    )


def render_store_management() -> None:
    hero("Groups & Stores", "Maintain the destinations used during shipment entry")
    groups = get_groups(active_only=False)
    tab1, tab2 = st.tabs(["Groups", "Stores"])
    with tab1:
        st.dataframe(groups, hide_index=True, use_container_width=True)
        with st.expander("Add group", expanded=groups.empty):
            with st.form("add_group"):
                new_group = st.text_input("Group name")
                if st.form_submit_button("Add Group", type="primary"):
                    try:
                        add_group(new_group)
                        st.success("Group added.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        if not groups.empty:
            with st.expander("Edit or remove group"):
                group_label = st.selectbox(
                    "Select group", groups["group_name"].tolist(), key="edit_group"
                )
                row = groups[groups["group_name"] == group_label].iloc[0]
                with st.form("update_group"):
                    name = st.text_input("Group name", value=row["group_name"])
                    active = st.checkbox("Active", value=bool(row["active"]))
                    if st.form_submit_button("Save Group"):
                        try:
                            update_group(int(row["id"]), name, active)
                            st.success("Group updated.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                confirm_group = st.checkbox(
                    "Confirm group deletion", key="confirm_group_delete"
                )
                if st.button("Delete Group", disabled=not confirm_group):
                    try:
                        delete_group(int(row["id"]))
                        st.success("Group deleted.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
    with tab2:
        stores = get_stores(active_only=False)
        st.dataframe(stores, hide_index=True, use_container_width=True)
        if groups.empty:
            st.warning("Add a group before adding stores.")
            return
        group_map = dict(zip(groups["group_name"], groups["id"]))
        with st.expander("Add store", expanded=stores.empty):
            with st.form("add_store"):
                new_store = st.text_input("Store name")
                group_name = st.selectbox("Group", list(group_map), key="new_store_group")
                if st.form_submit_button("Add Store", type="primary"):
                    try:
                        add_store(new_store, int(group_map[group_name]))
                        st.success("Store added.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        if not stores.empty:
            with st.expander("Edit or remove store"):
                store_label = st.selectbox(
                    "Select store", stores["store_name"].tolist(), key="edit_store"
                )
                row = stores[stores["store_name"] == store_label].iloc[0]
                with st.form("update_store"):
                    store_name = st.text_input("Store name", value=row["store_name"])
                    group_names = list(group_map)
                    current_group = (
                        group_names.index(row["group_name"])
                        if row["group_name"] in group_names
                        else 0
                    )
                    group_name = st.selectbox(
                        "Group", group_names, index=current_group, key="store_group_edit"
                    )
                    active = st.checkbox("Active", value=bool(row["active"]))
                    if st.form_submit_button("Save Store"):
                        try:
                            update_store(
                                int(row["id"]),
                                store_name,
                                int(group_map[group_name]),
                                active,
                            )
                            st.success("Store updated.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                confirm_store = st.checkbox(
                    "Confirm store deletion", key="confirm_store_delete"
                )
                if st.button("Delete Store", disabled=not confirm_store):
                    try:
                        delete_store(int(row["id"]))
                        st.success("Store deleted.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))


def render_audit_log() -> None:
    hero("Audit Trail", "A permanent record of creates, edits, and deletions")
    df = audit_history()
    st.dataframe(df, hide_index=True, use_container_width=True)
    downloads(df, "audit_trail")


def render_delivery_run() -> None:
    hero("Delivery Run", "Capture signatures store-by-store as you deliver")
    try:
        from streamlit_drawable_canvas import st_canvas
    except ImportError:
        st.error("Missing dependency: run `pip install streamlit-drawable-canvas` then restart.")
        return

    runs = get_delivery_runs()
    if runs.empty:
        st.info("No Delivery shipments in the last 14 days. Create one from New Shipment.")
        return

    # Build a label like "25 Jun — 5 stores / 23 boxes (3/5 signed)"
    def run_label(row) -> str:
        d = pd.Timestamp(row["Date"]).strftime("%-d %b")
        signed = int(row["signed_count"])
        total = int(row["total_stores"])
        boxes = int(row["total_boxes"])
        status = f"{signed}/{total} signed"
        return f"{d} — {total} stores / {boxes} boxes ({status})"

    run_labels = {run_label(r): int(r["id"]) for _, r in runs.iterrows()}
    selected_label = st.selectbox("Select delivery run", list(run_labels))
    shipment_id = run_labels[selected_label]

    details = get_delivery_details(shipment_id)

    # Summary strip
    total_stores = len(details)
    signed_count = int(details["signed_at"].notna().sum())
    total_boxes = int(details["boxes"].sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Stores", total_stores)
    c2.metric("Total Boxes", f"{total_boxes:,}")
    c3.metric("Signatures", f"{signed_count}/{total_stores}")

    if signed_count == total_stores:
        st.success("✅ All stores signed — delivery complete!")
    else:
        st.progress(signed_count / total_stores if total_stores else 0)

    st.divider()

    # Per-store signature capture
    for _, row in details.iterrows():
        store_name = row["store_name"]
        boxes = int(row["boxes"])
        is_signed = pd.notna(row["signed_at"])

        with st.expander(
            f"{'✅' if is_signed else '⬜'} {store_name} — {boxes} box{'es' if boxes != 1 else ''}",
            expanded=not is_signed,
        ):
            if is_signed:
                st.caption(f"Signed by: **{row['signed_by'] or 'Unknown'}** at {row['signed_at']}")
                if row["signature_data"]:
                    st.image(row["signature_data"], width=300)
                if st.button("Re-capture signature", key=f"redo_{row['detail_id']}"):
                    st.session_state[f"redo_{row['detail_id']}"] = True
                    st.rerun()
            else:
                signed_by = st.text_input(
                    "Receiver name (optional)",
                    key=f"name_{row['detail_id']}",
                    placeholder="e.g. Sarah",
                )
                st.caption("Ask the store person to sign below:")
                canvas_result = st_canvas(
                    fill_color="rgba(0,0,0,0)",
                    stroke_width=3,
                    stroke_color="#000000",
                    background_color="#ffffff",
                    height=150,
                    width=400,
                    drawing_mode="freedraw",
                    key=f"canvas_{shipment_id}_{row['detail_id']}",
                )
                if st.button("Save signature", key=f"save_{row['detail_id']}", type="primary"):
                    if (
                        canvas_result.image_data is not None
                        and canvas_result.image_data.sum() > 0
                    ):
                        import io, base64
                        from PIL import Image
                        img = Image.fromarray(canvas_result.image_data.astype("uint8"), "RGBA")
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        sig_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
                        save_signature(
                            shipment_id=shipment_id,
                            store_id=int(row["store_id"]),
                            store_name=store_name,
                            boxes=boxes,
                            signature_data=sig_b64,
                            signed_by=signed_by,
                        )
                        st.success(f"Signed for {store_name}!")
                        st.rerun()
                    else:
                        st.warning("Please capture a signature before saving.")


def render_address_book() -> None:
    """Admin screen for the Address Book and Store → Company mapping."""
    st.title("🗂 Address Book")
    st.caption(
        "Single source of truth for all delivery addresses. "
        "Import from Starshipit, map each store to a company, then courier bookings "
        "pull the address automatically."
    )

    tab_import, tab_companies, tab_mapping, tab_unmapped = st.tabs(
        ["📥 Import", "🏢 Companies", "🔗 Store Mapping", "⚠️ Unmapped Stores"]
    )

    # ── Import ────────────────────────────────────────────────────────────────
    with tab_import:
        st.markdown("### Import Address Book")
        st.markdown(
            "Export your Address Book from Starshipit (Settings → Address Book → Export) "
            "then upload the file here. Existing companies are updated; new ones are added."
        )
        st.caption(
            "Expected columns: **Company**, Name, Email, Telephone, Building, Street, "
            "Suburb, City, PostCode, Country, CountryCode, Instructions, Carrier, SignatureRequired"
        )
        uploaded = st.file_uploader(
            "Choose CSV or Excel file",
            type=["csv", "xlsx", "xls"],
            key="ab_upload",
        )
        if uploaded:
            if uploaded.name.lower().endswith(".csv"):
                df_raw = pd.read_csv(uploaded)
            else:
                df_raw = pd.read_excel(uploaded)
            df_raw.columns = [c.strip() for c in df_raw.columns]

            st.markdown(f"**Preview** — {len(df_raw)} rows found")
            st.dataframe(df_raw.head(10), use_container_width=True, hide_index=True)

            if st.button("Import into Address Book", type="primary", key="do_import"):
                records = []
                for _, row in df_raw.iterrows():
                    company = str(row.get("Company") or "").strip()
                    if not company:
                        continue

                    def _s(col: str, default: str = "") -> str | None:
                        v = str(row.get(col) or "").strip()
                        # Pandas reads numeric columns (e.g. PostCode) as float → strip ".0"
                        if v.endswith(".0"):
                            v = v[:-2]
                        return v if v and v.lower() not in ("nan", "none") else (default or None)

                    records.append({
                        "company_name":       company,
                        "contact_name":       _s("Name"),
                        "phone":              _s("Telephone"),
                        "email":              _s("Email"),
                        "code":               _s("Code"),
                        "building":           _s("Building"),
                        "street":             _s("Street"),
                        "suburb":             _s("Suburb"),
                        "city":               _s("City"),
                        "postcode":           _s("PostCode"),
                        "state":              _s("State"),
                        "country":            _s("Country", "New Zealand"),
                        "country_code":       _s("CountryCode", "NZ"),
                        "instructions":       _s("Instructions"),
                        "carrier":            _s("Carrier"),
                        "signature_required": bool(row.get("SignatureRequired", True)),
                    })
                with st.spinner("Importing…"):
                    count = import_address_book(records)
                st.success(f"✅ Imported {count} companies. Existing entries were updated in-place.")
                st.rerun()

    # ── Companies ─────────────────────────────────────────────────────────────
    with tab_companies:
        st.markdown("### All Companies")
        search = st.text_input(
            "Search", placeholder="Company name, city or suburb…", key="ab_search"
        )
        ab_df = get_address_book(search.strip())

        if ab_df.empty:
            msg = "No address book entries yet — import one above." if not search else "No matches."
            st.info(msg)
        else:
            st.dataframe(
                ab_df.drop(columns=["id"]),
                use_container_width=True,
                hide_index=True,
            )
            st.markdown(f"*{len(ab_df)} entries*")
            st.divider()
            st.markdown("### Edit an entry")

            ab_options = {row["company_name"]: row["id"] for _, row in ab_df.iterrows()}
            selected_company = st.selectbox(
                "Select company to edit", list(ab_options.keys()), key="ab_edit_sel"
            )
            ab_id = ab_options[selected_company]
            ab_row = ab_df[ab_df["id"] == ab_id].iloc[0]

            with st.form(f"edit_ab_{ab_id}"):
                c1, c2 = st.columns(2)
                new_company  = c1.text_input("Company Name *", value=ab_row["company_name"])
                new_contact  = c2.text_input("Contact Name", value=ab_row.get("contact_name") or "")
                c3, c4 = st.columns(2)
                new_phone    = c3.text_input("Phone", value=ab_row.get("phone") or "")
                new_email    = c4.text_input("Email", value=ab_row.get("email") or "")
                new_building = st.text_input("Building / Unit", value=ab_row.get("building") or "")
                new_street   = st.text_input("Street *", value=ab_row.get("street") or "")
                c5, c6, c7  = st.columns(3)
                new_suburb   = c5.text_input("Suburb", value=ab_row.get("suburb") or "")
                new_city     = c6.text_input("City *", value=ab_row.get("city") or "")
                new_postcode = c7.text_input("Postcode *", value=ab_row.get("postcode") or "")
                new_instruct = st.text_area(
                    "Delivery Instructions", value=ab_row.get("instructions") or ""
                )
                save_ab = st.form_submit_button("💾 Save Changes", type="primary")

            if save_ab:
                if not new_company.strip() or not new_street.strip():
                    st.error("Company Name and Street are required.")
                else:
                    update_address_book_entry(
                        ab_id,
                        company_name=new_company.strip(),
                        contact_name=new_contact.strip() or None,
                        phone=new_phone.strip() or None,
                        email=new_email.strip() or None,
                        building=new_building.strip() or None,
                        street=new_street.strip(),
                        suburb=new_suburb.strip() or None,
                        city=new_city.strip(),
                        postcode=new_postcode.strip(),
                        instructions=new_instruct.strip() or None,
                    )
                    st.success("Saved.")
                    st.rerun()

            with st.expander("🗑 Delete this entry"):
                st.warning(
                    "This permanently removes the company from the Address Book. "
                    "You must unmap any stores that point to it first."
                )
                if st.button("Delete permanently", key=f"del_ab_{ab_id}"):
                    try:
                        delete_address_book_entry(ab_id)
                        st.success("Deleted.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    # ── Store Mapping ─────────────────────────────────────────────────────────
    with tab_mapping:
        st.markdown("### Store → Company Mapping")
        st.caption(
            "Each store must be mapped to exactly one Address Book company before "
            "courier bookings can be created."
        )
        ab_all = get_address_book()
        if ab_all.empty:
            st.warning("Import an Address Book first (Import tab above).")
        else:
            # Auto-suggest banner
            suggestions = auto_suggest_mappings()
            if suggestions:
                st.info(
                    f"🔍 **{len(suggestions)} auto-suggestion(s) available** — "
                    "found 'Shosha + Store Name' matches in the Address Book."
                )
                col_a, col_b = st.columns([1, 2])
                if col_a.button("✅ Apply All Suggestions", type="primary", key="apply_all_sug"):
                    for s in suggestions:
                        set_store_mapping(s["store_id"], s["ab_id"])
                    st.success(f"Applied {len(suggestions)} mapping(s).")
                    st.rerun()

            # Build dropdown options
            ab_opt_names = ["— Unmapped —"] + list(ab_all["company_name"])
            ab_opt_ids   = {row["company_name"]: int(row["id"]) for _, row in ab_all.iterrows()}

            mappings = get_store_mappings()
            st.divider()

            current_group = None
            for _, s in mappings.iterrows():
                grp = s["group_name"]
                if grp != current_group:
                    st.markdown(f"**{grp}**")
                    current_group = grp

                current_name = s.get("company_name") or "— Unmapped —"
                try:
                    sel_idx = ab_opt_names.index(current_name)
                except ValueError:
                    sel_idx = 0

                col1, col2, col3 = st.columns([3, 5, 1])
                col1.markdown(s["store_name"])
                new_sel = col2.selectbox(
                    "Company",
                    ab_opt_names,
                    index=sel_idx,
                    key=f"map_{s['store_id']}",
                    label_visibility="collapsed",
                )
                if col3.button("Save", key=f"save_map_{s['store_id']}"):
                    if new_sel == "— Unmapped —":
                        delete_store_mapping(int(s["store_id"]))
                        st.success(f"Unmapped **{s['store_name']}**.")
                    else:
                        set_store_mapping(int(s["store_id"]), ab_opt_ids[new_sel])
                        st.success(f"Mapped **{s['store_name']}** → {new_sel}")
                    st.rerun()

    # ── Unmapped Stores ───────────────────────────────────────────────────────
    with tab_unmapped:
        st.markdown("### Unmapped Stores")
        unmapped = get_unmapped_stores()
        if unmapped.empty:
            st.success("✅ All active stores have Address Book mappings — courier bookings will work!")
        else:
            st.warning(
                f"**{len(unmapped)} store(s)** have no mapping and cannot receive courier bookings."
            )
            st.dataframe(
                unmapped[["store_name", "group_name"]].rename(
                    columns={"store_name": "Store", "group_name": "Group"}
                ),
                use_container_width=True,
                hide_index=True,
            )

            ab_all2 = get_address_book()
            if ab_all2.empty:
                st.info("Import an Address Book first to enable mapping.")
            else:
                suggestions2 = auto_suggest_mappings()
                if suggestions2:
                    st.markdown("#### Auto-suggestions (Shosha + Store Name matches)")
                    for s in suggestions2:
                        c1, c2, c3 = st.columns([3, 4, 1])
                        c1.markdown(f"**{s['store_name']}**")
                        c2.markdown(f"→ {s['company_name']}")
                        if c3.button("Apply", key=f"quick_map_{s['store_id']}"):
                            set_store_mapping(s["store_id"], s["ab_id"])
                            st.success(f"Mapped {s['store_name']}")
                            st.rerun()
                else:
                    st.info(
                        "No automatic suggestions found. "
                        "Go to the Store Mapping tab and assign companies manually."
                    )


def render_starshipit_diagnostics() -> None:
    from starshipit import get_order_details, _submit_for_label, list_available_services
    hero("Starshipit Diagnostics", "Inspect orders and find the correct carrier service code")

    # ── Step 0: List all configured carriers and service codes ────────────────
    st.subheader("Step 0 — Available carriers & service codes")
    st.caption("Shows every carrier/service configured for this Starshipit account. Use these codes in Step 2.")
    if st.button("List Available Services", key="list_services"):
        with st.spinner("Calling GET /api/carriers…"):
            services = list_available_services()
        st.json(services)
    st.divider()

    # ── Step 1: Inspect a recent order to see what Starshipit stored ─────────
    st.subheader("Step 1 — Inspect a recent order")
    st.caption(
        "Pick a recent booking. We'll call `GET /api/orders/{order_id}` and show "
        "what `service_code` and `carrier` Starshipit actually stored — "
        "those are the values the label endpoint needs."
    )

    recent = query_df(
        """
        SELECT cb.consignment_id, cb.store_name, cb.booking_status, cb.booked_at,
               s.id as shipment_id
        FROM courier_bookings cb
        JOIN shipments s ON s.id = cb.shipment_id
        WHERE cb.consignment_id IS NOT NULL AND cb.consignment_id != ''
        ORDER BY cb.booked_at DESC LIMIT 20
        """
    )

    if recent.empty:
        st.warning("No bookings with Starshipit order IDs found yet.")
    else:
        options = {
            f"SHP-{row['shipment_id']} — {row['store_name']} (ID: {row['consignment_id']})": row['consignment_id']
            for _, row in recent.iterrows()
        }
        chosen_label = st.selectbox("Select a booking", list(options))
        chosen_id    = options[chosen_label]
        st.caption(f"Starshipit order_id: **{chosen_id}**")

        if st.button("Fetch Order Details from Starshipit", type="primary"):
            with st.spinner("Calling GET /api/orders/…"):
                details = get_order_details(chosen_id)
            if "error" in details:
                st.error(details["error"])
            else:
                st.success("Order found. Key fields:")
                key_fields = {
                    k: details.get(k)
                    for k in ["order_id", "carrier_name", "service_code",
                               "carrier_service_code", "product_code",
                               "service_name", "order_number", "order_status"]
                    if details.get(k) is not None
                }
                st.json(key_fields)
                st.caption("Full response (all fields):")
                st.json(details)

    # ── Step 2: Test label endpoint ───────────────────────────────────────────
    st.divider()
    st.subheader("Step 2 — Test the label endpoint")
    st.caption(
        "Enter the Starshipit order_id and the `carrier_service_code` you discovered "
        "above. When this returns a label PDF, that code is correct — update "
        "`SERVICE_OPTIONS` in `starshipit.py` accordingly."
    )

    d_col1, d_col2 = st.columns(2)
    test_order_id = d_col1.text_input("Starshipit order_id (numeric)", value=chosen_id if not recent.empty else "")
    test_svc_code = d_col2.selectbox(
        "carrier_service_code to try",
        options=["CPOLP", "IWXOLP", "NZREG", "(empty — use stored)"],
        index=0,
    )

    if st.button("Test Label Endpoint", disabled=not test_order_id):
        code = "" if test_svc_code == "(empty — use stored)" else test_svc_code
        with st.spinner("Calling POST /api/orders/shipment…"):
            pdf, err = _submit_for_label(test_order_id, reprint=False, carrier_service_code=code)
        if pdf:
            st.success(f"✅ Label generated with `{code or '(none)'}` ({len(pdf):,} bytes) — this is the correct code!")
            st.download_button("Download Test Label", data=pdf, file_name="test_label.pdf", mime="application/pdf")
        else:
            st.error(f"❌ {err}")
            st.caption("Try a different code from the dropdown.")


PAGES = {
    "Dashboard": render_dashboard,
    "New Shipment": render_new_shipment,
    "Delivery Run": render_delivery_run,
    "History & Edit":             render_history,
    "Store Lookup": render_store_lookup,
    "Group Reporting": render_group_reporting,
    "Pallet Search": render_pallet_search,
    "Groups & Stores": render_store_management,
    "Address Book": render_address_book,
    "Audit Trail": render_audit_log,
    "Starshipit Diagnostics": render_starshipit_diagnostics,
}

with st.sidebar:
    st.markdown("## 📦 Shipment Tracker")
    st.caption("Wholesale distribution")
    page = st.radio("Navigation", list(PAGES), label_visibility="collapsed")
    st.divider()
    st.caption("Data stored in Supabase and backed by an audit trail.")

PAGES[page]()

