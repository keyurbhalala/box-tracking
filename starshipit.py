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
    label_pdf: bytes = b""       # decoded PDF from POST /api/orders/shipment
    label_error: str = ""        # non-empty if label generation failed (separate from booking)
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


def _cm_to_m(val: float) -> float:
    """Convert centimetres to metres for Starshipit API (which expects metres)."""
    return round(val / 100, 4)


def _build_packages_list(package: Package) -> list[dict[str, Any]]:
    """
    Build the packages list used by both POST /api/orders and POST /api/orders/shipment.
    Each entry: {weight (kg), length/width/height (m), quantity}.
    """
    if package.per_box_dims:
        return [
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
        total_weight = round(package.weight_per_box * package.boxes, 3)
        return [
            {
                "weight":   total_weight,
                "length":   _cm_to_m(package.length),
                "width":    _cm_to_m(package.width),
                "height":   _cm_to_m(package.height),
                "quantity": package.boxes,
            }
        ]


def _build_payload(
    sender: Address,
    recipient: Address,
    package: Package,
    reference: str,
    service_code: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (order_payload, packages_list). packages_list is reused for label submission."""
    packages_list = _build_packages_list(package)
    payload = {
        "order": {
            "order_number": reference,   # required by Starshipit API
            "reference":    reference,
            "carrier_name": "NZ Post Domestic",
            # service_code intentionally NOT set here — Starshipit concatenates
            # any value stored on the order with carrier_service_code sent to
            # POST /api/orders/shipment, producing "NZREG,NZREG" which is invalid.
            # The service code is set exclusively via the shipment endpoint.
            **_BOOKING_DEFAULTS,
            "sender_details": sender.as_api_dict(),
            "destination":    recipient.as_api_dict(),
            "packages":       packages_list,
        }
    }
    return payload, packages_list


def _submit_for_label(
    order_id: str,
    carrier: str,
    service_code: str,
    packages_list: list[dict[str, Any]],
    reprint: bool = False,
) -> tuple[bytes, str]:
    """
    POST /api/orders/shipment — submits the order to NZ Post and returns label PDF bytes.

    On first call (reprint=False): submits to carrier, moves order to "Printed" in NZ Post.
    On reprint (reprint=True):     fetches previously generated labels.

    carrier / service_code: pass the values returned by POST /api/orders (not user-entered),
    so Starshipit gets the exact internal names it assigned.  Pass empty strings to omit.

    The `labels` field in the response is a list of base64-encoded PDF strings,
    one per physical package/box.  We decode and concatenate them.
    """
    import base64

    # Expand quantity>1 into individual package entries — the shipment endpoint
    # expects one dict per physical box, not quantity shortcuts.
    expanded: list[dict] = []
    for pkg in packages_list:
        qty = int(pkg.get("quantity", 1))
        base_pkg = {k: v for k, v in pkg.items() if k != "quantity"}
        if qty > 1:
            per_box_w = round(base_pkg.get("weight", 1.0) / qty, 3)
            for _ in range(qty):
                expanded.append({**base_pkg, "weight": per_box_w})
        else:
            expanded.append(base_pkg)

    body: dict[str, Any] = {
        "order_id": int(order_id),
        "reprint":  reprint,
    }
    # The order already has package details from POST /api/orders creation.
    # Sending packages again causes Starshipit to apply the service_code once
    # per package entry → "NZREG,NZREG" for a 2-box shipment.
    # Solution: send carrier + carrier_service_code to identify the product,
    # but NO packages — Starshipit uses what's already on the order.
    if carrier:
        body["carrier"] = carrier
    if service_code:
        body["carrier_service_code"] = service_code
    # Intentionally omit "packages" — already stored on the order.

    try:
        resp = requests.post(
            f"{STARSHIPIT_API_BASE}/orders/shipment",
            headers=_headers(),
            json=body,
            timeout=30,
        )
        # Always log full response so errors are visible in Streamlit Cloud logs
        log.info(
            "Starshipit shipment label order_id=%s reprint=%s status=%s body=%s",
            order_id, reprint, resp.status_code, resp.text[:1000],
        )
        data = resp.json()

        if data.get("success") and data.get("labels"):
            pdfs = [base64.b64decode(lbl) for lbl in data["labels"]]
            return b"".join(pdfs), ""

        errors = data.get("errors") or []
        msg = (
            "; ".join(e.get("description", str(e)) for e in errors)
            if errors
            else data.get("message", f"No labels in response (HTTP {resp.status_code}): {resp.text[:200]}")
        )
        return b"", msg

    except requests.exceptions.Timeout:
        return b"", "Label request timed out"
    except Exception as exc:
        log.exception("Error in _submit_for_label for order %s", order_id)
        return b"", str(exc)


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
        payload, packages_list = _build_payload(sender, recipient, package, reference, service_code)
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
            order          = data.get("order", {})
            pkgs           = order.get("packages", [])
            tracking       = pkgs[0].get("tracking_number", "") if pkgs else ""
            order_id       = str(order.get("order_id", ""))
            # Use the exact carrier name / service code Starshipit assigned —
            # these must match what the shipment endpoint expects.
            actual_carrier = order.get("carrier_name", "")
            actual_svc     = order.get("service_code", service_code)

            log.info(
                "Order created: order_id=%s carrier='%s' service='%s' tracking='%s'",
                order_id, actual_carrier, actual_svc, tracking,
            )

            # ── Step 2: submit to carrier and generate label ──────────────
            # POST /api/orders only creates a draft; POST /api/orders/shipment
            # actually submits to NZ Post and returns base64 label PDFs.
            label_pdf  = b""
            label_error = ""
            if order_id:
                label_pdf, label_error = _submit_for_label(
                    order_id, actual_carrier, actual_svc, packages_list, reprint=False
                )
                if label_error:
                    log.warning("Label generation failed for %s: %s", reference, label_error)

            return BookingResult(
                success=True,
                store_name=recipient.name,
                boxes=package.boxes,
                tracking_number=tracking,
                label_url=order.get("label_url", ""),
                label_pdf=label_pdf,
                label_error=label_error,
                consignment_id=order_id,
                carrier=actual_carrier or "NZ Post Domestic",
                service_code=actual_svc,
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


def get_order_details(order_id: str) -> dict:
    """
    GET /api/orders/{order_id} — fetch a Starshipit order and return its full dict.
    Use this to inspect what service_code / carrier Starshipit stored on the order.
    """
    try:
        resp = requests.get(
            f"{STARSHIPIT_API_BASE}/orders/{order_id}",
            headers=_headers(),
            timeout=30,
        )
        log.info("Starshipit GET order [%s]: %.2000s", resp.status_code, resp.text)
        data = resp.json()
        return data.get("order") or data
    except Exception as exc:
        log.exception("Error fetching order %s", order_id)
        return {"error": str(exc)}


def generate_labels(order_id: str) -> tuple[bytes, str]:
    """
    Reprint labels for an already-submitted Starshipit order.

    Uses POST /api/orders/shipment with reprint=True — for orders that were
    already submitted (e.g. from shipment history / retry flow).
    Returns (pdf_bytes, error_message).
    """
    if not order_id:
        return b"", "No order ID provided"
    log.info("Starshipit reprint labels for order_id=%s", order_id)
    return _submit_for_label(order_id, carrier="", service_code="", packages_list=[], reprint=True)
