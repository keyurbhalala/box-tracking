from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool
import streamlit as st
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


# ---------------------------------------------------------------------------
# Connection pool — created once per session, reused on every interaction
# ---------------------------------------------------------------------------

@st.cache_resource
def _pool() -> psycopg2.pool.ThreadedConnectionPool:
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        dsn=st.secrets["DATABASE_URL"],
    )


@st.cache_resource
def get_engine() -> Engine:
    """
    SQLAlchemy engine backed by the same DATABASE_URL.

    Used *only* by pd.read_sql_query() in services.py to avoid the
    pandas DeprecationWarning about passing a raw psycopg2 connection.
    All write operations still go through the psycopg2 pool above.
    """
    return create_engine(st.secrets["DATABASE_URL"])


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

# ---------------------------------------------------------------------------
# Schema migrations — safe to run on every startup (ADD COLUMN IF NOT EXISTS)
# ---------------------------------------------------------------------------

_DDL_MIGRATIONS = [
    # Store address fields (populated from address book CSV seed)
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS contact_name TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS phone        TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS email        TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS address_line1 TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS address_line2 TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS suburb       TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS city         TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS postcode     TEXT",
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS country      TEXT DEFAULT 'New Zealand'",
    # Mainfreight housebill sequence counter (range MAS00000000 – MAS99999999)
    "ALTER TABLE warehouse_settings ADD COLUMN IF NOT EXISTS next_mf_housebill_seq INTEGER NOT NULL DEFAULT 0",
]


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
    """
    CREATE TABLE IF NOT EXISTS delivery_signatures (
        id              SERIAL PRIMARY KEY,
        shipment_id     INTEGER NOT NULL,
        store_id        INTEGER,
        store_name      TEXT NOT NULL,
        boxes           INTEGER NOT NULL,
        signed_by       TEXT,
        signature_data  TEXT,
        signed_at       TEXT,
        FOREIGN KEY (shipment_id) REFERENCES shipments(id) ON DELETE CASCADE,
        FOREIGN KEY (store_id)   REFERENCES stores(id)    ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delsig_shipment ON delivery_signatures(shipment_id)",

    # ── Warehouse sender config ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS warehouse_settings (
        id              SERIAL PRIMARY KEY,
        warehouse_name  TEXT NOT NULL DEFAULT 'Shosha Warehouse',
        contact_name    TEXT NOT NULL DEFAULT 'Keyur',
        phone           TEXT DEFAULT '0220923220',
        email           TEXT DEFAULT 'keyur.bhalala@highgroup.nz',
        address_line1   TEXT DEFAULT '53 O''rorke Road',
        address_line2   TEXT DEFAULT '',
        suburb          TEXT DEFAULT 'Penrose',
        city            TEXT DEFAULT 'Auckland',
        postcode        TEXT DEFAULT '1061',
        country         TEXT DEFAULT 'NZ',
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,

    # ── Starshipit courier booking results (keyed by shipment + store) ────────
    # Separate table so tracking data survives shipment edits.
    """
    CREATE TABLE IF NOT EXISTS courier_bookings (
        id              SERIAL PRIMARY KEY,
        shipment_id     INTEGER NOT NULL,
        store_id        INTEGER,
        store_name      TEXT NOT NULL,
        boxes           INTEGER NOT NULL,
        weight_per_box  REAL,
        length          REAL,
        width           REAL,
        height          REAL,
        service_code    TEXT,
        tracking_number TEXT,
        label_url       TEXT,
        consignment_id  TEXT,
        carrier         TEXT,
        booking_status  TEXT NOT NULL DEFAULT 'Pending',
        booked_at       TEXT,
        api_response    TEXT,
        api_error       TEXT,
        retry_count     INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (shipment_id) REFERENCES shipments(id) ON DELETE CASCADE,
        FOREIGN KEY (store_id)   REFERENCES stores(id)    ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_courier_shipment ON courier_bookings(shipment_id)",
    "CREATE INDEX IF NOT EXISTS idx_courier_store    ON courier_bookings(store_id)",

    # ── Address Book — single source of truth for all delivery addresses ──────
    """
    CREATE TABLE IF NOT EXISTS address_book (
        id                  SERIAL PRIMARY KEY,
        company_name        TEXT NOT NULL,
        contact_name        TEXT,
        phone               TEXT,
        email               TEXT,
        code                TEXT,
        building            TEXT,
        street              TEXT,
        suburb              TEXT,
        city                TEXT,
        postcode            TEXT,
        state               TEXT,
        country             TEXT DEFAULT 'New Zealand',
        country_code        TEXT DEFAULT 'NZ',
        instructions        TEXT,
        carrier             TEXT,
        signature_required  INTEGER DEFAULT 1,
        imported_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ab_company ON address_book(LOWER(company_name))",

    # ── Store → Address Book mapping ──────────────────────────────────────────
    # Each store maps to exactly one Address Book company.
    # Courier bookings look up the delivery address via this table — never
    # store duplicate address data in shipment records.
    """
    CREATE TABLE IF NOT EXISTS store_address_mapping (
        id              SERIAL PRIMARY KEY,
        store_id        INTEGER NOT NULL UNIQUE,
        address_book_id INTEGER NOT NULL,
        mapped_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (store_id)        REFERENCES stores(id)       ON DELETE CASCADE,
        FOREIGN KEY (address_book_id) REFERENCES address_book(id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sam_store ON store_address_mapping(store_id)",

    # ── Pallet address book — separate from NZ Post address_book ─────────────
    # Sourced from Mainfreight receivers CSV.  Stores delivery addresses,
    # notification emails, and instructions for Mainfreight pallet bookings.
    """
    CREATE TABLE IF NOT EXISTS pallet_address_book (
        id                      SERIAL PRIMARY KEY,
        code                    TEXT NOT NULL UNIQUE,
        name                    TEXT NOT NULL,
        address1                TEXT NOT NULL,
        address2                TEXT,
        suburb                  TEXT,
        city                    TEXT NOT NULL,
        postcode                TEXT,
        state_code              TEXT,
        contact_name            TEXT,
        contact_phone           TEXT,
        contact_email           TEXT,
        delivery_instructions   TEXT,
        pickup_from_depot       INTEGER NOT NULL DEFAULT 0,
        notification_email1     TEXT,
        notification_events1    TEXT,
        notification_email2     TEXT,
        notification_events2    TEXT,
        imported_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pab_code ON pallet_address_book(code)",
    "CREATE INDEX IF NOT EXISTS idx_pab_name ON pallet_address_book(LOWER(name))",

    # ── Mainfreight pallet booking results ───────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS mainfreight_bookings (
        id                  SERIAL PRIMARY KEY,
        shipment_id         INTEGER,
        pallet_store_code   TEXT NOT NULL,
        pallet_store_name   TEXT NOT NULL,
        pallets             INTEGER NOT NULL,
        height_m            REAL NOT NULL,
        weight_per_pallet   REAL NOT NULL,
        use_loscam          INTEGER NOT NULL DEFAULT 0,
        housebill_number    TEXT NOT NULL UNIQUE,
        shipment_uuid       TEXT,
        consignment_number  TEXT,
        tracking_url        TEXT,
        booking_status      TEXT NOT NULL DEFAULT 'Pending',
        label_error         TEXT,
        api_response        TEXT,
        api_error           TEXT,
        pickup_date         TEXT,
        booked_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (shipment_id) REFERENCES shipments(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mfb_housebill   ON mainfreight_bookings(housebill_number)",
    "CREATE INDEX IF NOT EXISTS idx_mfb_consignment ON mainfreight_bookings(consignment_number)",
    "CREATE INDEX IF NOT EXISTS idx_mfb_store       ON mainfreight_bookings(pallet_store_code)",
    "CREATE INDEX IF NOT EXISTS idx_mfb_shipment    ON mainfreight_bookings(shipment_id)",
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


# ---------------------------------------------------------------------------
# Warehouse defaults (from AddressBook CSV — row 63)
# ---------------------------------------------------------------------------

_WAREHOUSE_DEFAULT = {
    "warehouse_name": "Shosha Warehouse",
    "contact_name":   "Keyur",
    "phone":          "0220923220",
    "email":          "keyur.bhalala@highgroup.nz",
    "address_line1":  "53 O'rorke Road",
    "address_line2":  "",
    "suburb":         "Penrose",
    "city":           "Auckland",
    "postcode":       "1061",
    "country":        "NZ",
}

# ---------------------------------------------------------------------------
# Store address seed — sourced from Starshipit AddressBook CSV.
# Key = store_name exactly as it appears in DEFAULT_GROUPS / the stores table.
# ---------------------------------------------------------------------------

_STORE_ADDRESS_SEED: dict[str, dict] = {
    # ── Invercargill Group ──────────────────────────────────────────────────
    "Gore": {
        "contact_name": "Campbell", "phone": "0277592425",
        "email": "Campbell.Hamlin@shosha.nz",
        "address_line1": "19 Hokonui Drive", "address_line2": "",
        "suburb": "Gore", "city": "Gore", "postcode": "9710",
    },
    "Invercargill": {
        "contact_name": "Campbell", "phone": "0277592425",
        "email": "campbell.hamlin@shosha.nz",
        "address_line1": "147 Dee Street", "address_line2": "",
        "suburb": "Invercargill", "city": "Invercargill", "postcode": "9810",
    },
    # ── Nelson Group ────────────────────────────────────────────────────────
    "Nelson": {
        "contact_name": "Arun", "phone": "02108045242",
        "email": "arun.chakkaravarthi@shosha.nz",
        "address_line1": "82 Bridge Street", "address_line2": "",
        "suburb": "Nelson", "city": "Nelson", "postcode": "7010",
    },
    "Richmond": {
        "contact_name": "Anthony", "phone": "02108040163",
        "email": "antony.ambrose@shosha.nz",
        "address_line1": "251 Queen Street", "address_line2": "",
        "suburb": "Richmond", "city": "Richmond", "postcode": "7020",
    },
    "Motueka": {
        "contact_name": "Rajvir", "phone": "02108053971",
        "email": "rajvir.singh@shosha.nz",
        "address_line1": "277 High Street", "address_line2": "",
        "suburb": "Motueka", "city": "Motueka", "postcode": "7120",
    },
    # ── Timaru Group ────────────────────────────────────────────────────────
    "Oamaru": {
        "contact_name": "Prudhvi", "phone": "221654271",
        "email": "prudhvi.namburi@shosha.nz",
        "address_line1": "205A Thames Street", "address_line2": "",
        "suburb": "Oamaru", "city": "Oamaru", "postcode": "9400",
    },
    "Timaru": {
        "contact_name": "Praveen", "phone": "221635030",
        "email": "praveen.paul@shosha.nz",
        "address_line1": "162 Stafford Street", "address_line2": "",
        "suburb": "Timaru", "city": "Timaru", "postcode": "7910",
    },
    "Temuka": {
        "contact_name": "Praveen", "phone": "221635030",
        "email": "praveen.paul@shosha.nz",
        "address_line1": "95 King Street", "address_line2": "",
        "suburb": "Temuka", "city": "Temuka", "postcode": "7920",
    },
    # ── West Coast standalones ───────────────────────────────────────────────
    "Greymouth": {
        "contact_name": "Venkatesh", "phone": "2102246232",
        "email": "venkatesh.naik@shosha.nz",
        "address_line1": "36 MacKay Street", "address_line2": "",
        "suburb": "Greymouth", "city": "Greymouth", "postcode": "7805",
    },
    "Westport": {
        "contact_name": "Rakesh", "phone": "2108053972",
        "email": "rakesh.kumar@shosha.nz",
        "address_line1": "184 Palmerston Street", "address_line2": "",
        "suburb": "Westport", "city": "Westport", "postcode": "7825",
    },
    # ── Hawke's Bay Group ────────────────────────────────────────────────────
    "Napier South": {
        "contact_name": "Blake", "phone": "221522919",
        "email": "blake.isherwood@shosha.nz",
        "address_line1": "58 Dickens Street", "address_line2": "",
        "suburb": "Napier South", "city": "Napier", "postcode": "4110",
    },
    "Hastings": {
        "contact_name": "Blake", "phone": "221522919",
        "email": "blake.isherwood@shosha.nz",
        "address_line1": "237 Heretaunga Street West", "address_line2": "",
        "suburb": "Hastings", "city": "Hastings", "postcode": "4122",
    },
    "Taradale": {
        "contact_name": "Blake", "phone": "221522919",
        "email": "blake.isherwood@shosha.nz",
        "address_line1": "251 Gloucester Street", "address_line2": "",
        "suburb": "Taradale", "city": "Napier", "postcode": "4112",
    },
    "Waipukurau": {
        "contact_name": "Blake", "phone": "221522919",
        "email": "blake.isherwood@shosha.nz",
        "address_line1": "76A Ruataniwha Street", "address_line2": "",
        "suburb": "Waipukurau", "city": "Waipukurau", "postcode": "4200",
    },
    # ── New Plymouth Group ───────────────────────────────────────────────────
    "New Plymouth": {
        "contact_name": "Prince", "phone": "64297777702",
        "email": "prince@shosha.nz",
        "address_line1": "53 Devon Street West", "address_line2": "",
        "suburb": "New Plymouth Central", "city": "New Plymouth", "postcode": "4310",
    },
    "Waitara": {
        "contact_name": "Prince", "phone": "64297777702",
        "email": "prince@shosha.nz",
        "address_line1": "48 McLean Street", "address_line2": "",
        "suburb": "Waitara", "city": "Waitara", "postcode": "4320",
    },
    "Inglewood": {
        "contact_name": "Prince", "phone": "64297777702",
        "email": "prince@shosha.nz",
        "address_line1": "42 Matai Street", "address_line2": "",
        "suburb": "Inglewood", "city": "Inglewood", "postcode": "4330",
    },
    # ── Hornby Group ─────────────────────────────────────────────────────────
    "Hornby": {
        "contact_name": "Gaurav", "phone": "64277767777",
        "email": "gaurav.sethi@shosha.nz",
        "address_line1": "413 Main South Road", "address_line2": "",
        "suburb": "Hornby", "city": "Christchurch", "postcode": "8042",
    },
    "Papanui": {
        "contact_name": "Gaurav", "phone": "64277767777",
        "email": "gaurav.sethi@shosha.nz",
        "address_line1": "18B Main North Road", "address_line2": "",
        "suburb": "Papanui", "city": "Christchurch", "postcode": "8053",
    },
    "Riccarton": {
        "contact_name": "Gaurav", "phone": "64277767777",
        "email": "gaurav.sethi@shosha.nz",
        "address_line1": "136 Riccarton Road", "address_line2": "",
        "suburb": "Riccarton", "city": "Christchurch", "postcode": "8041",
    },
    # ── Tauranga Group ───────────────────────────────────────────────────────
    "Tauranga": {
        "contact_name": "Pranav", "phone": "64276766767",
        "email": "pranav.malhotra@shosha.nz",
        "address_line1": "371 Cameron Road", "address_line2": "",
        "suburb": "Tauranga", "city": "Tauranga", "postcode": "3110",
    },
    "Papamoa": {
        "contact_name": "Pranav", "phone": "64276766767",
        "email": "pranav.malhotra@shosha.nz",
        "address_line1": "34 Gravatt Road", "address_line2": "4",
        "suburb": "Papamoa Beach", "city": "Papamoa", "postcode": "3118",
    },
    "Bethlehem": {
        "contact_name": "Pranav", "phone": "64276766767",
        "email": "pranav.malhotra@shosha.nz",
        "address_line1": "245 State Highway 2", "address_line2": "",
        "suburb": "Bethlehem", "city": "Tauranga", "postcode": "3110",
    },
    "Mt Manganui": {
        "contact_name": "Pranav", "phone": "64276766767",
        "email": "pranav.malhotra@shosha.nz",
        "address_line1": "194 Maunganui Road", "address_line2": "1",
        "suburb": "Mount Maunganui", "city": "Mount Maunganui", "postcode": "3116",
    },
    "Greerton": {
        "contact_name": "Pranav", "phone": "64276766767",
        "email": "pranav.malhotra@shosha.nz",
        "address_line1": "196 Chadwick Road", "address_line2": "3",
        "suburb": "Greerton", "city": "Tauranga", "postcode": "3112",
    },
    "Te Puke": {
        "contact_name": "Pranav", "phone": "64276766767",
        "email": "pranav.malhotra@shosha.nz",
        "address_line1": "27 Jellicoe Street", "address_line2": "",
        "suburb": "Te Puke", "city": "Te Puke", "postcode": "3119",
    },
    # ── Dunedin Group ────────────────────────────────────────────────────────
    "Dunedin Central": {
        "contact_name": "Madison Tait", "phone": "021579436",
        "email": "madison.t@shosha.nz",
        "address_line1": "14 Hanover Street", "address_line2": "",
        "suburb": "Central Dunedin", "city": "Dunedin", "postcode": "9016",
    },
    "South Dunedin": {
        "contact_name": "Madison Tait", "phone": "021579436",
        "email": "madison.t@shosha.nz",
        "address_line1": "197 King Edward Street", "address_line2": "",
        "suburb": "South Dunedin", "city": "Dunedin", "postcode": "9012",
    },
    "Mosgiel": {
        "contact_name": "Madison Tait", "phone": "021579436",
        "email": "madison.t@shosha.nz",
        "address_line1": "116 Gordon Road", "address_line2": "",
        "suburb": "Mosgiel", "city": "Mosgiel", "postcode": "9024",
    },
    # ── North Island standalones ─────────────────────────────────────────────
    "Wairoa": {
        "contact_name": "Rajan", "phone": "212481786",
        "email": "rajan.manchanda@shosha.nz",
        "address_line1": "88 Marine Parade", "address_line2": "",
        "suburb": "Wairoa", "city": "Wairoa", "postcode": "4108",
    },
    "Gisborne": {
        "contact_name": "Aayush", "phone": "223926182",
        "email": "aayush.verma@shosha.nz",
        "address_line1": "227 Gladstone Road", "address_line2": "",
        "suburb": "Gisborne", "city": "Gisborne", "postcode": "4010",
    },
    "Whakatane": {
        "contact_name": "Akshay Kumar", "phone": "64297777793",
        "email": "akshay.kumar@shosha.nz",
        "address_line1": "115 The Strand", "address_line2": "1",
        "suburb": "Whakatane", "city": "Whakatane", "postcode": "3120",
    },
    "Whanganui": {
        "contact_name": "Ashley Ryan", "phone": "64276492673",
        "email": "ashley.ryan@shosha.nz",
        "address_line1": "176 Victoria Avenue", "address_line2": "",
        "suburb": "Whanganui", "city": "Whanganui", "postcode": "4500",
    },
    "Hawera": {
        "contact_name": "Aleesha", "phone": "64279125246",
        "email": "aleesha.veale@shosha.nz",
        "address_line1": "92 High Street", "address_line2": "",
        "suburb": "Hawera", "city": "Hawera", "postcode": "4610",
    },
    "Masterton": {
        "contact_name": "Amit Semwal", "phone": "0221915355",
        "email": "Amit.Semwal@shosha.nz",
        "address_line1": "152 Queen Street", "address_line2": "",
        "suburb": "Masterton", "city": "Masterton", "postcode": "5810",
    },
    "Kaitaia": {
        "contact_name": "Kanwar Jeet", "phone": "",
        "email": "Kanwarjeet.Singh@shosha.nz",
        "address_line1": "47 Commerce Street", "address_line2": "",
        "suburb": "Kaitaia", "city": "Kaitaia", "postcode": "0410",
    },
    "Kerikeri": {
        "contact_name": "Manpreet Singh", "phone": "2102327601",
        "email": "manpreet.sandhu@shosha.nz",
        "address_line1": "2C Fairway Drive", "address_line2": "",
        "suburb": "Kerikeri", "city": "Kerikeri", "postcode": "0230",
    },
    "Paeroa": {
        "contact_name": "Daniel Taylor", "phone": "64225889976",
        "email": "daniel.taylor@shosha.nz",
        "address_line1": "114 Normanby Road", "address_line2": "",
        "suburb": "Paeroa", "city": "Paeroa", "postcode": "3600",
    },
    "Thames": {
        "contact_name": "Ethan Shane", "phone": "07 868 6865",
        "email": "ethan.brenchley@shosha.nz",
        "address_line1": "406 Pollen Street", "address_line2": "",
        "suburb": "Thames", "city": "Thames", "postcode": "3500",
    },
    "Whangarei": {
        "contact_name": "Kanwarjeet", "phone": "2109113083",
        "email": "kanwarjeet.singh@shosha.nz",
        "address_line1": "67 Bank Street", "address_line2": "",
        "suburb": "Whangarei", "city": "Whangarei", "postcode": "0110",
    },
    "Dargaville": {
        "contact_name": "Kanwar Jeet", "phone": "211837862",
        "email": "Kanwarjeet.Singh@shosha.nz",
        "address_line1": "52 Victoria Street", "address_line2": "",
        "suburb": "Dargaville", "city": "Dargaville", "postcode": "0310",
    },
    # ── Rotorua Group ────────────────────────────────────────────────────────
    "Rotorua": {
        "contact_name": "Rachel", "phone": "21578714",
        "email": "rachel.sila@shosha.nz",
        "address_line1": "1222 Fenton Street", "address_line2": "",
        "suburb": "Rotorua", "city": "Rotorua", "postcode": "3010",
    },
    "Redwoods": {
        "contact_name": "Rachel", "phone": "21578714",
        "email": "rachel.sila@shosha.nz",
        "address_line1": "9/5 Tarawera Road", "address_line2": "Redwood Shopping Centre",
        "suburb": "Lynmore", "city": "Rotorua", "postcode": "3010",
    },
    # ── Hamilton Group ───────────────────────────────────────────────────────
    "Taupo": {
        "contact_name": "Vipandeep", "phone": "64220803086",
        "email": "vipandeep.singh@shosha.nz",
        "address_line1": "10 Gascoigne Street", "address_line2": "",
        "suburb": "Taupo", "city": "Taupo", "postcode": "3330",
    },
    "Tokoroa": {
        "contact_name": "Vipandeep", "phone": "64220803086",
        "email": "vipandeep.singh@shosha.nz",
        "address_line1": "231 Leith Place", "address_line2": "",
        "suburb": "Tokoroa", "city": "Tokoroa", "postcode": "3420",
    },
    "Te Rapa": {
        "contact_name": "Chandrakanth", "phone": "6421958442",
        "email": "chandrakanth.tipireddy@shosha.nz",
        "address_line1": "757 Te Rapa Road", "address_line2": "",
        "suburb": "Te Rapa", "city": "Hamilton", "postcode": "3200",
    },
    "Te Kuiti": {
        "contact_name": "Chandrakanth", "phone": "6421958442",
        "email": "chandrakanth.tipireddy@shosha.nz",
        "address_line1": "75 Rora Street", "address_line2": "",
        "suburb": "Te Kuiti", "city": "Te Kuiti", "postcode": "3910",
    },
    "Cambridge": {
        "contact_name": "Mahendar Aleti", "phone": "0221673854",
        "email": "mahendar.aleti@shosha.nz",
        "address_line1": "8 Anzac Street", "address_line2": "",
        "suburb": "Leamington", "city": "Cambridge", "postcode": "3434",
    },
    "Dinsdale": {
        "contact_name": "Swathi Nimma", "phone": "64225418994",
        "email": "swathi.nimma@shosha.nz",
        "address_line1": "12 Whatawhata Road", "address_line2": "",
        "suburb": "Dinsdale", "city": "Hamilton", "postcode": "3204",
    },
    "Fairfield": {
        "contact_name": "Ethan Gideon", "phone": "64221748191",
        "email": "ethan.diamond@shosha.nz",
        "address_line1": "303F Clarkin Road", "address_line2": "",
        "suburb": "Fairfield", "city": "Hamilton", "postcode": "3214",
    },
    "Grey St Hamilton East": {
        "contact_name": "Mikey", "phone": "64225214002",
        "email": "mukesh.kumar@shosha.nz",
        "address_line1": "382 Grey Street", "address_line2": "",
        "suburb": "Hamilton East", "city": "Hamilton", "postcode": "3216",
    },
    "Hamilton CBD": {
        "contact_name": "Kanik Chawla", "phone": "",
        "email": "kanik.chawla@shosha.nz",
        "address_line1": "661 Victoria Street", "address_line2": "",
        "suburb": "Hamilton Central", "city": "Hamilton", "postcode": "3204",
    },
    "Melville": {
        "contact_name": "Gurcharan Singh", "phone": "64220449015",
        "email": "gurcharan.singh@shosha.nz",
        "address_line1": "29 Ohaupo Road", "address_line2": "",
        "suburb": "Melville", "city": "Hamilton", "postcode": "3206",
    },
    "Matamata": {
        "contact_name": "Samuel Garrett", "phone": "0225742939",
        "email": "samuel.garrett@shosha.nz",
        "address_line1": "56 Arawa Street", "address_line2": "",
        "suburb": "Matamata", "city": "Matamata", "postcode": "3400",
    },
    "Te Awamutu": {
        "contact_name": "Brooke Schwass", "phone": "07 214 2022",
        "email": "brooke.schwass@shosha.nz",
        "address_line1": "45 Alexandra Street", "address_line2": "",
        "suburb": "Te Awamutu", "city": "Te Awamutu", "postcode": "3800",
    },
    "Morrinsville": {
        "contact_name": "Daniel Williams", "phone": "07 214 3031",
        "email": "daniel.williams@shosha.nz",
        "address_line1": "186 Thames Street", "address_line2": "",
        "suburb": "Morrinsville", "city": "Morrinsville", "postcode": "3300",
    },
    "Huntly": {
        "contact_name": "Harley Garner", "phone": "0204901900",
        "email": "harley.garner@shosha.nz",
        "address_line1": "160 Main Street", "address_line2": "",
        "suburb": "Huntly", "city": "Huntly", "postcode": "3700",
    },
    "Hillcrest": {
        "contact_name": "Rebecca", "phone": "02102736533",
        "email": "rebecca.cooper@shosha.nz",
        "address_line1": "113 Cambridge Road", "address_line2": "",
        "suburb": "Hillcrest", "city": "Hamilton", "postcode": "3216",
    },
    # ── Palmy Group ──────────────────────────────────────────────────────────
    "Palmerston North": {
        "contact_name": "Gurucharan Mallak", "phone": "2108033290",
        "email": "gurucharan.mallak@shosha.nz",
        "address_line1": "65 Broadway Avenue", "address_line2": "",
        "suburb": "Palmerston North Central", "city": "Palmerston North", "postcode": "4410",
    },
    "Feilding": {
        "contact_name": "Gurucharan Mallak", "phone": "2108033290",
        "email": "gurucharan.mallak@shosha.nz",
        "address_line1": "27A Manchester Square", "address_line2": "",
        "suburb": "Feilding", "city": "Feilding", "postcode": "4702",
    },
    # ── Levin Group ──────────────────────────────────────────────────────────
    "Levin": {
        "contact_name": "Tyler", "phone": "64220474492",
        "email": "tylar.cardno@shosha.nz",
        "address_line1": "185A Oxford Street", "address_line2": "",
        "suburb": "Horowhenua", "city": "Levin", "postcode": "5510",
    },
    "Paraparaumu": {
        "contact_name": "Tyler", "phone": "64220474492",
        "email": "tylar.cardno@shosha.nz",
        "address_line1": "3B Ihakara Street", "address_line2": "",
        "suburb": "Paraparaumu", "city": "Paraparaumu", "postcode": "5032",
    },
    "Otaki": {
        "contact_name": "Tyler", "phone": "64220474492",
        "email": "tylar.cardno@shosha.nz",
        "address_line1": "220 Main Highway", "address_line2": "",
        "suburb": "Otaki", "city": "Otaki", "postcode": "5512",
    },
    # ── North Auckland Group ─────────────────────────────────────────────────
    "Albany": {
        "contact_name": "Prashanth", "phone": "64297777200",
        "email": "prashanth.arukonda@shosha.nz",
        "address_line1": "329 Albany Highway", "address_line2": "",
        "suburb": "Albany", "city": "Auckland", "postcode": "0632",
    },
    "Birkenhead": {
        "contact_name": "Naveen", "phone": "6421391448",
        "email": "naveen@highgroup.nz",
        "address_line1": "12 Birkenhead Avenue", "address_line2": "",
        "suburb": "Birkenhead", "city": "Auckland", "postcode": "0626",
    },
    "Takapuna": {
        "contact_name": "Naveen", "phone": "6421391448",
        "email": "naveen@highgroup.nz",
        "address_line1": "3/461 Lake Road", "address_line2": "",
        "suburb": "Takapuna", "city": "Auckland", "postcode": "0622",
    },
    "Northcote": {
        "contact_name": "Darshan", "phone": "64211301116",
        "email": "darshan.chauhan@shosha.nz",
        "address_line1": "13 Pearn Crescent", "address_line2": "",
        "suburb": "Northcote", "city": "Auckland", "postcode": "0627",
    },
    "Glenfield": {
        "contact_name": "Darshan", "phone": "64211301116",
        "email": "darshan.chauhan@shosha.nz",
        "address_line1": "403 Glenfield Road", "address_line2": "Beside Salvation Army Shop L1",
        "suburb": "Glenfield", "city": "Auckland", "postcode": "0629",
    },
    "Silverdale": {
        "contact_name": "Brooklyn Macdonald", "phone": "",
        "email": "brooklyn.macdonald@shosha.nz",
        "address_line1": "3 Silverdale Street", "address_line2": "",
        "suburb": "Stanmore Bay", "city": "Silverdale", "postcode": "0932",
    },
    "Warkworth": {
        "contact_name": "Sunny Gupta", "phone": "297777700",
        "email": "sunny.g@shosha.nz",
        "address_line1": "6 Neville Street", "address_line2": "",
        "suburb": "Warkworth", "city": "Warkworth", "postcode": "0910",
    },
    "Wairau": {
        "contact_name": "Prashanth", "phone": "64297777200",
        "email": "prashanth.arukonda@shosha.nz",
        "address_line1": "31F Link Drive", "address_line2": "",
        "suburb": "Wairau Valley", "city": "North Shore", "postcode": "0627",
    },
    "Whangaparaoa": {
        "contact_name": "Prashanth", "phone": "64297777200",
        "email": "prashanth.arukonda@shosha.nz",
        "address_line1": "15 Karepiro Drive", "address_line2": "",
        "suburb": "Stanmore Bay", "city": "Whangaparaoa", "postcode": "0932",
    },
    # ── Auckland CBD ─────────────────────────────────────────────────────────
    "Hobson Street": {
        "contact_name": "Amrinder", "phone": "02041690988",
        "email": "amrinder.singh@shosha.nz",
        "address_line1": "51 Hobson Street", "address_line2": "Vogel Lane",
        "suburb": "Auckland CBD", "city": "Auckland", "postcode": "1010",
    },
    "K Road": {
        "contact_name": "Amrinder", "phone": "02041690988",
        "email": "amrinder.singh@shosha.nz",
        "address_line1": "258A Karangahape Road", "address_line2": "",
        "suburb": "Auckland CBD", "city": "Auckland", "postcode": "1010",
    },
    "Victoria Street": {
        "contact_name": "Amrinder", "phone": "02041690988",
        "email": "amrinder.singh@shosha.nz",
        "address_line1": "29 Victoria Street East", "address_line2": "",
        "suburb": "Auckland CBD", "city": "Auckland", "postcode": "1010",
    },
    "Quay Street": {
        "contact_name": "Amrinder", "phone": "02041690988",
        "email": "amrinder.singh@shosha.nz",
        "address_line1": "8 Quay Street", "address_line2": "",
        "suburb": "Auckland CBD", "city": "Auckland", "postcode": "1010",
    },
    "Kingsland": {
        "contact_name": "Amrinder", "phone": "02041690988",
        "email": "amrinder.singh@shosha.nz",
        "address_line1": "479 New North Road", "address_line2": "",
        "suburb": "Kingsland", "city": "Auckland", "postcode": "1021",
    },
    "Newmarket": {
        "contact_name": "Chandrasekhar", "phone": "02102606174",
        "email": "chandrasekhar.eppala@shosha.nz",
        "address_line1": "128 Broadway", "address_line2": "",
        "suburb": "Newmarket", "city": "Auckland", "postcode": "1023",
    },
    # ── West Auckland Group ──────────────────────────────────────────────────
    "Blockhouse Bay": {
        "contact_name": "Karun Mittal", "phone": "0223456844",
        "email": "Karun.M@shosha.nz",
        "address_line1": "509 Blockhouse Bay Road", "address_line2": "",
        "suburb": "Blockhouse Bay", "city": "Auckland", "postcode": "0600",
    },
    "Glen Eden": {
        "contact_name": "Karun Mittal", "phone": "0223456844",
        "email": "Karun.M@shosha.nz",
        "address_line1": "204 West Coast Road", "address_line2": "",
        "suburb": "Glen Eden", "city": "Auckland", "postcode": "0602",
    },
    "Dominion RD": {
        "contact_name": "Gurinder", "phone": "6421674447",
        "email": "gurinder.singh@shosha.nz",
        "address_line1": "1242 Dominion Road", "address_line2": "",
        "suburb": "Mount Roskill", "city": "Auckland", "postcode": "1041",
    },
    "Westgate": {
        "contact_name": "Bharat Sagar", "phone": "02041498279",
        "email": "bharat.bolleboina@shosha.nz",
        "address_line1": "12 Westgate Drive", "address_line2": "",
        "suburb": "Massey", "city": "Auckland", "postcode": "0614",
    },
    "Lincoln Road": {
        "contact_name": "Bharat Sagar", "phone": "02041498279",
        "email": "bharat.bolleboina@shosha.nz",
        "address_line1": "254 Lincoln Road", "address_line2": "",
        "suburb": "Henderson", "city": "Auckland", "postcode": "0610",
    },
    "Onehunga Mall": {
        "contact_name": "Rupin Kishor", "phone": "6420412873 69",
        "email": "rupin.chadwa@shosha.nz",
        "address_line1": "139 Onehunga Mall", "address_line2": "",
        "suburb": "Onehunga", "city": "Auckland", "postcode": "1061",
    },
    "New Lynn": {
        "contact_name": "Rupin", "phone": "6420412873 69",
        "email": "rupin.chadwa@shosha.nz",
        "address_line1": "3136 Great North Road", "address_line2": "",
        "suburb": "New Lynn", "city": "Auckland", "postcode": "0600",
    },
    "Point Chevalier": {
        "contact_name": "Harsh Rupesh", "phone": "64224982513",
        "email": "harsh.rupesh@shosha.nz",
        "address_line1": "1104C Great North Road", "address_line2": "",
        "suburb": "Point Chevalier", "city": "Auckland", "postcode": "1022",
    },
    "Avondale": {
        "contact_name": "Hope Foliola", "phone": "284029927",
        "email": "hope.foliola@shosha.nz",
        "address_line1": "1784 Great North Road", "address_line2": "4A",
        "suburb": "Avondale", "city": "Auckland", "postcode": "1026",
    },
    "Henderson": {
        "contact_name": "Manikandan Kaliraj", "phone": "2108051946",
        "email": "manikandan.kaliraj@shosha.nz",
        "address_line1": "2/330 Great North Road", "address_line2": "",
        "suburb": "Henderson", "city": "Auckland", "postcode": "0612",
    },
    "Kumeu": {
        "contact_name": "Charitha", "phone": "6421319447",
        "email": "charitha.wijenayaka@shosha.nz",
        "address_line1": "78 Main Road", "address_line2": "",
        "suburb": "Kumeu", "city": "Auckland", "postcode": "0810",
    },
    # ── South Auckland Group ─────────────────────────────────────────────────
    "Papatoetoe": {
        "contact_name": "Mahendra", "phone": "6421461449",
        "email": "mahendra.rawat@shosha.nz",
        "address_line1": "9 Saint George Street", "address_line2": "",
        "suburb": "Papatoetoe", "city": "Auckland", "postcode": "2025",
    },
    "Hunter Plaza": {
        "contact_name": "Mahendra", "phone": "6421461449",
        "email": "mahendra.rawat@shosha.nz",
        "address_line1": "225 Great South Road", "address_line2": "",
        "suburb": "Papatoetoe", "city": "Auckland", "postcode": "2025",
    },
    "Otahuhu": {
        "contact_name": "Mahendra", "phone": "6421461449",
        "email": "mahendra.rawat@shosha.nz",
        "address_line1": "457 Great South Road", "address_line2": "",
        "suburb": "Otahuhu", "city": "Auckland", "postcode": "1062",
    },
    "Takanini": {
        "contact_name": "Sweety Gupta", "phone": "64226908442",
        "email": "sweety.gupta@shosha.nz",
        "address_line1": "108 Great South Road", "address_line2": "Unit 16",
        "suburb": "Takanini", "city": "Auckland", "postcode": "2112",
    },
    "Manukau": {
        "contact_name": "Deepak", "phone": "64297777782",
        "email": "Deepak.Goyal@shosha.nz",
        "address_line1": "726 Great South Road", "address_line2": "Shop 3",
        "suburb": "Manukau", "city": "Auckland", "postcode": "2104",
    },
    "Mt Wellington": {
        "contact_name": "Amrit Maan", "phone": "021398448",
        "email": "amrit.maan@shosha.nz",
        "address_line1": "284 Penrose Road", "address_line2": "",
        "suburb": "Mount Wellington", "city": "Auckland", "postcode": "1060",
    },
    "Howick": {
        "contact_name": "Tien Van Pham", "phone": "64212335245",
        "email": "tien.pham@shosha.nz",
        "address_line1": "90B Whitford Road", "address_line2": "",
        "suburb": "Somerville", "city": "Auckland", "postcode": "2014",
    },
    "Pakuranga": {
        "contact_name": "Adnan", "phone": "64297777795",
        "email": "omer.mohammed@shosha.nz",
        "address_line1": "121 Pakuranga Road", "address_line2": "Pakuranga Plaza",
        "suburb": "Pakuranga", "city": "Auckland", "postcode": "2010",
    },
    # ── Christchurch Group ───────────────────────────────────────────────────
    "Christchurch CBD": {
        "contact_name": "Bharat Maddi", "phone": "64297777706",
        "email": "bharat.maddi@shosha.nz",
        "address_line1": "227B High Street", "address_line2": "",
        "suburb": "Christchurch Central City", "city": "Christchurch", "postcode": "8011",
    },
    "Colombo Sydenham": {
        "contact_name": "Bharat Maddi", "phone": "64297777706",
        "email": "bharat.maddi@shosha.nz",
        "address_line1": "429 Colombo Street", "address_line2": "",
        "suburb": "Sydenham", "city": "Christchurch", "postcode": "8023",
    },
    "Linwood": {
        "contact_name": "Atul Berry", "phone": "64297777003",
        "email": "atul.berry@shosha.nz",
        "address_line1": "1/9 Buckleys Road", "address_line2": "",
        "suburb": "Linwood", "city": "Christchurch", "postcode": "8062",
    },
    "New Brighton": {
        "contact_name": "Atul Berry", "phone": "64297777003",
        "email": "atul.berry@shosha.nz",
        "address_line1": "4/140 Brighton Mall", "address_line2": "",
        "suburb": "New Brighton", "city": "Christchurch", "postcode": "8061",
    },
    "Kaipoi": {
        "contact_name": "Atul Berry", "phone": "64297777003",
        "email": "atul.berry@shosha.nz",
        "address_line1": "115 Williams Street", "address_line2": "",
        "suburb": "Kaiapoi", "city": "Kaiapoi", "postcode": "7630",
    },
    "Edgeware": {
        "contact_name": "Atul Berry", "phone": "64297777003",
        "email": "atul.berry@shosha.nz",
        "address_line1": "64 North Avon Road", "address_line2": "",
        "suburb": "Richmond", "city": "Christchurch", "postcode": "8013",
    },
    # ── Blenheim Group ───────────────────────────────────────────────────────
    "Blenheim": {
        "contact_name": "Avinash Gorla", "phone": "02102254664",
        "email": "avinash.gorla@shosha.nz",
        "address_line1": "17 Queen Street", "address_line2": "",
        "suburb": "Blenheim Central", "city": "Marlborough", "postcode": "7201",
    },
    "Picton": {
        "contact_name": "Ankur", "phone": "0211251800",
        "email": "ankur@shosha.nz",
        "address_line1": "8 High Street", "address_line2": "",
        "suburb": "Picton", "city": "Picton", "postcode": "7220",
    },
}


# ---------------------------------------------------------------------------
# Pallet address book seed — sourced from Mainfreight receivers CSV
# 78 stores  (excludes sender HighGroup code=113 and depot code=6)
# ---------------------------------------------------------------------------

_PALLET_ADDRESS_SEED = [
    # Code 3: Shosha Whakatane
    {
        'code': '3',
        'name': 'Shosha Whakatane',
        'address1': '1/115 The Strand',
        'address2': 'UNIT 1',
        'suburb': '',
        'city': 'WHAKATANE',
        'postcode': '3120',
        'state_code': 'NI',
        'contact_name': 'Akshay',
        'contact_phone': '0297777793',
        'contact_email': 'whakatane@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'whakatane@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'akshay.kumar@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 4: Shosha Whanganui
    {
        'code': '4',
        'name': 'Shosha Whanganui',
        'address1': '176 Victoria Avenue',
        'address2': '',
        'suburb': '',
        'city': 'WHANGANUI',
        'postcode': '4500',
        'state_code': 'NI',
        'contact_name': 'Aleesha',
        'contact_phone': '+64279125246',
        'contact_email': 'whanganui@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'whanganui@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'aleesha.veale@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 5: Shosha Hawera
    {
        'code': '5',
        'name': 'Shosha Hawera',
        'address1': '92 High Street',
        'address2': '',
        'suburb': '',
        'city': 'HAWERA',
        'postcode': '4610',
        'state_code': 'NI',
        'contact_name': 'Aleesha',
        'contact_phone': '+64279125246',
        'contact_email': 'hawera@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'hawera@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'aleesha.veale@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 10: Shosha Nelson
    {
        'code': '10',
        'name': 'Shosha Nelson',
        'address1': '82 Bridge Street',
        'address2': '',
        'suburb': 'NELSON CENTRAL',
        'city': 'NELSON',
        'postcode': '7010',
        'state_code': 'SI',
        'contact_name': 'Rajvir Singh',
        'contact_phone': '02108053971',
        'contact_email': 'nelson@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'nelson@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'rajvir.singh@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 11: Shosha Richmond
    {
        'code': '11',
        'name': 'Shosha Richmond',
        'address1': '251 Queen Street',
        'address2': '',
        'suburb': '',
        'city': 'RICHMOND',
        'postcode': '7020',
        'state_code': 'SI',
        'contact_name': 'Jessica',
        'contact_phone': '0273797930',
        'contact_email': 'richmond@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'richmond@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 12: Shosha Motueka
    {
        'code': '12',
        'name': 'Shosha Motueka',
        'address1': '277 High street',
        'address2': '',
        'suburb': '',
        'city': 'MOTUEKA',
        'postcode': '7120',
        'state_code': 'SI',
        'contact_name': 'Jessica',
        'contact_phone': '0297777991',
        'contact_email': 'motueka@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'motueka@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 13: Shosha Lambton Quay
    {
        'code': '13',
        'name': 'Shosha Lambton Quay',
        'address1': '120A Lambton Quay',
        'address2': '',
        'suburb': 'WELLINGTON CENTRAL',
        'city': 'WELLINGTON',
        'postcode': '6011',
        'state_code': 'NI',
        'contact_name': 'Rohit',
        'contact_phone': '0277666677',
        'contact_email': 'lambtonquay@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'lambtonquay@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 14: Shosha Upper Hutt
    {
        'code': '14',
        'name': 'Shosha Upper Hutt',
        'address1': '103 Main Street',
        'address2': '',
        'suburb': '',
        'city': 'UPPER HUTT',
        'postcode': '5018',
        'state_code': 'NI',
        'contact_name': 'Rohit',
        'contact_phone': '0277666677',
        'contact_email': 'upperhutt@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'upperhutt@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 15: Shosha Timaru
    {
        'code': '15',
        'name': 'Shosha Timaru',
        'address1': '162 Stafford Street',
        'address2': '',
        'suburb': '',
        'city': 'TIMARU',
        'postcode': '7910',
        'state_code': 'SI',
        'contact_name': 'Praveen Kumar Paul',
        'contact_phone': '0221635030',
        'contact_email': 'timaru@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'timaru@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'praveen.paul@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 16: Shosha Oamaru
    {
        'code': '16',
        'name': 'Shosha Oamaru',
        'address1': '205A Thames Street',
        'address2': '',
        'suburb': '',
        'city': 'OAMARU',
        'postcode': '9400',
        'state_code': 'SI',
        'contact_name': 'Abhishek',
        'contact_phone': '0297777990',
        'contact_email': 'oamaru@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'oamaru@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 17: Shosha Taupo
    {
        'code': '17',
        'name': 'Shosha Taupo',
        'address1': '10 Gascoigne Street',
        'address2': '',
        'suburb': '',
        'city': 'TAUPO',
        'postcode': '3377',
        'state_code': 'NI',
        'contact_name': 'Vipan',
        'contact_phone': '0220803086',
        'contact_email': 'taupo@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'taupo@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 18: Shosha Invercargill
    {
        'code': '18',
        'name': 'Shosha Invercargill',
        'address1': '147 Dee Street',
        'address2': '',
        'suburb': 'INVERCARGILL CENTRAL',
        'city': 'INVERCARGILL',
        'postcode': '9810',
        'state_code': 'SI',
        'contact_name': 'Campbell',
        'contact_phone': '0277592425',
        'contact_email': 'invercargill@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'invercargill@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 19: Shosha Gore
    {
        'code': '19',
        'name': 'Shosha Gore',
        'address1': '19 Hokonui Drive',
        'address2': '',
        'suburb': '',
        'city': 'GORE',
        'postcode': '9710',
        'state_code': 'SI',
        'contact_name': 'Gautam',
        'contact_phone': '0297777792',
        'contact_email': 'gore@shosha.nn',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'gore@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 20: Shosha New Brighton
    {
        'code': '20',
        'name': 'Shosha New Brighton',
        'address1': '4/140 Brighton Mall',
        'address2': 'UNIT 4',
        'suburb': 'NEW BRIGHTON',
        'city': 'CHRISTCHURCH',
        'postcode': '8061',
        'state_code': 'SI',
        'contact_name': 'Mohit',
        'contact_phone': '0276767767',
        'contact_email': 'newbrighton@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'newbrighton@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 21: Shosha Christchurch (Colombo St)
    {
        'code': '21',
        'name': 'Shosha Christchurch',
        'address1': '429 Colombo Street',
        'address2': '',
        'suburb': 'SYDENHAM',
        'city': 'CHRISTCHURCH',
        'postcode': '8023',
        'state_code': 'SI',
        'contact_name': 'Atul',
        'contact_phone': '0297777003',
        'contact_email': 'christchurchcity@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'colombost@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 22: Shosha Queenstown
    {
        'code': '22',
        'name': 'Shosha Queenstown',
        'address1': '32 Red Oaks Drive',
        'address2': 'Wyndham Gardens Hotel',
        'suburb': 'FRANKTON',
        'city': 'QUEENSTOWN',
        'postcode': '9300',
        'state_code': 'SI',
        'contact_name': 'Sankeerth Kamana',
        'contact_phone': '02108051947',
        'contact_email': '',
        'delivery_instructions': 'SHOP 7, GROUND FLOOR. Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'sankeerth.kamana@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 23: Shosha Palmerston North
    {
        'code': '23',
        'name': 'Shosha Palmerston North',
        'address1': '65 Broadway Avenue',
        'address2': '',
        'suburb': 'PALMERSTON NORTH CENTRAL',
        'city': 'PALMERSTON NORTH',
        'postcode': '4410',
        'state_code': 'NI',
        'contact_name': 'Neeraj Kumar',
        'contact_phone': '02108040167',
        'contact_email': 'palmy@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'palmy@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'neeraj.kumar@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 24: Shosha Feilding
    {
        'code': '24',
        'name': 'Shosha Feilding',
        'address1': '25 Manchester Square',
        'address2': '',
        'suburb': '',
        'city': 'FEILDING',
        'postcode': '4702',
        'state_code': 'NI',
        'contact_name': 'Ankith',
        'contact_phone': '0297777708',
        'contact_email': 'fielding@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'fielding@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 25: Shosha Morrinsville
    {
        'code': '25',
        'name': 'Shosha Morrinsville',
        'address1': '186 Thames Street',
        'address2': '',
        'suburb': '',
        'city': 'MORRINSVILLE',
        'postcode': '3300',
        'state_code': 'NI',
        'contact_name': 'Paul',
        'contact_phone': '0279176066',
        'contact_email': '',
        'delivery_instructions': 'Tail lift/pallet jack required. No forklift on site.',
        'pickup_from_depot': False,
        'notification_email1': 'morrinsville@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 26: Shosha Huntly
    {
        'code': '26',
        'name': 'Shosha Huntly',
        'address1': '160 Main Street',
        'address2': '',
        'suburb': '',
        'city': 'HUNTLY',
        'postcode': '3700',
        'state_code': 'NI',
        'contact_name': 'Chandu',
        'contact_phone': '021958442',
        'contact_email': '',
        'delivery_instructions': 'Tail lift required, no pallet jack/forklift on site',
        'pickup_from_depot': False,
        'notification_email1': 'huntly@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 28: Shosha Wellington City
    {
        'code': '28',
        'name': 'Shosha Wellington City',
        'address1': '119 Manners street',
        'address2': '',
        'suburb': 'TE ARO',
        'city': 'WELLINGTON',
        'postcode': '6011',
        'state_code': 'NI',
        'contact_name': 'Rizwan',
        'contact_phone': '0277666677',
        'contact_email': 'wellingtoncity@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'wellingtoncity@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 29: Shosha Thames
    {
        'code': '29',
        'name': 'Shosha Thames',
        'address1': '406 Pollen Street',
        'address2': '',
        'suburb': '',
        'city': 'THAMES',
        'postcode': '3500',
        'state_code': 'NI',
        'contact_name': 'Daniel Taylor',
        'contact_phone': '64225889976',
        'contact_email': 'thames@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'thames@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'daniel.taylor@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 30: Shosha Paeroa
    {
        'code': '30',
        'name': 'Shosha Paeroa',
        'address1': '114 Normanby Road',
        'address2': '',
        'suburb': '',
        'city': 'PAEROA',
        'postcode': '3600',
        'state_code': 'NI',
        'contact_name': 'Paul',
        'contact_phone': '0279176066',
        'contact_email': 'paeroa@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'paeroa@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 31: Shosha Taranaki (New Plymouth)
    {
        'code': '31',
        'name': 'Shosha Taranaki (New Plymouth)',
        'address1': '53 Devon Street West',
        'address2': '',
        'suburb': 'NEW PLYMOUTH CENTRAL',
        'city': 'NEW PLYMOUTH',
        'postcode': '4310',
        'state_code': 'NI',
        'contact_name': 'Prince',
        'contact_phone': '0297777702',
        'contact_email': 'newplymouth@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'newplymouth@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 32: Shosha Whangarei
    {
        'code': '32',
        'name': 'Shosha Whangarei',
        'address1': '67 Bank street',
        'address2': '',
        'suburb': '',
        'city': 'WHANGAREI',
        'postcode': '',
        'state_code': 'NI',
        'contact_name': 'Kanwarjeet Singh',
        'contact_phone': '02109113083',
        'contact_email': 'whangarei@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'Shoshawhangarei@gmail.com',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'kanwarjeet.singh@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 33: Shosha Kerikeri
    {
        'code': '33',
        'name': 'Shosha Kerikeri',
        'address1': '2C Fairway Drive',
        'address2': '',
        'suburb': '',
        'city': 'KERIKERI',
        'postcode': '',
        'state_code': 'NI',
        'contact_name': 'Manpreet Singh',
        'contact_phone': '02102327601',
        'contact_email': 'kerikeri@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'kerikeri@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'manpreet.sandhu@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 34: Shosha Porirua
    {
        'code': '34',
        'name': 'Shosha Porirua',
        'address1': '2B Lyttelton Avenue',
        'address2': '',
        'suburb': '',
        'city': 'PORIRUA',
        'postcode': '5022',
        'state_code': 'NI',
        'contact_name': 'Simran',
        'contact_phone': '0297777796',
        'contact_email': 'porirua@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'porirua@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 35: Shosha Newtown
    {
        'code': '35',
        'name': 'Shosha Newtown',
        'address1': '171 Riddiford street',
        'address2': '',
        'suburb': 'NEWTOWN',
        'city': 'WELLINGTON',
        'postcode': '6021',
        'state_code': 'NI',
        'contact_name': 'Himanshu',
        'contact_phone': '0297777796',
        'contact_email': 'newtown@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'newtown@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 36: Shosha Tauranga
    {
        'code': '36',
        'name': 'Shosha Tauranga',
        'address1': '371 Cameron Road',
        'address2': '',
        'suburb': 'TAURANGA CENTRAL',
        'city': 'TAURANGA',
        'postcode': '',
        'state_code': 'NI',
        'contact_name': 'Paul',
        'contact_phone': '0279176066',
        'contact_email': 'tauranga@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'tauranga@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 37: Shosha Papamoa
    {
        'code': '37',
        'name': 'Shosha Papamoa',
        'address1': '4/34 Gravatt road',
        'address2': 'UNIT 4',
        'suburb': '',
        'city': 'PAPAMOA',
        'postcode': '3118',
        'state_code': 'NI',
        'contact_name': 'Abinash',
        'contact_phone': '0276766766',
        'contact_email': 'papamoa@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'papamoa@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 38: Shosha Bethlehem
    {
        'code': '38',
        'name': 'Shosha Bethlehem',
        'address1': '245 State Highway 2',
        'address2': '',
        'suburb': '',
        'city': 'BETHLEHEM',
        'postcode': '',
        'state_code': 'NI',
        'contact_name': 'Jaipal',
        'contact_phone': '0276766766',
        'contact_email': 'bethlehem@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'bethlehem@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 39: Shosha Mt Maunganui
    {
        'code': '39',
        'name': 'Shosha Mt Maunganui',
        'address1': '1/194 Maunganui Road',
        'address2': 'UNIT 1',
        'suburb': '',
        'city': 'MT MAUNGANUI',
        'postcode': '3116',
        'state_code': 'NI',
        'contact_name': 'Paul',
        'contact_phone': '0279176066',
        'contact_email': 'mountmaunganui@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'mountmaunganui@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 40: Shosha Rotorua
    {
        'code': '40',
        'name': 'Shosha Rotorua',
        'address1': '1222 Fenton St',
        'address2': '',
        'suburb': '',
        'city': 'ROTORUA',
        'postcode': '3010',
        'state_code': 'NI',
        'contact_name': 'Rachel',
        'contact_phone': '021578714',
        'contact_email': 'rotorua@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'rotorua@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'rachel.sila@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 41: Shosha Dinsdale
    {
        'code': '41',
        'name': 'Shosha Dinsdale',
        'address1': '12 Whatawhata road',
        'address2': '',
        'suburb': 'DINSDALE',
        'city': 'HAMILTON',
        'postcode': '3204',
        'state_code': 'NI',
        'contact_name': 'Chandu',
        'contact_phone': '021958442',
        'contact_email': 'dinsdale@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'dinsdale@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 42: Shosha Te Rapa
    {
        'code': '42',
        'name': 'Shosha Te Rapa',
        'address1': '757 Te Rapa Road',
        'address2': '',
        'suburb': 'TE RAPA',
        'city': 'HAMILTON',
        'postcode': '3200',
        'state_code': 'NI',
        'contact_name': 'Chandu',
        'contact_phone': '021958442',
        'contact_email': 'terapa@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'terapa@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'chandrakanth.tipireddy@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 43: Shosha Papanui
    {
        'code': '43',
        'name': 'Shosha Papanui',
        'address1': '18B Main North Road',
        'address2': '',
        'suburb': 'PAPANUI',
        'city': 'CHRISTCHURCH',
        'postcode': '8053',
        'state_code': 'SI',
        'contact_name': 'Gaurav',
        'contact_phone': '0277767777',
        'contact_email': 'papanui@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'papanui@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 44: Shosha Hornby (DEPOT COLLECT)
    {
        'code': '44',
        'name': 'Shosha Hornby',
        'address1': '38 McAlpine Street',
        'address2': '',
        'suburb': 'WIGRAM',
        'city': 'CHRISTCHURCH',
        'postcode': '8042',
        'state_code': 'SI',
        'contact_name': 'Gaurav',
        'contact_phone': '0277767777',
        'contact_email': 'hornby@shosha.nz',
        'delivery_instructions': 'Depot collect from Daily Freight Christchurch.',
        'pickup_from_depot': True,
        'notification_email1': 'hornby@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 45: Shosha Ashburton
    {
        'code': '45',
        'name': 'Shosha Ashburton',
        'address1': '519 East Street',
        'address2': '519B',
        'suburb': '',
        'city': 'ASHBURTON',
        'postcode': '7700',
        'state_code': 'SI',
        'contact_name': 'Bhupinder Singh',
        'contact_phone': '64225623613',
        'contact_email': 'ashburton@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required. 519B East Street.',
        'pickup_from_depot': False,
        'notification_email1': 'ashburton@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'bhupinder.singh@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 46: Shosha Riccarton
    {
        'code': '46',
        'name': 'Shosha Riccarton',
        'address1': '136 Riccarton road',
        'address2': '',
        'suburb': 'RICCARTON',
        'city': 'CHRISTCHURCH',
        'postcode': '8041',
        'state_code': 'SI',
        'contact_name': 'Sophie',
        'contact_phone': '0277767777',
        'contact_email': 'riccarton@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'riccarton@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 54: Shosha Lower Hutt
    {
        'code': '54',
        'name': 'Shosha Lower Hutt',
        'address1': '243 High St',
        'address2': '',
        'suburb': 'HUTT CENTRAL',
        'city': 'LOWER HUTT',
        'postcode': '5010',
        'state_code': 'NI',
        'contact_name': 'Mohit Kakkar',
        'contact_phone': '+64276767767',
        'contact_email': 'lowerhutt@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'lowerhutt@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'rohit.khosla@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 59: Shosha Dunedin
    {
        'code': '59',
        'name': 'Shosha Dunedin',
        'address1': '14 Hanover Street',
        'address2': '',
        'suburb': 'DUNEDIN CENTRAL',
        'city': 'DUNEDIN',
        'postcode': '9016',
        'state_code': 'SI',
        'contact_name': 'Bhupinder Singh',
        'contact_phone': '0297777006',
        'contact_email': 'dunedin@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'dunedin@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'bhupinder.singh@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 60: Shosha Dunedin South
    {
        'code': '60',
        'name': 'Shosha Dunedin South',
        'address1': '197 King Edward Street',
        'address2': '',
        'suburb': 'SOUTH DUNEDIN',
        'city': 'DUNEDIN',
        'postcode': '9012',
        'state_code': 'SI',
        'contact_name': 'Bharath',
        'contact_phone': '0297777706',
        'contact_email': 'southdunedin@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'southdunedin@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 61: Shosha Mosgiel
    {
        'code': '61',
        'name': 'Shosha Mosgiel',
        'address1': '116 Gordon Road',
        'address2': '',
        'suburb': '',
        'city': 'MOSGIEL',
        'postcode': '9024',
        'state_code': 'SI',
        'contact_name': 'Bharath',
        'contact_phone': '0297777706',
        'contact_email': 'mosgiel@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'mosgiel@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 64: Shosha Napier
    {
        'code': '64',
        'name': 'Shosha Napier',
        'address1': '58 Dickens Street',
        'address2': '',
        'suburb': 'NAPIER SOUTH',
        'city': 'NAPIER',
        'postcode': '4110',
        'state_code': 'NI',
        'contact_name': 'Blake',
        'contact_phone': '64221522919',
        'contact_email': 'napier@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'napier@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'blake.isherwood@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 65: Shosha Taradale
    {
        'code': '65',
        'name': 'Shosha Taradale',
        'address1': '251 Gloucester Street',
        'address2': '',
        'suburb': 'TARADALE',
        'city': 'NAPIER',
        'postcode': '4112',
        'state_code': 'NI',
        'contact_name': 'Devon',
        'contact_phone': '0276766767',
        'contact_email': 'taradale@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'taradale@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 66: Shosha Hastings
    {
        'code': '66',
        'name': 'Shosha Hastings',
        'address1': '237 Heretaunga Street West',
        'address2': '',
        'suburb': '',
        'city': 'HASTINGS',
        'postcode': '4122',
        'state_code': 'NI',
        'contact_name': 'Arvinder',
        'contact_phone': '0279797979',
        'contact_email': 'hastings@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'hastings@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 67: Shosha Waipukurau (duplicate code 1 also exists — keep 67 as primary)
    {
        'code': '67',
        'name': 'Shosha Waipukurau',
        'address1': '76A Ruataniwha Street',
        'address2': '',
        'suburb': '',
        'city': 'WAIPUKURAU',
        'postcode': '4200',
        'state_code': 'NI',
        'contact_name': '',
        'contact_phone': '',
        'contact_email': 'waipukurau@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'waipukurau@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 71: Shosha Hamilton CBD
    {
        'code': '71',
        'name': 'Shosha Hamilton CBD',
        'address1': '661 Victoria Street',
        'address2': '',
        'suburb': 'HAMILTON CENTRAL',
        'city': 'HAMILTON',
        'postcode': '3204',
        'state_code': 'NI',
        'contact_name': 'Ali',
        'contact_phone': '0277878878',
        'contact_email': 'hamilton@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'hamilton@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 72: Shosha Grey Street
    {
        'code': '72',
        'name': 'Shosha Grey Street',
        'address1': '382 Grey Street',
        'address2': '',
        'suburb': 'HAMILTON EAST',
        'city': 'HAMILTON',
        'postcode': '3216',
        'state_code': 'NI',
        'contact_name': 'Kanik',
        'contact_phone': '0277878878',
        'contact_email': 'hamiltoneast@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'hamiltoneast@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 73: Shosha Cambridge
    {
        'code': '73',
        'name': 'Shosha Cambridge',
        'address1': '8 Anzac street',
        'address2': '',
        'suburb': '',
        'city': 'CAMBRIDGE',
        'postcode': '3434',
        'state_code': 'NI',
        'contact_name': 'Ali',
        'contact_phone': '0277878878',
        'contact_email': 'cambridge@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'cambridge@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 74: Shosha Melville
    {
        'code': '74',
        'name': 'Shosha Melville',
        'address1': '29 Ohaupo Road',
        'address2': '',
        'suburb': 'MELVILLE',
        'city': 'HAMILTON',
        'postcode': '3206',
        'state_code': 'NI',
        'contact_name': 'Ali',
        'contact_phone': '0277878878',
        'contact_email': 'baderstreet@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'baderstreet@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 77: Shosha Gisborne
    {
        'code': '77',
        'name': 'Shosha Gisborne',
        'address1': '227 Gladstone Road',
        'address2': '',
        'suburb': '',
        'city': 'GISBORNE',
        'postcode': '4010',
        'state_code': 'NI',
        'contact_name': 'Aayush Verma',
        'contact_phone': '0223926182',
        'contact_email': 'gisborne@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'gisborne@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'aayush.verma@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 78: Shosha Blenheim
    {
        'code': '78',
        'name': 'Shosha Blenheim',
        'address1': '17 Queen Street',
        'address2': '',
        'suburb': 'BLENHEIM CENTRAL',
        'city': 'BLENHEIM',
        'postcode': '7201',
        'state_code': 'SI',
        'contact_name': 'Avinash Gorla',
        'contact_phone': '02102254664',
        'contact_email': 'Blenheim@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'Blenheim@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'avinash.gorla@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 79: Shosha Levin
    {
        'code': '79',
        'name': 'Shosha Levin',
        'address1': '185 Oxford Street',
        'address2': '',
        'suburb': 'Horowhenua',
        'city': 'LEVIN',
        'postcode': '5510',
        'state_code': 'NI',
        'contact_name': 'Tylar Cardno',
        'contact_phone': '64220474492',
        'contact_email': 'levin@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'levin@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'tylar.cardno@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 80: Shosha Greerton
    {
        'code': '80',
        'name': 'Shosha Greerton',
        'address1': '3/196 Chadwick Road',
        'address2': 'UNIT 3',
        'suburb': 'GREERTON',
        'city': 'TAURANGA',
        'postcode': '3112',
        'state_code': 'NI',
        'contact_name': 'Harinder',
        'contact_phone': '07-925 9593',
        'contact_email': 'greerton@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'greerton@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 81: Shosha Linwood
    {
        'code': '81',
        'name': 'Shosha Linwood',
        'address1': '1/9 Buckley Road',
        'address2': 'Unit 1',
        'suburb': 'EPSOM',
        'city': 'AUCKLAND',
        'postcode': '1023',
        'state_code': 'NI',
        'contact_name': 'Atul Berry',
        'contact_phone': '0297777003',
        'contact_email': '',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'linwood@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 82: Shosha Christchurch CBD
    {
        'code': '82',
        'name': 'Shosha Christchurch CBD',
        'address1': '227B High Street',
        'address2': '',
        'suburb': 'CHRISTCHURCH CENTRAL',
        'city': 'CHRISTCHURCH',
        'postcode': '8011',
        'state_code': 'SI',
        'contact_name': 'Atul Berry',
        'contact_phone': '0297777003',
        'contact_email': 'christchurchcbd@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'christchurchcbd@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 83: Shosha Rangiora
    {
        'code': '83',
        'name': 'Shosha Rangiora',
        'address1': '15 High Street',
        'address2': 'UNIT C',
        'suburb': '',
        'city': 'RANGIORA',
        'postcode': '7400',
        'state_code': 'SI',
        'contact_name': 'Abhishek Khurana',
        'contact_phone': '+64297777990',
        'contact_email': 'rangiora@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'rangiora@shosha.nz',
        'notification_events1': 'OnDeliveryVehicle;AtDeliveryDepot;PickedUp',
        'notification_email2': 'abhishek.khurana@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 84: Shosha Masterton
    {
        'code': '84',
        'name': 'Shosha Masterton',
        'address1': '152 Queen Street',
        'address2': '',
        'suburb': '',
        'city': 'MASTERTON',
        'postcode': '5810',
        'state_code': 'NI',
        'contact_name': 'Rohit',
        'contact_phone': '0221915366',
        'contact_email': '',
        'delivery_instructions': 'Pallet jack/tail lift required. No forklift on site',
        'pickup_from_depot': False,
        'notification_email1': 'masterton@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 85: Shosha Fairfield
    {
        'code': '85',
        'name': 'Shosha Fairfield',
        'address1': '303F Clarkin Road',
        'address2': '',
        'suburb': 'FAIRFIELD',
        'city': 'HAMILTON',
        'postcode': '3214',
        'state_code': 'NI',
        'contact_name': 'Chandu',
        'contact_phone': '021958442',
        'contact_email': '',
        'delivery_instructions': 'Tail Lift required, no pallet jack/forklift at site.',
        'pickup_from_depot': False,
        'notification_email1': 'fairfield@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 86: Shosha Waitara
    {
        'code': '86',
        'name': 'Shosha Waitara',
        'address1': '48 McLean Street',
        'address2': '',
        'suburb': '',
        'city': 'WAITARA',
        'postcode': '4320',
        'state_code': 'NI',
        'contact_name': 'Prince',
        'contact_phone': '0297777702',
        'contact_email': '',
        'delivery_instructions': 'Tail lift required. No pallet jack/forklift on site',
        'pickup_from_depot': False,
        'notification_email1': 'waitara@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 87: Shosha Inglewood
    {
        'code': '87',
        'name': 'Shosha Inglewood',
        'address1': '42 Matai Street',
        'address2': '',
        'suburb': '',
        'city': 'INGLEWOOD',
        'postcode': '4330',
        'state_code': 'NI',
        'contact_name': 'Prince',
        'contact_phone': '0297777702',
        'contact_email': '',
        'delivery_instructions': 'Tail lift required, no pallet jack/forklift on site',
        'pickup_from_depot': False,
        'notification_email1': 'inglewood@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': 'taranaki@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 88: Shosha Dargaville
    {
        'code': '88',
        'name': 'Shosha Dargaville',
        'address1': '52 Victoria Street',
        'address2': '',
        'suburb': '',
        'city': 'DARGAVILLE',
        'postcode': '0310',
        'state_code': 'NI',
        'contact_name': 'Hameed',
        'contact_phone': '0211837862',
        'contact_email': '',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'dargaville@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': 'shagul.kareem@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 89: Shosha Warkworth
    {
        'code': '89',
        'name': 'Shosha Warkworth',
        'address1': '6 Neville Street',
        'address2': '',
        'suburb': '',
        'city': 'WARKWORTH',
        'postcode': '0910',
        'state_code': 'NI',
        'contact_name': 'Sunny Gupta',
        'contact_phone': '0297777700',
        'contact_email': '',
        'delivery_instructions': 'Please call 30 minutes prior to delivery. Tail lift required.',
        'pickup_from_depot': False,
        'notification_email1': 'sunny.g@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 90: Shosha Wainuiomata
    {
        'code': '90',
        'name': 'Shosha Wainuiomata',
        'address1': '3 Queen Street',
        'address2': '',
        'suburb': 'WAINUIOMATA',
        'city': 'LOWER HUTT',
        'postcode': '5014',
        'state_code': 'NI',
        'contact_name': 'Harpreet Bajwa',
        'contact_phone': '0297777781',
        'contact_email': '',
        'delivery_instructions': 'Tail lift/ pallet jack required. No Forklift on site',
        'pickup_from_depot': False,
        'notification_email1': 'wainuiomata@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 91: Shosha Picton
    {
        'code': '91',
        'name': 'Shosha Picton',
        'address1': '8 High Street',
        'address2': '',
        'suburb': '',
        'city': 'PICTON',
        'postcode': '7220',
        'state_code': 'SI',
        'contact_name': 'Sunny Gupta',
        'contact_phone': '0297777700',
        'contact_email': '',
        'delivery_instructions': 'Tail lift/pallet jack required. No forklift on site',
        'pickup_from_depot': False,
        'notification_email1': 'picton@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 92: Shosha Miramar
    {
        'code': '92',
        'name': 'Shosha Miramar',
        'address1': '508A Broadway',
        'address2': '',
        'suburb': 'STRATHMORE PARK',
        'city': 'WELLINGTON',
        'postcode': '6022',
        'state_code': 'NI',
        'contact_name': 'Mohammad Rizwan',
        'contact_phone': '+64273608080',
        'contact_email': '',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'mohammad.rizwan@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 93: Shosha Te Awamutu
    {
        'code': '93',
        'name': 'Shosha Te Awamutu',
        'address1': '45 Alexandra Street',
        'address2': '',
        'suburb': '',
        'city': 'TE AWAMUTU',
        'postcode': '3800',
        'state_code': 'NI',
        'contact_name': 'Chandu',
        'contact_phone': '021958442',
        'contact_email': '',
        'delivery_instructions': 'Tail lift required, no pallet jack/forklift on site',
        'pickup_from_depot': False,
        'notification_email1': '',
        'notification_events1': '',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 101: Shosha Te Puke
    {
        'code': '101',
        'name': 'Shosha Te Puke',
        'address1': '27 Jellicoe Street',
        'address2': '',
        'suburb': '',
        'city': 'TE PUKE',
        'postcode': '3119',
        'state_code': 'NI',
        'contact_name': 'Himanshu',
        'contact_phone': '0224607369',
        'contact_email': '',
        'delivery_instructions': 'Tail lift required, no pallet jack/forklift on site.',
        'pickup_from_depot': False,
        'notification_email1': 'tepuke@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 102: Shosha Kaitaia
    {
        'code': '102',
        'name': 'Shosha Kaitaia',
        'address1': '47 Commerce Street',
        'address2': '',
        'suburb': '',
        'city': 'KAITAIA',
        'postcode': '0410',
        'state_code': 'NI',
        'contact_name': 'Tama Natrass',
        'contact_phone': '0211043295',
        'contact_email': '',
        'delivery_instructions': 'Tail lift required, no pallet jack/forklift on site',
        'pickup_from_depot': False,
        'notification_email1': 'kaitaia@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 103: Shosha Kaiapoi
    {
        'code': '103',
        'name': 'Shosha Kaiapoi',
        'address1': '115 Williams Street',
        'address2': '',
        'suburb': '',
        'city': 'KAIAPOI',
        'postcode': '7630',
        'state_code': 'SI',
        'contact_name': 'Kartik Sharma',
        'contact_phone': '0204904904',
        'contact_email': '',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'kaiapoi@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 111: Shosha Greymouth
    {
        'code': '111',
        'name': 'Shosha Greymouth',
        'address1': '36 MacKay Street',
        'address2': '',
        'suburb': '',
        'city': 'GREYMOUTH',
        'postcode': '7805',
        'state_code': 'SI',
        'contact_name': 'Venkatesh',
        'contact_phone': '02102246232',
        'contact_email': '',
        'delivery_instructions': 'Pallet jack/Tail Lift required as no forklift on site. Please call 30 minutes prior to delivery',
        'pickup_from_depot': False,
        'notification_email1': 'greymouth@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': 'venkatesh.naik@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 112: Shosha Westport
    {
        'code': '112',
        'name': 'Shosha Westport',
        'address1': '184 Palmerston Street',
        'address2': '',
        'suburb': '',
        'city': 'WESTPORT',
        'postcode': '7825',
        'state_code': 'SI',
        'contact_name': 'Rakesh Kumar',
        'contact_phone': '02108053972',
        'contact_email': '',
        'delivery_instructions': 'Pallet jack/Tail Lift required as no forklift on site. Please call 30 minutes prior to delivery',
        'pickup_from_depot': False,
        'notification_email1': 'abhishek.khurana@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': 'rakesh.kumar@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
    # Code 118: Shosha Matamata
    {
        'code': '118',
        'name': 'Shosha Matamata',
        'address1': '56 Arawa Street',
        'address2': '',
        'suburb': '',
        'city': 'MATAMATA',
        'postcode': '3400',
        'state_code': 'NI',
        'contact_name': 'Chandu',
        'contact_phone': '021958442',
        'contact_email': '',
        'delivery_instructions': 'Tail lift/pallet jack required, no forklift on site. Please call Chandu 021958442, 1 hour prior to delivery',
        'pickup_from_depot': False,
        'notification_email1': 'matamata@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': '',
        'notification_events2': '',
    },
    # Code 119: Shosha Wellsford
    {
        'code': '119',
        'name': 'Shosha Wellsford',
        'address1': '153 Rodney St',
        'address2': '',
        'suburb': '',
        'city': 'WELLSFORD',
        'postcode': '0900',
        'state_code': 'NI',
        'contact_name': 'Sunny Gupta',
        'contact_phone': '0297777700',
        'contact_email': 'praveen.paul@shosha.nz',
        'delivery_instructions': 'Please call 30 minutes prior. Tail-Lift and Pallet Jack Required.',
        'pickup_from_depot': False,
        'notification_email1': 'Wellsford@shosha.nz',
        'notification_events1': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
        'notification_email2': 'sunny.g@shosha.nz',
        'notification_events2': 'InformationReceived;PickedUp;InTransit;AtDeliveryDepot;OnDeliveryVehicle;Delivered',
    },
]


def init_db() -> None:
    with connection() as conn:
        # ── Core schema (idempotent CREATE TABLE IF NOT EXISTS) ──────────────
        for stmt in _DDL:
            conn.execute(stmt)

        # ── Column migrations (idempotent ADD COLUMN IF NOT EXISTS) ──────────
        for stmt in _DDL_MIGRATIONS:
            conn.execute(stmt)

        # ── Seed groups + stores on fresh database ────────────────────────────
        group_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM store_groups"
        ).fetchone()["cnt"]

        if group_count == 0:
            for group_name, stores in DEFAULT_GROUPS.items():
                conn.execute(
                    "INSERT INTO store_groups (group_name) VALUES (?) ON CONFLICT (group_name) DO NOTHING",
                    (group_name,),
                )
                row = conn.execute(
                    "SELECT id FROM store_groups WHERE group_name = ?",
                    (group_name,),
                ).fetchone()
                group_id = row["id"]
                conn.executemany(
                    "INSERT INTO stores (store_name, group_id, group_name) VALUES (?, ?, ?) ON CONFLICT (store_name) DO NOTHING",
                    [(store, group_id, group_name) for store in stores],
                )

        # ── Seed store addresses (safe to run repeatedly — no-ops if set) ─────
        for store_name, addr in _STORE_ADDRESS_SEED.items():
            conn.execute(
                """
                UPDATE stores
                SET contact_name  = COALESCE(contact_name,  ?),
                    phone         = COALESCE(phone,         ?),
                    email         = COALESCE(email,         ?),
                    address_line1 = COALESCE(address_line1, ?),
                    address_line2 = COALESCE(address_line2, ?),
                    suburb        = COALESCE(suburb,        ?),
                    city          = COALESCE(city,          ?),
                    postcode      = COALESCE(postcode,      ?)
                WHERE store_name = ? AND address_line1 IS NULL
                """,
                (
                    addr["contact_name"], addr["phone"], addr["email"],
                    addr["address_line1"], addr["address_line2"],
                    addr["suburb"], addr["city"], addr["postcode"],
                    store_name,
                ),
            )

        # ── Seed warehouse settings (once) ────────────────────────────────────
        wh_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM warehouse_settings"
        ).fetchone()["cnt"]
        if wh_count == 0:
            w = _WAREHOUSE_DEFAULT
            conn.execute(
                """
                INSERT INTO warehouse_settings
                    (warehouse_name, contact_name, phone, email,
                     address_line1, address_line2, suburb, city, postcode, country)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    w["warehouse_name"], w["contact_name"], w["phone"], w["email"],
                    w["address_line1"], w["address_line2"], w["suburb"],
                    w["city"], w["postcode"], w["country"],
                ),
            )

        # ── Seed pallet address book (upsert — safe to re-run) ───────────────
        for rec in _PALLET_ADDRESS_SEED:
            conn.execute(
                """
                INSERT INTO pallet_address_book
                    (code, name, address1, address2, suburb, city, postcode,
                     state_code, contact_name, contact_phone, contact_email,
                     delivery_instructions, pickup_from_depot,
                     notification_email1, notification_events1,
                     notification_email2, notification_events2)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (code) DO UPDATE SET
                    name                  = EXCLUDED.name,
                    address1              = EXCLUDED.address1,
                    address2              = EXCLUDED.address2,
                    suburb                = EXCLUDED.suburb,
                    city                  = EXCLUDED.city,
                    postcode              = EXCLUDED.postcode,
                    state_code            = EXCLUDED.state_code,
                    contact_name          = EXCLUDED.contact_name,
                    contact_phone         = EXCLUDED.contact_phone,
                    contact_email         = EXCLUDED.contact_email,
                    delivery_instructions = EXCLUDED.delivery_instructions,
                    pickup_from_depot     = EXCLUDED.pickup_from_depot,
                    notification_email1   = EXCLUDED.notification_email1,
                    notification_events1  = EXCLUDED.notification_events1,
                    notification_email2   = EXCLUDED.notification_email2,
                    notification_events2  = EXCLUDED.notification_events2,
                    updated_at            = CURRENT_TIMESTAMP
                """,
                (
                    rec["code"], rec["name"], rec["address1"], rec["address2"],
                    rec["suburb"], rec["city"], rec["postcode"], rec["state_code"],
                    rec["contact_name"], rec["contact_phone"], rec["contact_email"],
                    rec["delivery_instructions"], 1 if rec["pickup_from_depot"] else 0,
                    rec["notification_email1"], rec["notification_events1"],
                    rec["notification_email2"], rec["notification_events2"],
                ),
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
