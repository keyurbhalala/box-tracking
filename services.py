from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pandas as pd

from database import audit, connection


METHODS = ["Courier", "Pallet", "Delivery"]


def query_df(sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame. Converts ? → %s for psycopg2."""
    with connection() as conn:
        return pd.read_sql_query(
            sql.replace("?", "%s"), conn.raw, params=tuple(params)
        )


def get_groups(active_only: bool = True) -> pd.DataFrame:
    where = "WHERE active = 1" if active_only else ""
    return query_df(
        f"""
        SELECT id, group_name, active
        FROM store_groups
        {where}
        ORDER BY LOWER(group_name)
        """
    )


def get_stores(group_id: int | None = None, active_only: bool = True) -> pd.DataFrame:
    clauses, params = [], []
    if group_id is not None:
        clauses.append("group_id = ?")
        params.append(group_id)
    if active_only:
        clauses.append("active = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return query_df(
        f"""
        SELECT id, store_name, group_id, group_name, active
        FROM stores
        {where}
        ORDER BY LOWER(store_name)
        """,
        params,
    )


def add_group(group_name: str) -> int:
    name = group_name.strip()
    if not name:
        raise ValueError("Group name is required.")
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO store_groups (group_name) VALUES (?) RETURNING id",
            (name,),
        ).fetchone()
        group_id = row["id"]
        audit(conn, "group", group_id, "CREATE", new_values={"group_name": name})
        return group_id


def update_group(group_id: int, group_name: str, active: bool) -> None:
    name = group_name.strip()
    if not name:
        raise ValueError("Group name is required.")
    with connection() as conn:
        old = dict(
            conn.execute(
                "SELECT * FROM store_groups WHERE id = ?", (group_id,)
            ).fetchone()
        )
        conn.execute(
            """
            UPDATE store_groups
            SET group_name = ?, active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, int(active), group_id),
        )
        conn.execute(
            """
            UPDATE stores
            SET group_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE group_id = ?
            """,
            (name, group_id),
        )
        audit(
            conn,
            "group",
            group_id,
            "UPDATE",
            old_values=old,
            new_values={"group_name": name, "active": active},
        )


