"""
Starshipit API service layer — Shosha NZ Post courier integration.

Architecture
------------
- All communication with the Starshipit REST API happens here.
- Business rules are enforced in this module only — NEVER in the UI.
- Credentials are read from st.secrets. NEVER hardcode them.

Business rules (per spec — never editable by warehouse staff):
  Signature Required  : TRUE
  Authority To Leave  : FALSE
  Dangerous Goods     : FALSE
  Saturday Delivery   : FALSE
  Photo Required      : FALSE
  Create Return       : FALSE
  Age Restricted      : FALSE
  Insurance           : FALSE
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
import streamlit as st

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STARSHIPIT_API_BASE = "https://api.starshipit.com/api"

NZ_POST_TRACKING_URL = "https://www.nzpost.co.nz/tools/tracking/item/{}"

DEFAULT_SERVICE_CODE = "NZREG"          # NZ Post Standard Tracked

SERVICE_OPTIONS: dict[str, str] = {
    "NZ Post Standard (NZREG)": "NZREG",
    "NZ Post Express (NZEXP)":  "NZEXP",
}

# ---------------------------------------------------------------------------
# Business rules — applied to every booking, invisible to UI
# ---------------------------------------------------------------------------

_BOOKING_DEFAULTS: dict[str, bool] = {
    "signature_required":  True,
    "authority_to_leave":  False,
    "dangerous_goods":     False,
    "saturday_delivery":   False,
    "photo_required":      False,
    "create_return":       False,
    "age_restricted":      False,
    "insurance":           False,
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Address:
    name: str       # contact person's name ("Name" field in Starshipit)
    phone: str
    street: str
    city: str
    postcode: str
    country: str = "NZ"
    email: str = ""
    suburb: str = ""
    building: str = ""
    company: str = ""  # business/company name ("Company" field in Starshipit)

    def as_api_dict(self) -> dict[str, Any]:
        """Format for Starshipit API request body."""
        street = (
            f"{self.building} {self.street}".strip()
            if self.building
            else self.street
        )
        # Postcodes imported via CSV/Excel often arrive as floats (e.g. 1010.0).
        # Strip the redundant decimal so NZ Post receives "1010" not "1010.0".
        pc = str(self.postcode).strip()
        if pc.endswith(".0"):
            pc = pc[:-2]

        d: dict[str, Any] = {
            "name":      self.name,
            "phone":     self.phone,
            "street":    street,
            "city":      self.city,
            "post_code": pc,
            "country":   self.country,
        }
        if self.company:
            d["company"] = self.company
        if self.suburb:
            d["suburb"] = self.suburb
        if self.email:
            d["email"] = self.email
        return d


@dataclass
class Package:
    boxes: int
    weight_per_box: float   # kg  (used when all boxes share the same size)
    length: float           # cm
    width: float            # cm
    height: float           # cm
    # Optional per-box override: list of {weight, length, width, height} dicts,
    # one entry per physical box.  When set, each box is sent as a separate
    # package line in the Starshipit payload so dimensions differ per box.
    per_box_dims: "list[dict] | None" = None


@dataclass
class BookingResult:
    """Returned by create_order(). Never raises — errors are in .error."""
    success: bool
    store_name: str
    boxes: int
    tracking_number: str = ""
    label_url: str = ""
    consignment_id: str = ""
    carrier: str = ""
    service_code: str = ""
    booking_status: str = ""
    booked_at: str = ""
    api_response: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    """Read API credentials from Streamlit secrets (never hardcoded)."""
    try:
        return {
            "StarShipIT-Api-Key":           st.secrets["STARSHIPIT_API_KEY"],
            "Ocp-Apim-Subscription-Key":    st.secrets["STARSHIPIT_SUBSCRIPTION_KEY"],
            "Content-Type":                 "application/json",
        }
    except KeyError as exc:
        raise RuntimeError(
            f"Missing Starshipit credential in secrets.toml: {exc}. "
            "Add STARSHIPIT_API_KEY and STARSHIPIT_SUBSCRIPTION_KEY."
        ) from exc


def _build_payload(
    sender: Address,
    recipient: Address,
    package: Package,
    reference: str,
    service_code: str,
) -> dict[str, Any]:
    # Starshipit API expects dimensions in METRES. Users enter cm → divide by 100.
    def _cm_to_m(val: float) -> float:
        return round(val / 100, 4)

    if package.per_box_dims:
        # Each physical box has its own dimensions — send as individual package entries.
        packages_list = [
            {
                "weight":   float(d.get("weight") or 1.0),
                "length":   _cm_to_m(float(d.get("length") or 30.0)),
                "width":    _cm_to_m(float(d.get("width")  or 20.0)),
                "height":   _cm_to_m(float(d.get("height") or 15.0)),
                "quantity": 1,
            }
            for d in package.per_box_dims
        ]
    else:
        # All boxes are the same size — use quantity shorthand.
        total_weight = round(package.weight_per_box * package.boxes, 3)
        packages_list = [
            {
                "weight":   total_weight,
                "length":   _cm_to_m(package.length),
                "width":    _cm_to_m(package.width),
                "height":   _cm_to_m(package.height),
                "quantity": package.boxes,
            }
        ]
    return {
        "order": {
            "order_number": reference,   # required by Starshipit API
            "reference":    reference,   # optional customer reference (same value)
            "carrier_name": "NZ Post",
            "service_code": service_code,
            **_BOOKING_DEFAULTS,
            "sender_details": sender.as_api_dict(),
            "destination":    recipient.as_api_dict(),
            "packages":       packages_list,
        }
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_order(
    sender: Address,
    recipient: Address,
    package: Package,
    reference: str,
    service_code: str = DEFAULT_SERVICE_CODE,
) -> BookingResult:
    """
    Create a Starshipit order and return a BookingResult.

    Never raises — all errors are captured in BookingResult.error so the
    caller can save partial results and prompt for retry.
    """
    log.info("Starshipit create_order ref=%s store=%s", reference, recipient.name)
    raw = ""
    try:
        payload = _build_payload(sender, recipient, package, reference, service_code)
        resp = requests.post(
            f"{STARSHIPIT_API_BASE}/orders",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
        raw = resp.text
        log.debug("Starshipit response [%s]: %.500s", resp.status_code, raw)
        data = resp.json()

        if resp.ok and data.get("success"):
            order    = data.get("order", {})
            packages = order.get("packages", [])
            tracking = packages[0].get("tracking_number", "") if packages else ""
            return BookingResult(
                success=True,
                store_name=recipient.name,
                boxes=package.boxes,
                tracking_number=tracking,
                label_url=order.get("label_url", ""),
                consignment_id=str(order.get("order_id", "")),
                carrier=order.get("carrier_name", "NZ Post"),
                service_code=order.get("service_code", service_code),
                booking_status="Booked",
                booked_at=datetime.utcnow().isoformat(timespec="seconds"),
                api_response=raw[:10_000],
            )

        # API returned an error body
        errors = data.get("errors") or []
        msg = (
            "; ".join(e.get("description", str(e)) for e in errors)
            if errors
            else data.get("message", f"HTTP {resp.status_code}")
        )
        return BookingResult(
            success=False,
            store_name=recipient.name,
            boxes=package.boxes,
            booking_status="Failed",
            error=msg,
            api_response=raw[:10_000],
        )

    except requests.exceptions.Timeout:
        return BookingResult(
            success=False, store_name=recipient.name, boxes=package.boxes,
            booking_status="Failed", error="Request timed out (30 s)",
            api_response=raw,
        )
    except requests.exceptions.ConnectionError as exc:
        return BookingResult(
            success=False, store_name=recipient.name, boxes=package.boxes,
            booking_status="Failed", error=f"Connection error: {exc}",
            api_response=raw,
        )
    except RuntimeError as exc:
        # Credentials not configured
        return BookingResult(
            success=False, store_name=recipient.name, boxes=package.boxes,
            booking_status="Failed", error=str(exc),
            api_response=raw,
        )
    except Exception as exc:
        log.exception("Unexpected Starshipit error for ref=%s", reference)
        return BookingResult(
            success=False, store_name=recipient.name, boxes=package.boxes,
            booking_status="Failed", error=str(exc),
            api_response=raw,
        )


def tracking_url(tracking_number: str) -> str:
    """Return NZ Post tracking URL for a given tracking number."""
    return NZ_POST_TRACKING_URL.format(tracking_number)
