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

_BOOKING_DEFAULTS: dict[str, Any] = {
    "signature_required":     True,
    "authority_to_leave":     False,   # Don't allow leaving without signature
    "no_authority_to_leave":  True,    # Explicit "No ATL" checkbox in Starshipit UI
    "dangerous_goods":        False,
    "saturday_delivery":      False,
    "photo_required":         False,
    "create_return":          False,
    "age_restricted":         False,
    "insurance":              False,
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


def generate_labels(order_ids: list[str]) -> tuple[bytes | str, str]:
    """
    Request Starshipit to generate printable label PDFs for the given order IDs.

    Returns one of:
        (bytes, "")   — raw PDF bytes; caller should use st.download_button
        (str,  "")    — a URL string; caller should use st.link_button
        ("",   msg)   — failure; msg describes the error

    Starshipit's /api/labels endpoint may return either raw PDF bytes or a JSON
    envelope with a URL — we handle both.  We try GET first (which Starshipit
    documents as the download path), then POST as fallback.
    """
    if not order_ids:
        return "", "No order IDs provided"

    clean_ids = [oid for oid in order_ids if oid]
    if not clean_ids:
        return "", "No valid order IDs"

    log.info("Starshipit generate_labels for order_ids=%s", clean_ids)
    ids_param = ",".join(clean_ids)

    def _parse_response(resp: "requests.Response") -> tuple[bytes | str, str]:
        """Try to extract a label from a response — PDF bytes or URL."""
        ct = resp.headers.get("Content-Type", "")
        if resp.ok:
            if "application/pdf" in ct or resp.content[:4] == b"%PDF":
                # Raw PDF bytes — caller will offer a download button
                return resp.content, ""
            # Try JSON envelope
            try:
                data = resp.json()
                if data.get("success") or resp.ok:
                    url = (
                        data.get("url")
                        or data.get("label_url")
                        or data.get("labels_url")
                        or ""
                    )
                    if not url and data.get("labels"):
                        lbls = data["labels"]
                        if isinstance(lbls, list) and lbls:
                            url = lbls[0].get("url") or lbls[0].get("label_url") or ""
                    if url:
                        return url, ""
                errors = data.get("errors") or []
                msg = (
                    "; ".join(e.get("description", str(e)) for e in errors)
                    if errors
                    else data.get("message", "Unknown response from Starshipit")
                )
                return "", msg
            except Exception:
                pass
        return "", f"HTTP {resp.status_code}"

    headers = _headers()
    try:
        # ── Attempt 1: GET /api/labels?order_ids=... ─────────────────────────
        resp = requests.get(
            f"{STARSHIPIT_API_BASE}/labels",
            headers=headers,
            params={"order_ids": ids_param},
            timeout=30,
        )
        log.debug("Starshipit GET labels [%s] ct=%s", resp.status_code, resp.headers.get("Content-Type"))
        result, err = _parse_response(resp)
        if result:
            return result, ""

        # ── Attempt 2: POST /api/labels {order_ids: [...]} ───────────────────
        resp2 = requests.post(
            f"{STARSHIPIT_API_BASE}/labels",
            headers=headers,
            json={"order_ids": [int(oid) for oid in clean_ids]},
            timeout=30,
        )
        log.debug("Starshipit POST labels [%s] ct=%s", resp2.status_code, resp2.headers.get("Content-Type"))
        result2, err2 = _parse_response(resp2)
        if result2:
            return result2, ""

        return "", err2 or err or "No label returned by Starshipit"

    except requests.exceptions.Timeout:
        return "", "Label request timed out (30 s)"
    except requests.exceptions.ConnectionError as exc:
        return "", f"Connection error: {exc}"
    except RuntimeError as exc:
        return "", str(exc)
    except Exception as exc:
        log.exception("Unexpected error generating labels")
        return "", str(exc)