def delete_group(group_id: int) -> None:
    with connection() as conn:
        stores = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stores WHERE group_id = ?", (group_id,)
        ).fetchone()["cnt"]
        if stores:
            raise ValueError("Move or remove the stores in this group first.")
        old = conn.execute(
            "SELECT * FROM store_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if old:
            conn.execute("DELETE FROM store_groups WHERE id = ?", (group_id,))
            audit(conn, "group", group_id, "DELETE", old_values=dict(old))


def add_store(store_name: str, group_id: int) -> int:
    name = store_name.strip()
    if not name:
        raise ValueError("Store name is required.")
    with connection() as conn:
        group = conn.execute(
            "SELECT group_name FROM store_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if not group:
            raise ValueError("Selected group does not exist.")
        row = conn.execute(
            """
            INSERT INTO stores (store_name, group_id, group_name)
            VALUES (?, ?, ?) RETURNING id
            """,
            (name, group_id, group["group_name"]),
        ).fetchone()
        store_id = row["id"]
        audit(
            conn,
            "store",
            store_id,
            "CREATE",
            new_values={"store_name": name, "group_name": group["group_name"]},
        )
        return store_id


def update_store(store_id: int, store_name: str, group_id: int, active: bool) -> None:
    name = store_name.strip()
    if not name:
        raise ValueError("Store name is required.")
    with connection() as conn:
        old = dict(
            conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        )
        group = conn.execute(
            "SELECT group_name FROM store_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if not group:
            raise ValueError("Selected group does not exist.")
        conn.execute(
            """
            UPDATE stores
            SET store_name = ?, group_id = ?, group_name = ?, active = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, group_id, group["group_name"], int(active), store_id),
        )
        audit(
            conn,
            "store",
            store_id,
            "UPDATE",
            old_values=old,
            new_values={
                "store_name": name,
                "group_name": group["group_name"],
                "active": active,
            },
        )


def delete_store(store_id: int) -> None:
    with connection() as conn:
        old = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        if old:
            conn.execute("DELETE FROM stores WHERE id = ?", (store_id,))
            audit(conn, "store", store_id, "DELETE", old_values=dict(old))


def _next_pallet_id(conn, shipment_date: date | str) -> str:
    prefix = f"PAL-{pd.Timestamp(shipment_date).strftime('%Y%m%d')}-"
    row = conn.execute(
        """
        SELECT pallet_id FROM shipments
        WHERE pallet_id LIKE ?
        ORDER BY pallet_id DESC LIMIT 1
        """,
        (f"{prefix}%",),
    ).fetchone()
    sequence = int(row["pallet_id"].split("-")[-1]) + 1 if row else 1
    return f"{prefix}{sequence:03d}"


def create_shipment(
    shipment_date: date,
    shipment_method: str,
    notes: str,
    details: list[dict[str, Any]],
) -> tuple[int, str | None]:
    if shipment_method not in METHODS:
        raise ValueError("Invalid shipment method.")
    cleaned = [item for item in details if int(item.get("boxes", 0)) > 0]
    if not cleaned:
        raise ValueError("Enter boxes for at least one store.")

    with connection() as conn:
        pallet_id = (
            _next_pallet_id(conn, shipment_date)
            if shipment_method == "Pallet"
            else None
        )
        row = conn.execute(
            """
            INSERT INTO shipments
                (shipment_date, shipment_method, pallet_id, notes)
            VALUES (?, ?, ?, ?) RETURNING id
            """,
            (shipment_date.isoformat(), shipment_method, pallet_id, notes.strip()),
        ).fetchone()
        shipment_id = row["id"]
        conn.executemany(
            """
            INSERT INTO shipment_details
                (shipment_id, store_id, store_name, group_name, boxes)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    shipment_id,
                    int(item["store_id"]),
                    item["store_name"],
                    item["group_name"],
                    int(item["boxes"]),
                )
                for item in cleaned
            ],
        )
        audit(
            conn,
            "shipment",
            shipment_id,
            "CREATE",
            new_values={
                "shipment_date": shipment_date.isoformat(),
                "shipment_method": shipment_method,
                "pallet_id": pallet_id,
                "notes": notes,
                "details": cleaned,
            },
        )
        return shipment_id, pallet_id


def get_shipment(shipment_id: int) -> tuple[dict[str, Any], pd.DataFrame]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
        ).fetchone()
        if not row:
            raise ValueError("Shipment not found.")
        details = pd.read_sql_query(
            """
            SELECT id, store_id, store_name, group_name, boxes
            FROM shipment_details WHERE shipment_id = %s
            ORDER BY store_name
            """,
            conn.raw,
            params=(shipment_id,),
        )
        return dict(row), details


def update_shipment(
    shipment_id: int,
    shipment_date: date,
    shipment_method: str,
    notes: str,
    details: list[dict[str, Any]],
) -> str | None:
    cleaned = [item for item in details if int(item.get("boxes", 0)) > 0]
    if not cleaned:
        raise ValueError("A shipment must contain at least one store.")
    with connection() as conn:
        old_header = conn.execute(
            "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
        ).fetchone()
        if not old_header:
            raise ValueError("Shipment not found.")
        old_details = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM shipment_details WHERE shipment_id = ?", (shipment_id,)
            ).fetchall()
        ]
        pallet_id = old_header["pallet_id"]
        if shipment_method == "Pallet" and not pallet_id:
            pallet_id = _next_pallet_id(conn, shipment_date)
        elif shipment_method != "Pallet":
            pallet_id = None
        conn.execute(
            """
            UPDATE shipments
            SET shipment_date = ?, shipment_method = ?, pallet_id = ?, notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                shipment_date.isoformat(),
                shipment_method,
                pallet_id,
                notes.strip(),
                shipment_id,
            ),
        )
        conn.execute(
            "DELETE FROM shipment_details WHERE shipment_id = ?", (shipment_id,)
        )
        conn.executemany(
            """
            INSERT INTO shipment_details
                (shipment_id, store_id, store_name, group_name, boxes)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    shipment_id,
                    int(item["store_id"]),
                    item["store_name"],
                    item["group_name"],
                    int(item["boxes"]),
                )
                for item in cleaned
            ],
        )
        audit(
            conn,
            "shipment",
            shipment_id,
            "UPDATE",
            old_values={"header": dict(old_header), "details": old_details},
            new_values={
                "shipment_date": shipment_date.isoformat(),
                "shipment_method": shipment_method,
                "pallet_id": pallet_id,
                "notes": notes,
                "details": cleaned,
            },
        )
        return pallet_id


