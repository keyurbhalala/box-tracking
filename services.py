from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import text as _sa_text

import streamlit as st

from database import audit, connection, get_engine


METHODS = ["Courier", "Pallet", "Delivery"]


def query_df(sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
    """
    Run a SELECT and return a DataFrame via a SQLAlchemy engine.

    Using the engine instead of a raw psycopg2 connection avoids the
    pandas DeprecationWarning introduced in pandas 2.x.

    Placeholder style: accepts either ``?`` (sqlite-style) or ``%s``
    (psycopg2-style); both are converted to SQLAlchemy named params
    ``:p0``, ``:p1``, … so that ``sqlalchemy.text()`` can bind them.
    """
    params_list = list(params)
    # Normalise to %s first, then rebind to :p0, :p1, …
    sa_sql = sql.replace("?", "%s")
    named: dict[str, Any] = {}
    for i, v in enumerate(params_list):
        sa_sql = sa_sql.replace("%s", f":p{i}", 1)
        named[f"p{i}"] = v
    with get_engine().connect() as sa_conn:
        return pd.read_sql_query(_sa_text(sa_sql), sa_conn, params=named)


@st.cache_data(ttl=60)
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


@st.cache_data(ttl=60)
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


def _clear_store_cache() -> None:
    get_groups.clear()
    get_stores.clear()


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
    _clear_store_cache()
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
    _clear_store_cache()


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
    _clear_store_cache()


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
    _clear_store_cache()
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
    _clear_store_cache()


def delete_store(store_id: int) -> None:
    with connection() as conn:
        old = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        if old:
            conn.execute("DELETE FROM stores WHERE id = ?", (store_id,))
            audit(conn, "store", store_id, "DELETE", old_values=dict(old))
    _clear_store_cache()


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
        details = query_df(
            """
            SELECT id, store_id, store_name, group_name, boxes
            FROM shipment_details WHERE shipment_id = ?
            ORDER BY store_name
            """,
            (shipment_id,),
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
        details = query_df(
            """
            SELECT store_name AS "Store", group_name AS "Group", boxes AS "Boxes"
            FROM shipment_details WHERE shipment_id = ?
            ORDER BY store_name
            """,
            (header["id"],),
        )
        return details, dict(header)


def get_delivery_runs(days_back: int = 14) -> pd.DataFrame:
    """Return recent Delivery shipments with signature progress."""
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    return query_df(
        """
        SELECT
            s.id,
            s.shipment_date AS "Date",
            s.notes AS "Notes",
            COUNT(d.id) AS total_stores,
            COALESCE(SUM(d.boxes), 0) AS total_boxes,
            COUNT(sig.id) AS signed_count
        FROM shipments s
        JOIN shipment_details d ON d.shipment_id = s.id
        LEFT JOIN delivery_signatures sig
               ON sig.shipment_id = s.id AND sig.signed_at IS NOT NULL
        WHERE s.shipment_method = 'Delivery'
          AND s.shipment_date >= ?
        GROUP BY s.id, s.shipment_date, s.notes
        ORDER BY s.shipment_date DESC, s.id DESC
        """,
        (cutoff,),
    )


def get_delivery_details(shipment_id: int) -> pd.DataFrame:
    """Return stores for a delivery shipment merged with any existing signatures."""
    return query_df(
        """
        SELECT
            d.id AS detail_id,
            d.store_id,
            d.store_name,
            d.group_name,
            d.boxes,
            sig.id AS sig_id,
            sig.signed_by,
            sig.signature_data,
            sig.signed_at
        FROM shipment_details d
        LEFT JOIN delivery_signatures sig
               ON sig.shipment_id = d.shipment_id AND sig.store_id = d.store_id
        WHERE d.shipment_id = ?
        ORDER BY d.store_name
        """,
        (shipment_id,),
    )


def save_signature(
    shipment_id: int,
    store_id: int,
    store_name: str,
    boxes: int,
    signature_data: str,
    signed_by: str = "",
) -> None:
    """Upsert a signature for one store in a delivery shipment."""
    with connection() as conn:
        existing = conn.execute(
            "SELECT id FROM delivery_signatures WHERE shipment_id = ? AND store_id = ?",
            (shipment_id, store_id),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE delivery_signatures
                SET signature_data = ?, signed_by = ?, signed_at = ?
                WHERE id = ?
                """,
                (
                    signature_data,
                    signed_by.strip(),
                    datetime.now().isoformat(timespec="seconds"),
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO delivery_signatures
                    (shipment_id, store_id, store_name, boxes, signature_data, signed_by, signed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shipment_id,
                    store_id,
                    store_name,
                    boxes,
                    signature_data,
                    signed_by.strip(),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )


# ---------------------------------------------------------------------------
# Starshipit / Courier booking
# ---------------------------------------------------------------------------


def get_warehouse() -> dict[str, Any]:
    """Return the warehouse sender details from warehouse_settings."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM warehouse_settings ORDER BY id LIMIT 1"
        ).fetchone()
        return dict(row) if row else {}


def get_store_with_address(store_id: int) -> dict[str, Any] | None:
    """
    Return the address_book entry mapped to this store.
    Returns None when no mapping exists — the caller must refuse the booking
    and direct the user to Admin → Address Book.
    """
    with connection() as conn:
        row = conn.execute(
            """
            SELECT ab.*
            FROM store_address_mapping m
            JOIN address_book ab ON ab.id = m.address_book_id
            WHERE m.store_id = ?
            """,
            (store_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Address Book
# ---------------------------------------------------------------------------


def import_address_book(records: list[dict]) -> int:
    """
    Bulk upsert address book records from an imported CSV/Excel.

    Matches on company_name (case-insensitive).  Existing entries are updated;
    new entries are inserted.  Returns the total number of records processed.
    """
    if not records:
        return 0
    with connection() as conn:
        for r in records:
            existing = conn.execute(
                "SELECT id FROM address_book WHERE LOWER(company_name) = LOWER(?)",
                (r["company_name"],),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE address_book
                    SET contact_name = ?, phone = ?, email = ?, code = ?,
                        building = ?, street = ?, suburb = ?, city = ?,
                        postcode = ?, state = ?, country = ?, country_code = ?,
                        instructions = ?, carrier = ?, signature_required = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        r.get("contact_name"), r.get("phone"), r.get("email"),
                        r.get("code"), r.get("building"), r.get("street"),
                        r.get("suburb"), r.get("city"), r.get("postcode"),
                        r.get("state"), r.get("country"), r.get("country_code"),
                        r.get("instructions"), r.get("carrier"),
                        int(bool(r.get("signature_required", True))),
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO address_book
                        (company_name, contact_name, phone, email, code,
                         building, street, suburb, city, postcode, state,
                         country, country_code, instructions, carrier,
                         signature_required)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["company_name"], r.get("contact_name"), r.get("phone"),
                        r.get("email"), r.get("code"), r.get("building"),
                        r.get("street"), r.get("suburb"), r.get("city"),
                        r.get("postcode"), r.get("state"),
                        r.get("country", "New Zealand"),
                        r.get("country_code", "NZ"),
                        r.get("instructions"), r.get("carrier"),
                        int(bool(r.get("signature_required", True))),
                    ),
                )
    _clear_address_book_cache()
    return len(records)


@st.cache_data(ttl=60)
def get_address_book(search: str = "") -> pd.DataFrame:
    """Return all address book entries, optionally filtered by search term."""
    if search:
        return query_df(
            """
            SELECT id, company_name, contact_name, phone, email,
                   building, street, suburb, city, postcode,
                   country_code, instructions, carrier, signature_required
            FROM address_book
            WHERE LOWER(company_name) LIKE LOWER(?)
               OR LOWER(city)         LIKE LOWER(?)
               OR LOWER(suburb)       LIKE LOWER(?)
            ORDER BY LOWER(company_name)
            """,
            (f"%{search}%", f"%{search}%", f"%{search}%"),
        )
    return query_df(
        """
        SELECT id, company_name, contact_name, phone, email,
               building, street, suburb, city, postcode,
               country_code, instructions, carrier, signature_required
        FROM address_book
        ORDER BY LOWER(company_name)
        """
    )


def update_address_book_entry(ab_id: int, **fields) -> None:
    """Update specific columns in an address_book row."""
    allowed = {
        "company_name", "contact_name", "phone", "email", "code",
        "building", "street", "suburb", "city", "postcode", "state",
        "country", "country_code", "instructions", "carrier",
        "signature_required",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with connection() as conn:
        conn.execute(
            f"UPDATE address_book SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (*updates.values(), ab_id),
        )
    _clear_address_book_cache()


def delete_address_book_entry(ab_id: int) -> None:
    """Delete an address book entry.  Raises if any stores are still mapped to it."""
    with connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM store_address_mapping WHERE address_book_id = ?",
            (ab_id,),
        ).fetchone()["c"]
        if count:
            raise ValueError(
                f"{count} store(s) are mapped to this address. Remove the mapping(s) first."
            )
        conn.execute("DELETE FROM address_book WHERE id = ?", (ab_id,))
    _clear_address_book_cache()


def get_store_mappings() -> pd.DataFrame:
    """All active stores with their mapped Address Book company (NULL columns if unmapped)."""
    return query_df(
        """
        SELECT s.id AS store_id, s.store_name, s.group_name,
               ab.id AS address_book_id, ab.company_name,
               ab.city, ab.postcode
        FROM stores s
        LEFT JOIN store_address_mapping m ON m.store_id = s.id
        LEFT JOIN address_book ab         ON ab.id = m.address_book_id
        WHERE s.active = 1
        ORDER BY LOWER(s.group_name), LOWER(s.store_name)
        """
    )


def get_store_mapping(store_id: int) -> dict[str, Any] | None:
    """Return the address_book row mapped to store_id, or None."""
    with connection() as conn:
        row = conn.execute(
            """
            SELECT ab.*
            FROM store_address_mapping m
            JOIN address_book ab ON ab.id = m.address_book_id
            WHERE m.store_id = ?
            """,
            (store_id,),
        ).fetchone()
        return dict(row) if row else None


def set_store_mapping(store_id: int, address_book_id: int) -> None:
    """Create or replace the mapping from a store to an address book entry."""
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO store_address_mapping (store_id, address_book_id)
            VALUES (?, ?)
            ON CONFLICT (store_id) DO UPDATE
                SET address_book_id = EXCLUDED.address_book_id,
                    mapped_at = CURRENT_TIMESTAMP
            """,
            (store_id, address_book_id),
        )
        audit(conn, "store_mapping", store_id, "UPSERT",
              new_values={"address_book_id": address_book_id})
    _clear_store_cache()


def delete_store_mapping(store_id: int) -> None:
    """Remove the address mapping for a store."""
    with connection() as conn:
        conn.execute(
            "DELETE FROM store_address_mapping WHERE store_id = ?", (store_id,)
        )
        audit(conn, "store_mapping", store_id, "DELETE")
    _clear_store_cache()


def get_unmapped_stores() -> pd.DataFrame:
    """Return active stores that have no address book mapping."""
    return query_df(
        """
        SELECT s.id, s.store_name, s.group_name
        FROM stores s
        LEFT JOIN store_address_mapping m ON m.store_id = s.id
        WHERE s.active = 1 AND m.id IS NULL
        ORDER BY LOWER(s.group_name), LOWER(s.store_name)
        """
    )


def auto_suggest_mappings() -> list[dict]:
    """
    Suggest mappings for unmapped stores where 'Shosha ' + store_name
    matches an address_book company_name (case-insensitive).

    Returns a list of dicts: {store_id, store_name, ab_id, company_name}.
    """
    unmapped = get_unmapped_stores()
    if unmapped.empty:
        return []
    ab = get_address_book()
    if ab.empty:
        return []
    company_lower: dict[str, dict] = {
        row["company_name"].lower(): dict(row)
        for _, row in ab.iterrows()
    }
    suggestions = []
    for _, s in unmapped.iterrows():
        candidate = f"shosha {s['store_name'].lower()}"
        if candidate in company_lower:
            ab_row = company_lower[candidate]
            suggestions.append({
                "store_id":    int(s["id"]),
                "store_name":  s["store_name"],
                "ab_id":       int(ab_row["id"]),
                "company_name": ab_row["company_name"],
            })
    return suggestions


def _clear_address_book_cache() -> None:
    get_address_book.clear()


def save_courier_booking(
    shipment_id: int,
    store_id: int,
    store_name: str,
    boxes: int,
    weight_per_box: float,
    length: float,
    width: float,
    height: float,
    service_code: str,
    result: Any,  # starshipit.BookingResult
) -> int:
    """
    Persist a courier booking result to courier_bookings.
    Returns the new row id.
    """
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO courier_bookings
                (shipment_id, store_id, store_name, boxes,
                 weight_per_box, length, width, height, service_code,
                 tracking_number, label_url, consignment_id,
                 carrier, booking_status, booked_at,
                 api_response, api_error, retry_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            RETURNING id
            """,
            (
                shipment_id, store_id, store_name, boxes,
                weight_per_box, length, width, height, service_code,
                result.tracking_number or None,
                result.label_url or None,
                result.consignment_id or None,
                result.carrier or None,
                result.booking_status,
                result.booked_at or None,
                (result.api_response or "")[:10_000] or None,
                (result.error or "")[:2_000] or None,
            ),
        ).fetchone()
        booking_id = row["id"]
        audit(
            conn, "courier_booking", booking_id, "CREATE",
            new_values={
                "shipment_id": shipment_id,
                "store_name": store_name,
                "status": result.booking_status,
                "tracking": result.tracking_number,
            },
        )
    return booking_id


def retry_courier_booking(booking_id: int) -> tuple[bool, str]:
    """
    Retry a failed courier booking.

    Updates the existing courier_bookings row — never inserts a duplicate.
    Returns (success, tracking_number_or_error_message).
    """
    from starshipit import create_order, Address, Package

    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM courier_bookings WHERE id = ?", (booking_id,)
        ).fetchone()
        if not row:
            return False, "Booking record not found."
        if row["booking_status"] == "Booked":
            return False, "Already successfully booked."
        booking = dict(row)

    store    = get_store_with_address(booking["store_id"])
    warehouse = get_warehouse()

    if not store or not store.get("street"):
        return (
            False,
            f"No address book mapping for store '{booking['store_name']}'. "
            "Go to Admin → Address Book to map this store.",
        )
    if not warehouse:
        return False, "Warehouse settings not found in database."

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
    recipient = Address(
        name=store.get("contact_name") or store.get("company_name") or booking["store_name"],
        company=store.get("company_name") or booking["store_name"],
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
        boxes=booking["boxes"],
        weight_per_box=booking["weight_per_box"] or 1.0,
        length=booking["length"] or 30.0,
        width=booking["width"] or 20.0,
        height=booking["height"] or 15.0,
    )
    ref = f"SHP-{booking['shipment_id']}-{booking['store_id']}"
    svc = booking.get("service_code") or "NZREG"

    result = create_order(sender, recipient, pkg, ref, svc)

    # Label bytes are handled in the UI layer for new bookings.
    # For retries we can't store binary in the DB, so leave label_url as-is.
    label_url = result.label_url or ""

    with connection() as conn:
        conn.execute(
            """
            UPDATE courier_bookings
            SET tracking_number = ?, label_url     = ?, consignment_id = ?,
                carrier         = ?, service_code  = ?, booking_status  = ?,
                booked_at       = ?, api_response  = ?, api_error       = ?,
                retry_count     = retry_count + 1
            WHERE id = ?
            """,
            (
                result.tracking_number or None,
                label_url or None,
                result.consignment_id or None,
                result.carrier or None,
                result.service_code or None,
                result.booking_status,
                result.booked_at or None,
                (result.api_response or "")[:10_000] or None,
                (result.error or "")[:2_000] or None,
                booking_id,
            ),
        )
        audit(
            conn, "courier_booking", booking_id, "RETRY",
            new_values={
                "status": result.booking_status,
                "tracking": result.tracking_number,
                "error": result.error,
            },
        )

    return result.success, result.tracking_number or result.error


def get_courier_bookings(shipment_id: int) -> pd.DataFrame:
    """Return all courier bookings for a shipment, latest retry per store."""
    return query_df(
        """
        SELECT id, store_name, boxes,
               tracking_number, label_url, carrier,
               service_code, booking_status, booked_at,
               api_error, retry_count, consignment_id
        FROM courier_bookings
        WHERE shipment_id = ?
        ORDER BY store_name
        """,
        (shipment_id,),
    )


# ---------------------------------------------------------------------------
# Store-to-store courier transfers
# ---------------------------------------------------------------------------


def save_store_transfer(
    transfer_date: date,
    source_store_id: int,
    source_store_name: str,
    destination_store_id: int,
    destination_store_name: str,
    courier_type: str,
    weight: float,
    length: float,
    width: float,
    height: float,
    service_code: str,
    estimated_cost: float | None,
    notes: str,
    result: Any,  # starshipit.BookingResult
) -> int:
    """
    Persist a store-to-store courier transfer booking to store_transfers.
    Returns the new row id.
    """
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO store_transfers
                (transfer_date, source_store_id, source_store_name,
                 destination_store_id, destination_store_name, courier_type,
                 weight, length, width, height, service_code, estimated_cost,
                 notes, tracking_number, label_url, consignment_id, carrier,
                 booking_status, booked_at, api_response, api_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                transfer_date.isoformat(), source_store_id, source_store_name,
                destination_store_id, destination_store_name, courier_type,
                weight, length, width, height, service_code, estimated_cost,
                (notes or "").strip() or None,
                result.tracking_number or None,
                result.label_url or None,
                result.consignment_id or None,
                result.carrier or None,
                result.booking_status,
                result.booked_at or None,
                (result.api_response or "")[:10_000] or None,
                (result.error or "")[:2_000] or None,
            ),
        ).fetchone()
        transfer_id = row["id"]
        audit(
            conn, "store_transfer", transfer_id, "CREATE",
            new_values={
                "source": source_store_name,
                "destination": destination_store_name,
                "courier_type": courier_type,
                "status": result.booking_status,
                "tracking": result.tracking_number,
                "estimated_cost": estimated_cost,
            },
        )
    return transfer_id


def get_store_transfers(limit: int = 50) -> pd.DataFrame:
    """Return the most recent store-to-store transfers, newest first."""
    return query_df(
        """
        SELECT id, transfer_date AS "Date",
               source_store_name AS "From", destination_store_name AS "To",
               courier_type AS "Type", weight AS "Wt (kg)",
               estimated_cost AS "Est. Cost", tracking_number AS "Tracking",
               carrier AS "Carrier", booking_status AS "Status",
               booked_at AS "Booked At"
        FROM store_transfers
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )


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
