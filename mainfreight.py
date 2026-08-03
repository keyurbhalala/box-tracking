"""
Mainfreight Transport API service layer — Shosha Daily Freight pallet integration.

Architecture
------------
- All communication with the Mainfreight REST API happens here.
- Business rules are enforced in this module only — NEVER in the UI.
- Credentials are read from st.secrets. NEVER hardcode them.

Housebill numbers
-----------------
  Range: MAS00000000 – MAS99999999 (allocated by Mainfreight).
  The next sequence number is stored in warehouse_settings.next_mf_housebill_seq
  and incremented atomically before each booking via claim_mf_housebill() in
  services.py.

Environment
-----------
  The app starts against the TEST environment (apitest.mainfreight.com).
  Set MAINFREIGHT_PRODUCTION = "true" in st.secrets to switch to production.
  The test API key and production API key are stored separately.

Business rules (per account — never editable by warehouse staff):
  Account code       : HIGHGDF
  Service level      : LCL  (DailyFreight LCL)
  Transport mode     : ROAD
  Freight terms      : D2D  (Door to Door)
  Routing type       : LCL
  Measurement system : metric  (metres / kg)
  Pallet L × W       : 1.20 m × 1.00 m  (LOSCAM standard, fixed)
  Pickup cutoff      : 15:30 (3:30 PM) local time
  LOSCAM account     : 116023  (Retrieval)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

import requests
import streamlit as st

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACCOUNT_CODE   = "HIGHGDF"
_SERVICE_LEVEL  = "LCL"
_TRANSPORT_MODE = "ROAD"
_FREIGHT_TERMS  = "D2D"
_ROUTING_TYPE   = "LCL"
_MEASUREMENT    = "metric"

# Fixed LOSCAM pallet footprint (metres)
PALLET_LENGTH_M = 1.20
PALLET_WIDTH_M  = 1.00

# LOSCAM hire details
_LOSCAM_SUPPLIER    = "L"
_LOSCAM_PALLET_TYPE = "P"
_LOSCAM_TXN_TYPE    = "R"   # Retrieval
_LOSCAM_FROM_ACCT   = "116023"

# Pickup cutoff — 3:30 PM
PICKUP_CUTOFF = time(15, 30)

# Sender — HighGroup warehouse
_SENDER: dict[str, Any] = {
    "name":         "HIGH GROUP",
    "streetNumber": "53",
    "address1":     "O'Rorke Road",
    "address2":     None,
    "residential":  False,
    "suburb":       "Penrose",
    "postCode":     "1061",
    "city":         "Auckland",
    "stateCode":    "NI",
    "countryCode":  "NZ",
}
_SENDER_CONTACT: dict[str, Any] = {
    "name":         "Keyur",
    "phone":        "0220923220",
    "emailAddress": "keyur.bhalala@highgroup.nz",
}

# Mainfreight API event code mapping from CSV notification event names
_EVENT_MAP: dict[str, str] = {
    "InformationReceived":    "StatusUpdateBookingConfirmation",
    "PickedUp":               "StatusUpdateShipmentPickedUp",
    "Delivered":              "StatusUpdateShipmentDelivered",
    "OnDeliveryVehicle":      "StatusUpdateShipmentOutForDelivery",
    "AtDeliveryDepot":        "StatusUpdateDepotReceived",
    "InTransit":              "StatusUpdateDepotReceived",   # closest match
}

MF_TRACKING_URL = "https://www.mainfreight.com/track/MSNZS/{}"


# ---------------------------------------------------------------------------
# API base URL — test vs production
# ---------------------------------------------------------------------------

def _base_url() -> str:
    use_prod = str(st.secrets.get("MAINFREIGHT_PRODUCTION", "false")).lower() == "true"
    host = "api.mainfreight.com" if use_prod else "apitest.mainfreight.com"
    return f"https://{host}/transport/1.0"


def _headers() -> dict[str, str]:
    """Read API key from Streamlit secrets (never hardcoded)."""
    use_prod = str(st.secrets.get("MAINFREIGHT_PRODUCTION", "false")).lower() == "true"
    secret_key = "MAINFREIGHT_API_KEY" if use_prod else "MAINFREIGHT_TEST_API_KEY"
    try:
        return {
            "Authorization": f"Secret {st.secrets[secret_key]}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
    except KeyError as exc:
        raise RuntimeError(
            f"Missing Mainfreight credential in secrets.toml: {exc}. "
            "Add MAINFREIGHT_TEST_API_KEY (and MAINFREIGHT_API_KEY for production)."
        ) from exc


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PalletStore:
    """Receiver address from the pallet_address_book."""
    code: str
    name: str
    address1: str
    address2: str
    suburb: str
    city: str
    postcode: str
    state_code: str               # NI / SI
    contact_name: str
    contact_phone: str
    contact_email: str
    delivery_instructions: str
    pickup_from_depot: bool
    notification_email1: str
    notification_events1: str     # semicolon-separated CSV event names
    notification_email2: str
    notification_events2: str


@dataclass
class MFResult:
    """
    Returned by create_shipment().  Never raises — errors are in .error.
    """
    success: bool
    store_name: str
    pallets: int
    housebill_number: str         = ""
    shipment_uuid: str            = ""   # Mainfreight internal id
    consignment_number: str       = ""   # shipmentNumber e.g. 01031117032
    tracking_url: str             = ""
    label_pdf: bytes              = b""
    label_error: str              = ""
    api_response: str             = ""
    error: str                    = ""


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------

def _build_notifications(store: PalletStore) -> list[dict]:
    """
    Convert pallet_address_book notification fields into the Mainfreight
    notifications array.  Skips empty email addresses.
    """
    result = []
    for email, events_csv in [
        (store.notification_email1, store.notification_events1),
        (store.notification_email2, store.notification_events2),
    ]:
        if not email or not email.strip():
            continue
        raw_events = [e.strip() for e in events_csv.split(";") if e.strip()]
        api_events = []
        seen: set[str] = set()
        for raw in raw_events:
            code = _EVENT_MAP.get(raw)
            if code and code not in seen:
                api_events.append({"code": code})
                seen.add(code)
        if api_events:
            result.append({
                "eventTypeCode": "StatusUpdate",
                "transport": {"code": "Email", "destination": email.strip()},
                "events": api_events,
            })
    return result


def _build_freight_details(
    pallets: int,
    height_m: float,
    weight_per_pallet_kg: float,
) -> list[dict]:
    """One freightDetail entry per pallet (Mainfreight tracks individual units)."""
    return [
        {
            "packTypeCode": "PLT",
            "description":  "Shosha products",
            "weight":       int(round(weight_per_pallet_kg)),
            "length":       PALLET_LENGTH_M,
            "width":        PALLET_WIDTH_M,
            "height":       round(height_m, 3),
            "stackable":    False,
        }
        for _ in range(pallets)
    ]


def _build_hire_lines(pallets: int) -> list[dict]:
    """LOSCAM retrieval hire line."""
    return [
        {
            "supplier":        _LOSCAM_SUPPLIER,
            "palletType":      _LOSCAM_PALLET_TYPE,
            "fromAccount":     _LOSCAM_FROM_ACCT,
            "transactionType": _LOSCAM_TXN_TYPE,
            "quantity":        pallets,
        }
    ]


def _build_payload(
    store: PalletStore,
    pallets: int,
    height_m: float,
    weight_per_pallet_kg: float,
    housebill: str,
    pickup_datetime: datetime,
    use_loscam: bool,
) -> dict:
    payload: dict[str, Any] = {
        "account":           {"code": _ACCOUNT_CODE},
        "housebillNumber":   housebill,
        "serviceLevel":      {"code": _SERVICE_LEVEL},
        "transportMode":     _TRANSPORT_MODE,
        "freightTerms":      _FREIGHT_TERMS,
        "routingType":       _ROUTING_TYPE,
        "systemOfMeasurement": _MEASUREMENT,
        "origin": {
            "sender":  {"name": _SENDER["name"]},
            "address": {k: v for k, v in _SENDER.items() if k != "name"},
            "contact": _SENDER_CONTACT,
            "pickupTime": {
                "toDateTime": pickup_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            "instructions": store.pickup_from_depot
                            and "Depot collect — contact Daily Freight"
                            or None,
        },
        "destination": {
            "receiver": {"name": store.name},
            "address": {
                "address1":    store.address1,
                "address2":    store.address2 or None,
                "residential": False,
                "suburb":      store.suburb or None,
                "postCode":    store.postcode or None,
                "city":        store.city,
                "stateCode":   store.state_code or None,
                "countryCode": "NZ",
            },
            "contact": {
                "name":         store.contact_name or None,
                "phone":        store.contact_phone or None,
                "emailAddress": store.contact_email or None,
            },
            "instructions": store.delivery_instructions or None,
        },
        "freightDetails": _build_freight_details(pallets, height_m, weight_per_pallet_kg),
        "references": [
            {"type": "SenderReference", "value": housebill},
        ],
        "notifications": _build_notifications(store),
    }

    if use_loscam:
        payload["hireLines"] = _build_hire_lines(pallets)

    return payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_shipment(
    store: PalletStore,
    pallets: int,
    height_m: float,
    weight_per_pallet_kg: float,
    housebill: str,
    pickup_datetime: datetime,
    use_loscam: bool = False,
) -> MFResult:
    """
    POST /transport/1.0/customer/shipment?region=NZ

    Creates a Mainfreight consignment.  Returns MFResult — never raises.
    On success: populates consignment_number + shipment_uuid for label fetch.
    """
    log.info(
        "Mainfreight create_shipment housebill=%s store=%s pallets=%d loscam=%s",
        housebill, store.name, pallets, use_loscam,
    )
    raw = ""
    try:
        payload = _build_payload(
            store, pallets, height_m, weight_per_pallet_kg,
            housebill, pickup_datetime, use_loscam,
        )
        resp = requests.post(
            f"{_base_url()}/customer/shipment?region=NZ",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
        raw = resp.text
        log.info(
            "Mainfreight shipment [%s] housebill=%s body=%.1000s",
            resp.status_code, housebill, raw,
        )

        if resp.ok:
            data = resp.json()
            uuid         = data.get("id", "")
            consignment  = data.get("shipmentNumber", "")
            tracking     = MF_TRACKING_URL.format(consignment) if consignment else ""

            log.info(
                "Mainfreight OK: uuid=%s consignment=%s tracking=%s",
                uuid, consignment, tracking,
            )
            return MFResult(
                success=True,
                store_name=store.name,
                pallets=pallets,
                housebill_number=housebill,
                shipment_uuid=uuid,
                consignment_number=consignment,
                tracking_url=tracking,
                api_response=raw[:10_000],
            )

        # API error body
        try:
            err_data = resp.json()
            errors   = err_data.get("errors") or []
            msg      = (
                "; ".join(e.get("message", str(e)) for e in errors)
                if errors
                else err_data.get("message", f"HTTP {resp.status_code}")
            )
        except Exception:
            msg = f"HTTP {resp.status_code}: {raw[:300]}"

        return MFResult(
            success=False, store_name=store.name, pallets=pallets,
            housebill_number=housebill, error=msg, api_response=raw[:10_000],
        )

    except requests.exceptions.Timeout:
        return MFResult(
            success=False, store_name=store.name, pallets=pallets,
            housebill_number=housebill, error="Request timed out (30 s)",
            api_response=raw,
        )
    except requests.exceptions.ConnectionError as exc:
        return MFResult(
            success=False, store_name=store.name, pallets=pallets,
            housebill_number=housebill, error=f"Connection error: {exc}",
            api_response=raw,
        )
    except RuntimeError as exc:
        return MFResult(
            success=False, store_name=store.name, pallets=pallets,
            housebill_number=housebill, error=str(exc),
            api_response=raw,
        )
    except Exception as exc:
        log.exception("Unexpected Mainfreight error housebill=%s", housebill)
        return MFResult(
            success=False, store_name=store.name, pallets=pallets,
            housebill_number=housebill, error=str(exc),
            api_response=raw,
        )


def get_label(shipment_uuid: str) -> tuple[bytes, str]:
    """
    POST /transport/1.0/customer/document?region=NZ

    Fetch the shipping label PDF for an already-created shipment.
    Returns (pdf_bytes, error_message).  Called after create_shipment() succeeds.
    """
    if not shipment_uuid:
        return b"", "No shipment UUID provided"
    log.info("Mainfreight get_label uuid=%s", shipment_uuid)
    try:
        resp = requests.post(
            f"{_base_url()}/customer/document?region=NZ",
            headers=_headers(),
            json={"id": shipment_uuid},
            timeout=30,
        )
        log.info(
            "Mainfreight label [%s] uuid=%s body=%.500s",
            resp.status_code, shipment_uuid, resp.text[:500],
        )
        if resp.ok and resp.content:
            # Response is raw PDF bytes when Content-Type is application/pdf
            ct = resp.headers.get("Content-Type", "")
            if "pdf" in ct.lower():
                return resp.content, ""
            # Some environments return JSON with a base64 field
            try:
                import base64
                data = resp.json()
                b64  = data.get("document") or data.get("content") or ""
                if b64:
                    return base64.b64decode(b64), ""
            except Exception:
                pass
            return resp.content, ""

        return b"", f"Label fetch failed (HTTP {resp.status_code}): {resp.text[:200]}"

    except requests.exceptions.Timeout:
        return b"", "Label request timed out"
    except Exception as exc:
        log.exception("Error fetching Mainfreight label uuid=%s", shipment_uuid)
        return b"", str(exc)


def track_consignment(consignment_number: str) -> dict:
    """
    GET /tracking/1.0/references?referenceType=ConsignmentNumber&reference={n}

    Returns the latest tracking status dict, or {"error": "..."} on failure.
    Requires the Tracking API key (available in Production now).
    """
    if not consignment_number:
        return {"error": "No consignment number"}
    try:
        # Tracking API has its own key (separate from Transport API)
        try:
            key = st.secrets["MAINFREIGHT_TRACKING_API_KEY"]
        except KeyError:
            return {"error": "MAINFREIGHT_TRACKING_API_KEY not set in secrets.toml"}

        resp = requests.get(
            "https://api.mainfreight.com/tracking/1.0/references",
            headers={"Authorization": f"Secret {key}", "Accept": "application/json"},
            params={"referenceType": "ConsignmentNumber", "reference": consignment_number},
            timeout=30,
        )
        log.info(
            "Mainfreight track [%s] consignment=%s body=%.500s",
            resp.status_code, consignment_number, resp.text[:500],
        )
        if resp.ok:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    except Exception as exc:
        log.exception("Error tracking consignment %s", consignment_number)
        return {"error": str(exc)}


def default_pickup_datetime(for_date: "date | None" = None) -> datetime:
    """
    Return the pickup datetime for a given date (default = today).
    Cutoff is 3:30 PM.
    """
    from datetime import date as _date
    d = for_date or _date.today()
    return datetime.combine(d, PICKUP_CUTOFF)