def delete_shipment(shipment_id: int) -> None:
    with connection() as conn:
        header = conn.execute(
            "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
        ).fetchone()
        if not header:
            return
        details = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM shipment_details WHERE shipment_id = ?", (shipment_id,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM shipments WHERE id = ?", (shipment_id,))
        audit(
            conn,
            "shipment",
            shipment_id,
            "DELETE",
            old_values={"header": dict(header), "details": details},
        )


def history(
    start_date: date | None = None,
    end_date: date | None = None,
    store_ids: list[int] | None = None,
    groups: list[str] | None = None,
    methods: list[str] | None = None,
) -> pd.DataFrame:
    clauses, params = [], []
    if start_date:
        clauses.append("s.shipment_date >= ?")
        params.append(start_date.isoformat())
    if end_date:
        clauses.append("s.shipment_date <= ?")
        params.append(end_date.isoformat())
    if store_ids:
        marks = ",".join("?" for _ in store_ids)
        clauses.append(f"d.store_id IN ({marks})")
        params.extend(store_ids)
    if groups:
        marks = ",".join("?" for _ in groups)
        clauses.append(f"d.group_name IN ({marks})")
        params.extend(groups)
    if methods:
        marks = ",".join("?" for _ in methods)
        clauses.append(f"s.shipment_method IN ({marks})")
        params.extend(methods)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return query_df(
        f"""
        SELECT
            s.id AS shipment_id,
            s.shipment_date AS "Date",
            d.store_name AS "Store",
            d.group_name AS "Group",
            d.boxes AS "Boxes",
            s.shipment_method AS "Method",
            COALESCE(s.pallet_id, '') AS "Pallet ID",
            s.notes AS "Notes"
        FROM shipment_details d
        JOIN shipments s ON s.id = d.shipment_id
        {where}
        ORDER BY s.shipment_date DESC, s.id DESC, d.store_name
        """,
        params,
    )


def dashboard_metrics() -> dict[str, int]:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    with connection() as conn:
        boxes_today = conn.execute(
            """
            SELECT COALESCE(SUM(d.boxes), 0) AS val
            FROM shipment_details d JOIN shipments s ON s.id = d.shipment_id
            WHERE s.shipment_date = ?
            """,
            (today.isoformat(),),
        ).fetchone()["val"]
        shipments_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM shipments WHERE shipment_date = ?",
            (today.isoformat(),),
        ).fetchone()["cnt"]
        method_counts = {
            method: conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM shipments
                WHERE shipment_date >= ? AND shipment_method = ?
                """,
                (week_start.isoformat(), method),
            ).fetchone()["cnt"]
            for method in METHODS
        }
    return {
        "boxes_today": boxes_today,
        "shipments_today": shipments_today,
        "pallets_week": method_counts["Pallet"],
        "couriers_week": method_counts["Courier"],
        "deliveries_week": method_counts["Delivery"],
    }


def trend_data() -> pd.DataFrame:
    df = history(start_date=date.today() - timedelta(days=730))
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def pallet_lookup(pallet_id: str) -> tuple[pd.DataFrame, dict[str, Any] | None]:
    value = pallet_id.strip().upper()
    if not value:
        return pd.DataFrame(), None
    with connection() as conn:
        header = conn.execute(
            """
            SELECT id, shipment_date, shipment_method, pallet_id, notes, created_at
            FROM shipments WHERE UPPER(pallet_id) = ?
            """,
            (value,),
        ).fetchone()
        if not header:
            return pd.DataFrame(), None
        details = pd.read_sql_query(
            """
            SELECT store_name AS "Store", group_name AS "Group", boxes AS "Boxes"
            FROM shipment_details WHERE shipment_id = %s
            ORDER BY store_name
            """,
            conn.raw,
            params=(header["id"],),
        )
        return details, dict(header)


def audit_history(limit: int = 500) -> pd.DataFrame:
    return query_df(
        """
        SELECT changed_at AS "Changed At", entity_type AS "Entity",
               entity_id AS "Record ID", action AS "Action",
               changed_by AS "Changed By", old_values AS "Before",
               new_values AS "After"
        FROM audit_log
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
