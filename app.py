from __future__ import annotations

from datetime import date, timedelta

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
    create_shipment,
    dashboard_metrics,
    delete_group,
    delete_shipment,
    delete_store,
    get_groups,
    get_shipment,
    get_stores,
    history,
    pallet_lookup,
    trend_data,
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
init_db()

st.markdown(
    """
    <style>
    :root { --brand: #0f766e; --ink: #102a43; }
    .stApp { background: #f5f7fa; }
    [data-testid="stSidebar"] { background: #102a43; }
    [data-testid="stSidebar"] * { color: #f8fafc; }
    [data-testid="stMetric"] {
        background: white; border: 1px solid #e5e7eb; border-radius: 14px;
        padding: 16px; box-shadow: 0 4px 18px rgba(15, 23, 42, .05);
    }
    .hero {
        background: linear-gradient(120deg, #0f766e, #155e75);
        color: white; padding: 22px 26px; border-radius: 16px; margin-bottom: 18px;
    }
    .hero h1 { margin: 0; font-size: 1.8rem; }
    .hero p { margin: 6px 0 0; opacity: .88; }
    .section-card {
        background: white; border: 1px solid #e5e7eb; border-radius: 14px;
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


def shipment_editor_rows(stores: pd.DataFrame, existing: pd.DataFrame | None = None):
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


def render_new_shipment() -> None:
    hero("New Shipment", "Record boxes by store in one quick entry")
    groups = get_groups()
    if groups.empty:
        st.warning("Add an active group and stores before recording shipments.")
        return
    group_map = dict(zip(groups["group_name"], groups["id"]))
    with st.form("new_shipment", clear_on_submit=False):
        c1, c2, c3 = st.columns([1, 1, 2])
        shipment_date = c1.date_input("Shipment Date", value=date.today())
        method = c2.selectbox("Shipment Method", METHODS)
        selected_group = c3.selectbox("Group", list(group_map))
        notes = st.text_area("Notes", placeholder="Optional reference or instructions")
        stores = get_stores(int(group_map[selected_group]))
        if stores.empty:
            st.warning("This group has no active stores.")
        editor = st.data_editor(
            shipment_editor_rows(stores),
            hide_index=True,
            use_container_width=True,
            disabled=["Store ID", "Store", "Group"],
            column_config={
                "Store ID": None,
                "Boxes": st.column_config.NumberColumn(
                    "Boxes", min_value=0, step=1, format="%d"
                ),
            },
            key=f"new_editor_{group_map[selected_group]}",
        )
        total = int(pd.to_numeric(editor["Boxes"], errors="coerce").fillna(0).sum())
        st.caption(f"Shipment total: **{total:,} boxes**")
        submitted = st.form_submit_button(
            "Save Shipment", type="primary", use_container_width=True
        )
    if submitted:
        details = [
            {
                "store_id": int(row["Store ID"]),
                "store_name": row["Store"],
                "group_name": row["Group"],
                "boxes": int(row["Boxes"] or 0),
            }
            for _, row in editor.iterrows()
        ]
        try:
            shipment_id, pallet_id = create_shipment(
                shipment_date, method, notes, details
            )
            message = f"Shipment #{shipment_id} saved with {total:,} boxes."
            if pallet_id:
                message += f" Pallet ID: {pallet_id}"
            st.success(message)
        except Exception as exc:
            st.error(str(exc))


def render_history() -> None:
    hero("Shipment History", "Filter, sort, export, edit, or delete records")
    stores = get_stores(active_only=False)
    groups = get_groups(active_only=False)
    today = date.today()
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
    with st.form(f"edit_{selected_id}"):
        e1, e2 = st.columns(2)
        edit_date = e1.date_input(
            "Shipment Date", value=pd.Timestamp(header["shipment_date"]).date()
        )
        edit_method = e2.selectbox(
            "Shipment Method", METHODS, index=METHODS.index(header["shipment_method"])
        )
        edit_notes = st.text_area("Notes", value=header["notes"] or "")
        edit_rows = shipment_editor_rows(all_stores, current_details)
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
    today = pd.Timestamp(date.today())
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


PAGES = {
    "Dashboard": render_dashboard,
    "New Shipment": render_new_shipment,
    "History & Edit": render_history,
    "Store Lookup": render_store_lookup,
    "Group Reporting": render_group_reporting,
    "Pallet Search": render_pallet_search,
    "Groups & Stores": render_store_management,
    "Audit Trail": render_audit_log,
}

with st.sidebar:
    st.markdown("## 📦 Shipment Tracker")
    st.caption("Wholesale distribution")
    page = st.radio("Navigation", list(PAGES), label_visibility="collapsed")
    st.divider()
    st.caption("SQLite data is stored locally and backed by an audit trail.")

PAGES[page]()

