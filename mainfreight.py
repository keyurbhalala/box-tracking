"""
Mainfreight Transport API service layer — Shosha Daily Freight pallet integration.

Architecture
------------
- All communication with the Mainfreight REST API happens here.
- Business rules are enforced in this module only — NEVER in the UI.
- Credentials are read from st.secrets. NEVER hardcode them.

Housebill numbers
-----------------
  Test:       MAS  + 8 digits  (MAS00000000  – MAS99999999)
  Production: MASC + 7 digits  (MASC0000000  – MASC9999999)
  The next sequence number is stored in warehouse_settings.next_mf_housebill_seq
  and incremented atomically before each booking via claim_mf_housebill() in
  services.py.  Format is selected automatically based on MAINFREIGHT_PRODUCTION.

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
_LOSCAM_TXN_TYPE    = "R"        # Retrieval
_LOSCAM_FROM_ACCT   = "116023"

# Pickup cutoff — 3:30 PM
PICKUP_CUTOFF = time(15, 30)

# Sender — HighGroup warehouse
# Structure matches Mainfreight confirmed working payload exactly.
_SENDER: dict[str, Any] = {
    "code":        _ACCOUNT_CODE,
    "name":        "HIGH GROUP",
    "address1":    "53 O'RORKE ROAD",
    "address2":    "",
    "suburb":      "PENROSE",
    "postCode":    "",
    "city":        "AUCKLAND",
    "stateCode":   "",
    "countryCode": "NZ",
}
_SENDER_CONTACT: dict[str, Any] = {
    "name":           "HIGH GROUP",
    "phone":          "29777005",
    "phoneExtension": "",
    "emailAddress":   "vish@highgroup.nz",
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

def _is_production() -> bool:
    return str(st.secrets.get("MAINFREIGHT_PRODUCTION", "false")).lower() == "true"


def _base_url() -> str:
    host = "api.mainfreight.com" if _is_production() else "apitest.mainfreight.com"
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
                "transport": {"code": "Email", "destination": email.strip()},
                "events": api_events,
            })
    return result


def _build_freight_details(
    heights_m: list[float],
    weights_kg: list[float],
    housebill: str,
    use_loscam: bool,
    length_m: float = PALLET_LENGTH_M,
    width_m: float = PALLET_WIDTH_M,
    description: str = "Shosha Products",
) -> list[dict]:
    """One freightDetail entry per pallet — structure confirmed by Mainfreight working payload.

    - weight / length / width / height sent as strings (Mainfreight requirement).
    - volume calculated as length × width × height.
    - customerItemNumber = housebill + "-" + zero-padded pallet index.
    - hireLines is nested inside each freightDetail (not at shipment level).
    """
    details = []
    for i, (h, w) in enumerate(zip(heights_m, weights_kg)):
        volume = round(length_m * width_m * h, 2)
        entry: dict[str, Any] = {
            "customerItemNumber":  f"{housebill}-{i + 1:04d}",
            "freightDetailNumber": None,
            "packTypeCode":        "PLT",
            "description":         description,
            "weight":              str(int(round(w))),
            "volume":              str(volume),
            "itemLines": [
                {
                    "units":              "1",
                    "packTypeCode":       "PLT",
                    "itemNumber":         None,
                    "description":        "Pallet",
                    "dangerousGoodsLines": [],
                }
            ],
            "length": f"{length_m:.2f}",
            "width":  f"{width_m:.2f}",
            "height": f"{h:.2f}",
        }
        if use_loscam:
            entry["hireLines"] = [
                {
                    "supplier":        _LOSCAM_SUPPLIER,
                    "palletType":      _LOSCAM_PALLET_TYPE,
                    "transactionType": _LOSCAM_TXN_TYPE,
                    "fromAccount":     _LOSCAM_FROM_ACCT,
                    "toAccount":       "",
                    "quantity":        1,
                }
            ]
        details.append(entry)
    return details


def _build_payload(
    store: PalletStore,
    heights_m: list[float],
    weights_kg: list[float],
    housebill: str,
    pickup_datetime: datetime,
    use_loscam: bool,
    length_m: float = PALLET_LENGTH_M,
    width_m: float = PALLET_WIDTH_M,
    description: str = "Shosha Products",
) -> dict:
    """Build the Mainfreight shipment payload.

    Structure confirmed against Mainfreight working payload (Aug 2026):
    - systemOfMeasurement = "Metric" (capital M)
    - hireLines nested inside each freightDetail (not at shipment level)
    - weight / length / width / height sent as strings
    - Empty strings used for optional fields (not null)
    - Origin sender includes account code
    """
    instructions_origin = (
        "Depot collect — contact Daily Freight"
        if store.pickup_from_depot else ""
    )
    instructions_dest = store.delivery_instructions or ""

    return {
        "account":             {"code": _ACCOUNT_CODE},
        "housebillNumber":     housebill,
        "serviceLevel":        {"code": _SERVICE_LEVEL},
        "transportMode":       _TRANSPORT_MODE,
        "freightTerms":        _FREIGHT_TERMS,
        "routingType":         _ROUTING_TYPE,
        "systemOfMeasurement": "Metric",   # capital M — confirmed by working payload
        "origin": {
            "sender": {
                "code": _SENDER["code"],
                "name": _SENDER["name"],
            },
            "address": {
                "streetNumber": "",
                "address1":     _SENDER["address1"],
                "address2":     _SENDER["address2"],
                "suburb":       _SENDER["suburb"],
                "postCode":     _SENDER["postCode"],
                "town":         "",
                "city":         _SENDER["city"],
                "stateCode":    _SENDER["stateCode"],
                "countryCode":  _SENDER["countryCode"],
                "geometry":     {"location": {"latitude": "", "longitude": ""}},
            },
            "contact":         _SENDER_CONTACT,
            "pickupTime":      {"toDateTime": pickup_datetime.strftime("%Y-%m-%dT%H:%M:%S")},
            "referenceNumber": "",
            "instructions":    instructions_origin,
        },
        "destination": {
            "receiver": {"name": store.name},
            "address": {
                "premises":    "",
                "streetNumber": "",
                "address1":    store.address1,
                "address2":    store.address2 or "",
                "residential": False,
                "suburb":      store.suburb or "",
                "postCode":    store.postcode or "",
                "town":        store.city,
                "city":        store.city,
                "stateCode":   store.state_code or "",
                "countryCode": "NZ",
                "geometry":    {"location": {"latitude": None, "longitude": None}},
            },
            "contact": {
                "name":           store.contact_name or "",
                "phone":          store.contact_phone or "",
                "phoneExtension": "",
                "emailAddress":   store.contact_email or "",
            },
            "deliveryTime":    None,
            "serviceProvider": None,
            "referenceNumber": "",
            "instructions":    instructions_dest,
        },
        "freightDetails": _build_freight_details(
            heights_m, weights_kg, housebill, use_loscam, length_m, width_m, description
        ),
        "references": [
            {"type": "SenderReference", "value": housebill},
        ],
        # TEST environment rejects non-whitelisted emails (error TNI003.002) — send []
        # Production sends the store's real notification emails.
        "notifications": _build_notifications(store) if _is_production() else [],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_shipment(
    store: PalletStore,
    heights_m: list[float],
    weights_kg: list[float],
    housebill: str,
    pickup_datetime: datetime,
    use_loscam: bool = False,
    length_m: float = PALLET_LENGTH_M,
    width_m: float = PALLET_WIDTH_M,
    description: str = "Shosha Products",
) -> MFResult:
    """
    POST /transport/1.0/customer/shipment?region=NZ

    Creates a Mainfreight consignment.  Returns MFResult — never raises.
    On success: populates consignment_number + shipment_uuid for label fetch.

    heights_m / weights_kg — one value per pallet (len == number of pallets).
    length_m / width_m     — LOSCAM standard by default; pass custom values when
                             non-LOSCAM pallet is used.
    description            — freight line description on the label / consignment note.
    """
    pallets = len(heights_m)
    log.info(
        "Mainfreight create_shipment housebill=%s store=%s pallets=%d loscam=%s",
        housebill, store.name, pallets, use_loscam,
    )
    raw = ""
    try:
        payload = _build_payload(
            store, heights_m, weights_kg,
            housebill, pickup_datetime, use_loscam,
            length_m, width_m, description,
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


def get_label(
    store: PalletStore,
    heights_m: list[float],
    weights_kg: list[float],
    housebill: str,
    pickup_datetime: datetime,
    use_loscam: bool = False,
    length_m: float = PALLET_LENGTH_M,
    width_m: float = PALLET_WIDTH_M,
    description: str = "Shosha Products",
    thermal_only: bool = False,
) -> tuple[bytes, bytes, str]:
    """
    POST https://{host}/document/1.1/transportdocument?servicetype=system&region=NZ

    Fetch shipping label PDFs for an already-created shipment.
    Returns (thermal_pdf, a4_pdf, error_message).

    Requests both label formats in a single API call (body is an array):
      - STOCK_4X6  → thermal pallet label  (one page per pallet, goes on the pallet)
      - A4         → A4 label sheet        (for records / driver handover)

    Note: the A4 consignment note (freight docket with driver signature fields)
    that Mainfreight generates via their portal is NOT available through this API.
    Mainfreight produces that document internally and gives it to their driver.

    The Document API (v1.1) is a SEPARATE API from the Transport API (v1.0):
      - Different host path: /document/1.1/ (not /transport/1.0/)
      - Requires servicetype=system query param
      - Request body: JSON array of label requests, each with the full shipment payload
      - Response body: JSON array where content[0] is a base64-encoded PDF per item
    """
    import base64

    log.info("Mainfreight get_label housebill=%s store=%s", housebill, store.name)

    host = "api.mainfreight.com" if _is_production() else "apitest.mainfreight.com"
    url = f"https://{host}/document/1.1/transportdocument?servicetype=system&region=NZ"

    shipment_payload = _build_payload(
        store, heights_m, weights_kg,
        housebill, pickup_datetime, use_loscam,
        length_m, width_m, description,
    )

    # Build request array — confirmed field names from Mainfreight working payload:
    #   "region" inside the object, "PageSize" (capital P and S)
    label_request = [
        {
            "region":   "NZ",
            "type":     "NZLabel",
            "PageSize": "STOCK_4X6",   # thermal pallet sticker label
            "format":   "PDF",
            "shipment": shipment_payload,
        },
    ]
    if not thermal_only:
        label_request.append(
            {
                "region":   "NZ",
                "type":     "NZLabel",
                "PageSize": "A4",      # A4 label sheet (skipped for reprints)
                "format":   "PDF",
                "shipment": shipment_payload,
            }
        )

    def _extract_pdf(item: dict) -> bytes:
        content_list = item.get("content") or []
        if content_list and content_list[0]:
            return base64.b64decode(content_list[0])
        return b""

    try:
        resp = requests.post(url, headers=_headers(), json=label_request, timeout=30)
        log.info(
            "Mainfreight label [%s] housebill=%s thermal_only=%s body=%.300s",
            resp.status_code, housebill, thermal_only, resp.text[:300],
        )

        if resp.ok and resp.content:
            try:
                data = resp.json()
                if isinstance(data, list):
                    thermal = _extract_pdf(data[0]) if len(data) > 0 else b""
                    a4      = _extract_pdf(data[1]) if len(data) > 1 else b""
                    if thermal or a4:
                        return thermal, a4, ""
            except Exception as parse_exc:
                log.warning("Could not parse label JSON: %s", parse_exc)

            # Fallback: raw bytes (only one doc returned)
            ct = resp.headers.get("Content-Type", "")
            if "pdf" in ct.lower():
                return resp.content, b"", ""

            return b"", b"", f"Label response not recognised: {resp.text[:200]}"

        return b"", b"", f"Label fetch failed (HTTP {resp.status_code}): {resp.text[:300]}"

    except requests.exceptions.Timeout:
        return b"", b"", "Label request timed out"
    except Exception as exc:
        log.exception("Error fetching Mainfreight label housebill=%s", housebill)
        return b"", b"", str(exc)


def validate_address(
    address1: str,
    suburb: str | None,
    city: str,
    postcode: str | None,
) -> tuple[bool, dict, str]:
    """
    POST /transport/1.0/address/validate?region=NZ

    Validate a NZ delivery address against Mainfreight's address database.
    Call this before saving a new pallet store so typos are caught early.

    Returns:
        (is_valid, validated_data, error_message)
        On success, validated_data contains the normalised address Mainfreight
        matched (suburb / city / postCode may differ from what was submitted).
    """
    payload: dict[str, str] = {"address1": address1.strip(), "countryCode": "NZ"}
    if suburb and suburb.strip():
        payload["suburb"] = suburb.strip()
    if city and city.strip():
        payload["city"] = city.strip()
    if postcode and postcode.strip():
        payload["postCode"] = postcode.strip()

    log.info("Mainfreight validate_address payload=%s", payload)
    try:
        resp = requests.post(
            f"{_base_url()}/address/validate?region=NZ",
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        log.info(
            "Mainfreight validate_address [%s] body=%.300s",
            resp.status_code, resp.text[:300],
        )

        if resp.ok:
            return True, resp.json(), ""

        try:
            err  = resp.json()
            errs = err.get("errors") or []
            msg  = (
                "; ".join(e.get("message", str(e)) for e in errs)
                if errs else err.get("message", f"HTTP {resp.status_code}")
            )
        except Exception:
            msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
        return False, {}, msg

    except requests.exceptions.Timeout:
        return False, {}, "Address validation timed out (15 s)"
    except requests.exceptions.ConnectionError as exc:
        return False, {}, f"Connection error: {exc}"
    except RuntimeError as exc:
        return False, {}, str(exc)
    except Exception as exc:
        log.exception("Unexpected error in validate_address")
        return False, {}, str(exc)


def delete_shipment(shipment_uuid: str) -> tuple[bool, str]:
    """
    DELETE /transport/1.0/customer/shipment/{uuid}?region=NZ

    Cancels an existing shipment.  Only succeeds if the shipment has not yet
    been processed (picked up) by Mainfreight.

    Returns (success: bool, error_message: str).
    """
    if not shipment_uuid:
        return False, "No shipment UUID — cannot delete."
    log.info("Mainfreight delete_shipment uuid=%s", shipment_uuid)
    try:
        resp = requests.delete(
            f"{_base_url()}/customer/shipment/{shipment_uuid}",
            headers=_headers(),
            params={"region": "NZ"},
            timeout=30,
        )
        log.info(
            "Mainfreight delete [%s] uuid=%s body=%.300s",
            resp.status_code, shipment_uuid, resp.text[:300],
        )
        if resp.ok:
            return True, ""
        try:
            err  = resp.json()
            errs = err.get("errors") or []
            msg  = (
                "; ".join(e.get("message", str(e)) for e in errs)
                if errs else err.get("message", f"HTTP {resp.status_code}")
            )
        except Exception:
            msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
        return False, msg
    except requests.exceptions.Timeout:
        return False, "Request timed out (30 s)"
    except Exception as exc:
        log.exception("Error deleting shipment uuid=%s", shipment_uuid)
        return False, str(exc)


def track_consignment(consignment_number: str) -> dict:
    """
    GET /transport/1.0/customer/shipment?region=NZ&housebillNumber={n}

    Returns the latest tracking status dict, or {"error": "..."} on failure.
    Uses the same API key as the transport API (MAINFREIGHT_API_KEY in production).
    Only available in production — test environment does not expose tracking data.
    """
    if not consignment_number:
        return {"error": "No consignment number"}
    if not _is_production():
        return {"error": "Tracking is only available in the production environment."}
    try:
        host = "api.mainfreight.com"
        resp = requests.get(
            f"https://{host}/transport/1.0/customer/shipment",
            headers=_headers(),
            params={"region": "NZ", "housebillNumber": consignment_number},
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


# Public alias — lets the UI layer inspect the payload without calling the API
build_payload = _build_payload


def default_pickup_datetime(for_date: "date | None" = None) -> datetime:
    """
    Return the pickup datetime for a given date (default = today).
    Cutoff is 3:30 PM.
    """
    from datetime import date as _date
    d = for_date or _date.today()
    return datetime.combine(d, PICKUP_CUTOFF)
