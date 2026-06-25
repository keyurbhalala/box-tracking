from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool
import streamlit as st


# ---------------------------------------------------------------------------
# Connection pool — created once per session, reused on every interaction
# ---------------------------------------------------------------------------

@st.cache_resource
def _pool() -> psycopg2.pool.ThreadedConnectionPool:
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=st.secrets["DATABASE_URL"],
    )


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

class _Conn:
    """Thin adapter so psycopg2 looks like sqlite3 to callers.

    * Converts ``?`` placeholders to ``%s`` automatically.
    * Exposes ``.raw`` for pd.read_sql_query which needs the bare connection.
    """

    def __init__(self, raw: psycopg2.extensions.connection) -> None:
        self._raw = raw
        self._cur = raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    @property
    def raw(self) -> psycopg2.extensions.connection:
        return self._raw

    def execute(self, sql: str, params: tuple = ()) -> psycopg2.extras.RealDictCursor:
        self._cur.execute(sql.replace("?", "%s"), params or ())
        return self._cur

    def executemany(self, sql: str, params_seq) -> None:
        psycopg2.extras.execute_batch(
            self._cur, sql.replace("?", "%s"), list(params_seq)
        )


@contextmanager
def connection() -> Iterator[_Conn]:
    pool = _pool()
    raw = pool.getconn()
    raw.autocommit = False
    try:
        yield _Conn(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        pool.putconn(raw)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS store_groups (
        id              SERIAL PRIMARY KEY,
        group_name      TEXT NOT NULL UNIQUE,
        active          INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stores (
        id              SERIAL PRIMARY KEY,
        store_name      TEXT NOT NULL UNIQUE,
        group_id        INTEGER,
        group_name      TEXT NOT NULL,
        active          INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (group_id) REFERENCES store_groups(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shipments (
        id                  SERIAL PRIMARY KEY,
        shipment_date       TEXT NOT NULL,
        shipment_method     TEXT NOT NULL
                                CHECK (shipment_method IN ('Courier', 'Pallet', 'Delivery')),
        pallet_id           TEXT UNIQUE,
        notes               TEXT,
        created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shipment_details (
        id              SERIAL PRIMARY KEY,
        shipment_id     INTEGER NOT NULL,
        store_id        INTEGER,
        store_name      TEXT NOT NULL,
        group_name      TEXT NOT NULL,
        boxes           INTEGER NOT NULL CHECK (boxes > 0),
        FOREIGN KEY (shipment_id) REFERENCES shipments(id) ON DELETE CASCADE,
        FOREIGN KEY (store_id)   REFERENCES stores(id)    ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id              SERIAL PRIMARY KEY,
        entity_type     TEXT NOT NULL,
        entity_id       INTEGER,
        action          TEXT NOT NULL,
        old_values      TEXT,
        new_values      TEXT,
        changed_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        changed_by      TEXT NOT NULL DEFAULT 'Local user'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shipments_date   ON shipments(shipment_date)",
    "CREATE INDEX IF NOT EXISTS idx_shipments_method ON shipments(shipment_method)",
    "CREATE INDEX IF NOT EXISTS idx_shipments_pallet ON shipments(pallet_id)",
    "CREATE INDEX IF NOT EXISTS idx_details_shipment ON shipment_details(shipment_id)",
    "CREATE INDEX IF NOT EXISTS idx_details_store    ON shipment_details(store_id)",
    "CREATE INDEX IF NOT EXISTS idx_details_group    ON shipment_details(group_name)",
]

DEFAULT_GROUPS = {
    "Christchurch Group": [
        "Christchurch CBD", "Colombo Sydenham", "Linwood", "New Brighton",
        "Kaipoi", "Edgeware", "Rangiora",
    ],
    "Dunedin Group": [
        "Dunedin Central", "Mosgiel", "South Dunedin",
    ],
    "Hornby Group": [
        "Papanui", "Hornby", "Riccarton",
    ],
    "Invercargill Group": [
        "Gore", "Invercargill",
    ],
    "Nelson Group": [
        "Motueka", "Nelson", "Richmond",
    ],
    "Timaru Group": [
        "Oamaru", "Timaru", "Temuka",
    ],
    "Blenheim Group": [
        "Blenheim", "Picton",
    ],
    # South Island standalone stores — each ships individually
    "Ashburton": ["Ashburton"],
    "Queenstown": ["Queenstown"],
    "Greymouth": ["Greymouth"],
    "Westport": ["Westport"],
    "Wellington Group": [
        "Lambton", "Lower Hutt Central", "Newtown", "Porirua",
        "Te Aro, Wellington City", "Upper Hutt", "Wainuiomata", "Tawa",
    ],
    "Palmy Group": [
        "Feilding", "Palmerston North",
    ],
    "Levin Group": [
        "Levin", "Paraparaumu", "Otaki",
    ],
    "Hawke's Bay Group": [
        "Hastings", "Napier South", "Taradale", "Waipukurau",
    ],
    "New Plymouth Group": [
        "New Plymouth", "Waitara", "Inglewood",
    ],
    "Tauranga Group": [
        "Greerton", "Bethlehem", "Mt Manganui", "Papamoa", "Tauranga", "Te Puke",
    ],
    "Hamilton Group": [
        "Te Rapa", "Cambridge", "Dinsdale", "Fairfield", "Grey St Hamilton East",
        "Hamilton CBD", "Melville", "Matamata", "Te Awamutu", "Morrinsville",
        "Te Kuiti", "Huntly", "Hillcrest", "Tokoroa",
    ],
    "Rotorua Group": [
        "Rotorua", "Redwoods",
    ],
    # North Island standalone stores — each ships individually
    "Whakatane": ["Whakatane"],
    "Whanganui": ["Whanganui"],
    "Kerikeri": ["Kerikeri"],
    "Gisborne": ["Gisborne"],
    "Hawera": ["Hawera"],
    "Kaitaia": ["Kaitaia"],
    "Masterton": ["Masterton"],
    "Paeroa": ["Paeroa"],
    "Wairoa": ["Wairoa"],
    "Thames": ["Thames"],
    "Whangarei": ["Whangarei"],
    "Dargaville": ["Dargaville"],
    "Taupo": ["Taupo"],
    "North Auckland": [
        "Albany", "Birkenhead", "Browns Bay", "Constellation", "Glenfield",
        "Northcote", "Silverdale", "Takapuna", "Wairau", "Warkworth", "Whangaparaoa",
    ],
    "West Auckland": [
        "Blockhouse Bay", "Dominion RD", "Glen Eden", "Lincoln Road", "Mangere",
        "New Lynn", "Onehunga Mall", "Point Chevalier", "Westgate", "Kumeu",
        "Kingsland", "Henderson", "Avondale",
    ],
    "South Auckland": [
        "East Tamaki", "Glen Innes", "Howick", "Manukau", "Mt Wellington",
        "Otahuhu", "Pakuranga", "Papakura", "Papatoetoe", "Takanini", "Hunter Plaza",
    ],
    "Auckland CBD": [
        "Hobson Street", "K Road", "Newmarket", "Quay Street", "Victoria Street",
    ],
}


def init_db() -> None:
    with connection() as conn:
        for stmt in _DDL:
            conn.execute(stmt)

        group_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM store_groups"
        ).fetchone()["cnt"]

        if group_count == 0:
            # Truly fresh database — seed default groups and stores
            for group_name, stores in DEFAULT_GROUPS.items():
                row = conn.execute(
                    "INSERT INTO store_groups (group_name) VALUES (?) RETURNING id",
                    (group_name,),
                ).fetchone()
                group_id = row["id"]
                conn.executemany(
                    "INSERT INTO stores (store_name, group_id, group_name) VALUES (?, ?, ?)",
                    [(store, group_id, group_name) for store in stores],
                )


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def audit(
    conn: _Conn,
    entity_type: str,
    entity_id: int | None,
    action: str,
    old_values: Any = None,
    new_values: Any = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log
            (entity_type, entity_id, action, old_values, new_values, changed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            entity_id,
            action,
            json.dumps(old_values, default=str) if old_values is not None else None,
            json.dumps(new_values, default=str) if new_values is not None else None,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
