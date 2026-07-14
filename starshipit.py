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

DEFAULT_SERVICE_CODE = "CPOLP"          # NZ Post Courier Post (cheaper option, default for this account)

SERVICE_OPTIONS: dict[str, str] = {
    "Courier Post — Cheap (CPOLE)": "CPOLE",
    "Courier Post — Express (CPOLP)": "CPOLP",
    "ParcelPost Standard (IWXOLP)": "IWXOLP",
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
            "service_code": service_code,  # NZ Post product code e.g. CPOLP (cheap) / CPOLE (express)
            **_BOOKING_DEFAULTS,
            "sender_details": sender.as_api_dict(),
            "destination":    recipient.as_api_dict(),
            "packages":       packages_list,
        }
    }
    return payload, packages_list


def _submit_for_label(
    order_id: str,
    reprint: bool = False,
    carrier_service_code: str = "",
) -> tuple[bytes, str]:
    """
    POST /api/orders/shipment — submits the order to NZ Post and returns label PDF bytes.

    On first call (reprint=False): submits to carrier, moves order to "Printed" in NZ Post.
    On reprint (reprint=True):     fetches previously generated labels.

    carrier_service_code: the NZ Post product code (e.g. "CPOLP" for CourierPost standard).
    Stored on the order during POST /api/orders; passed here for override / diagnostics.

    The `labels` field in the response is a list of base64-encoded PDF strings,
    one per physical package/box.  We decode and concatenate them.
    """
    import base64

    body: dict[str, Any] = {
        "order_id": int(order_id),
        "reprint":  reprint,
    }
    if carrier_service_code:
        body["carrier_service_code"] = carrier_service_code

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



# Keys containing "tracking" that are clearly NOT a tracking number itself
# (status text, a URL, a history list, a date, etc.) — excluded from the
# deep scan below so we don't pick up junk like "tracking_status": "Printed".
_TRACKING_KEY_EXCLUDE = ("status", "url", "link", "page", "event", "history", "date")


def _deep_collect_tracking_numbers(obj: Any, found: list[str]) -> None:
    """
    Recursively walk an arbitrarily-nested dict/list and collect every value
    found under a key containing "tracking" (case-insensitive), skipping
    keys that are obviously something else (tracking_status, tracking_url…).

    Different Starshipit endpoints (the order list endpoint vs. the single
    order detail endpoint) have been observed returning tracking numbers at
    different nesting levels and under different key names — sometimes on
    the order itself, sometimes per-package, sometimes under a "shipments"
    or "consignment" sub-object. Rather than keep guessing exact paths, this
    just searches everywhere.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = k.lower()
            if "tracking" in lk and not any(bad in lk for bad in _TRACKING_KEY_EXCLUDE):
                if isinstance(v, str) and v.strip() and v.strip() not in found:
                    found.append(v.strip())
                elif isinstance(v, (int, float)) and v:
                    s = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
                    if s not in found:
                        found.append(s)
            _deep_collect_tracking_numbers(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _deep_collect_tracking_numbers(item, found)


def _extract_tracking_numbers(order: dict) -> str:
    """
    Pull every tracking number off an order payload and join them.

    A single Starshipit order can hold multiple packages — multi-box store
    shipments create one package per box — and each package gets its OWN NZ
    Post tracking number, not a single shared one. Rather than checking a
    fixed set of paths/key names (which turned out not to match what this
    account's API actually returns), this recursively scans the entire
    response via _deep_collect_tracking_numbers(). Returns "" if nothing has
    been allocated yet, otherwise a comma-separated string of every distinct
    tracking number found (just one, most of the time).
    """
    if not order:
        return ""
    found: list[str] = []
    _deep_collect_tracking_numbers(order, found)
    return ", ".join(found)


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
            tracking       = _extract_tracking_numbers(order)
            order_id       = str(order.get("order_id", ""))
            actual_carrier = order.get("carrier_name", "NZ Post Domestic")
            actual_svc     = order.get("service_code", "")

            log.info(
                "Order created: order_id=%s carrier='%s' service='%s' tracking='%s'",
                order_id, actual_carrier, actual_svc, tracking,
            )

            # ── Step 2: submit to carrier and generate label ──────────────
            # POST /api/orders only creates a draft; POST /api/orders/shipment
            # actually submits to NZ Post and returns base64 label PDFs.
            # Pass the service_code so Starshipit knows the NZ Post product.
            label_pdf  = b""
            label_error = ""
            if order_id:
                # Use the caller-supplied service_code (CPOLP = cheap, CPOLE = express).
                label_pdf, label_error = _submit_for_label(
                    order_id, reprint=False, carrier_service_code=service_code
                )
                if label_error:
                    log.warning("Label generation failed for %s: %s", reference, label_error)

            # Some accounts don't allocate a tracking number until the order is
            # actually submitted to the carrier (POST /api/orders/shipment,
            # just above) — the draft-creation response above can come back
            # with an empty tracking_number even though the booking succeeded.
            # Re-fetch the order once so we don't silently save an empty
            # tracking number (which would hide this booking from Live Tracking).
            if not tracking and order_id:
                try:
                    refreshed = get_order_details(order_id)
                    tracking = _extract_tracking_numbers(refreshed)
                    if tracking:
                        log.info("Tracking number recovered via re-fetch for order_id=%s: %s", order_id, tracking)
                except Exception:
                    log.exception("Could not re-fetch order %s to recover tracking number", order_id)

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


def _rate_address(addr: Address) -> dict[str, Any]:
    """Format an Address for the /api/rates and /api/deliveryservices endpoints."""
    pc = str(addr.postcode).strip()
    if pc.endswith(".0"):
        pc = pc[:-2]
    d: dict[str, Any] = {
        "street":       addr.street,
        "city":         addr.city,
        "post_code":    pc,
        "country_code": addr.country or "NZ",
    }
    if addr.suburb:
        d["suburb"] = addr.suburb
    return d


def get_delivery_quote(
    sender: Address,
    destination: Address,
    package: Package,
    currency: str = "NZD",
) -> tuple[list[dict[str, Any]], str]:
    """
    Fetch live courier price quotes between two addresses.

    Tries POST /api/deliveryservices first — it returns available delivery
    services regardless of whether "checkout rates" are configured on this
    Starshipit account. Falls back to POST /api/rates (checkout rates) if
    that returns nothing.

    Returns (services, error):
      services — list of {"service_code", "service_name", "price"} dicts,
                 cheapest first.
      error    — human-readable message if no live quote could be fetched
                 (services will be [] in that case).
    """
    packages = _build_packages_list(package)
    body: dict[str, Any] = {
        "sender":      _rate_address(sender),
        "destination": _rate_address(destination),
        "packages":    packages,
    }

    def _extract(data: dict[str, Any]) -> list[dict[str, Any]]:
        items = (
            data.get("delivery_services")
            or data.get("services")
            or data.get("rates")
            or []
        )
        out: list[dict[str, Any]] = []
        for it in items:
            code = it.get("carrier_service_code") or it.get("service_code") or ""
            name = (
                it.get("carrier_service_name")
                or it.get("service_name")
                or it.get("carrier_name")
                or code
            )
            price = it.get("total_price", it.get("price"))
            if price is None:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            out.append({"service_code": code, "service_name": name, "price": price})
        out.sort(key=lambda x: x["price"])
        return out

    last_error = ""

    # ── 1. Delivery services — works without checkout-rates configuration ────
    try:
        resp = requests.post(
            f"{STARSHIPIT_API_BASE}/deliveryservices",
            headers=_headers(),
            json={**body, "include_pricing": True},
            timeout=20,
        )
        log.info("Starshipit POST /deliveryservices [%s]: %.1500s", resp.status_code, resp.text)
        data = resp.json()
        services = _extract(data)
        if services:
            return services, ""
        if not data.get("success", True):
            errors = data.get("errors") or []
            last_error = (
                "; ".join(e.get("description", str(e)) for e in errors)
                if errors else data.get("message", "")
            )
    except requests.exceptions.Timeout:
        last_error = "Delivery services request timed out"
    except RuntimeError as exc:
        # Credentials not configured — no point trying the fallback either.
        return [], str(exc)
    except Exception as exc:
        log.exception("Error calling /api/deliveryservices")
        last_error = str(exc)

    # ── 2. Fall back to checkout rates ────────────────────────────────────────
    try:
        resp = requests.post(
            f"{STARSHIPIT_API_BASE}/rates",
            headers=_headers(),
            json={**body, "currency": currency},
            timeout=20,
        )
        log.info("Starshipit POST /rates [%s]: %.1500s", resp.status_code, resp.text)
        data = resp.json()
        services = _extract(data)
        if services:
            return services, ""
        if not data.get("success", True):
            errors = data.get("errors") or []
            last_error = (
                "; ".join(e.get("description", str(e)) for e in errors)
                if errors else data.get("message", last_error)
            )
        elif not last_error:
            last_error = "No live rates returned — check courier & rates setup in Starshipit."
    except requests.exceptions.Timeout:
        last_error = last_error or "Rates request timed out"
    except Exception as exc:
        log.exception("Error calling /api/rates")
        last_error = last_error or str(exc)

    return [], last_error or "Unable to fetch a live quote."


def tracking_url(tracking_number: str) -> str:
    """Return NZ Post tracking URL for a given tracking number."""
    return NZ_POST_TRACKING_URL.format(tracking_number)


# Every status Starshipit's tracking feed can return. The five "main" ones
# are used to draw the progress bar on the Live Tracking page; the rest are
# shown as a standalone badge instead (see app.py _status_bar_html).
TRACKING_STAGES: list[str] = ["Printed", "Dispatched", "InTransit", "OutForDelivery", "Delivered"]


def get_tracking_status(order_id: str = "", tracking_number: str = "") -> dict[str, Any]:
    """
    GET /api/track — poll live carrier tracking status for one order.

    Pass whichever identifier is available; order_id is tried first when
    both are supplied. Never raises — on failure returns {"status": "",
    "error": "..."} so the caller can show a per-row warning instead of
    crashing the whole tracking page.
    """
    params: dict[str, Any] = {}
    if order_id:
        params["order_id"] = order_id
    if tracking_number:
        params["tracking_number"] = tracking_number
    if not params:
        return {"status": "", "error": "No order_id or tracking_number supplied."}

    try:
        resp = requests.get(
            f"{STARSHIPIT_API_BASE}/track",
            headers=_headers(),
            params=params,
            timeout=15,
        )
        log.info("Starshipit GET /track %s [%s]: %.1000s", params, resp.status_code, resp.text)
        data = resp.json()
    except requests.exceptions.Timeout:
        return {"status": "", "error": "Tracking request timed out"}
    except RuntimeError as exc:
        return {"status": "", "error": str(exc)}
    except Exception as exc:
        log.exception("Error calling /api/track for %s", params)
        return {"status": "", "error": str(exc)}

    # This account's exact /api/track response shape isn't fully verified,
    # so unwrap defensively: try a bare top-level record first, then common
    # wrapper keys used elsewhere in the Starshipit API (result/results/tracking).
    record: dict[str, Any] = data
    for key in ("result", "tracking", "results"):
        val = data.get(key)
        if isinstance(val, dict):
            record = val
            break
        if isinstance(val, list) and val:
            record = val[0]
            break

    status = (
        record.get("tracking_status")
        or record.get("status")
        or record.get("order_status")
        or ""
    )
    if not status and not data.get("success", True):
        errors = data.get("errors") or []
        err_msg = (
            "; ".join(e.get("description", str(e)) for e in errors)
            if errors else data.get("message", "No tracking status returned.")
        )
        return {"status": "", "error": err_msg}

    return {
        "status": status,
        "last_updated": record.get("last_updated_date") or record.get("last_updated") or "",
        "carrier_name": record.get("carrier_name") or "",
        "carrier_service": record.get("carrier_service") or "",
    }


def _fetch_order_raw(order_id: str) -> tuple[dict, dict]:
    """
    Hit both Starshipit order-lookup endpoints and return their RAW,
    unmerged responses: (list_endpoint_order, detail_endpoint_order).
    Either half is {} if that endpoint returned nothing / errored.

    Kept separate (rather than merging immediately) so the Diagnostics page
    can show exactly what each endpoint returns on its own — useful for
    figuring out which one actually carries per-package tracking numbers.
    """
    list_order: dict = {}
    detail_order: dict = {}

    try:
        resp = requests.get(
            f"{STARSHIPIT_API_BASE}/orders",
            headers=_headers(),
            params={"order_id": order_id, "limit": 1},
            timeout=30,
        )
        log.info("Starshipit GET orders?order_id=%s [%s]: %.2000s", order_id, resp.status_code, resp.text)
        data = resp.json()
        orders = data.get("orders") or []
        if orders and isinstance(orders[0], dict):
            list_order = orders[0]
    except Exception:
        log.exception("Error fetching order %s via list endpoint", order_id)

    try:
        resp2 = requests.get(
            f"{STARSHIPIT_API_BASE}/orders/{order_id}",
            headers=_headers(),
            timeout=30,
        )
        log.info("Starshipit GET order/%s [%s]: %.2000s", order_id, resp2.status_code, resp2.text)
        data2 = resp2.json()
        detail = data2.get("order") or data2
        if isinstance(detail, dict):
            detail_order = detail
    except Exception:
        log.exception("Error fetching order %s via detail endpoint", order_id)

    return list_order, detail_order


def get_order_details(order_id: str) -> dict:
    """
    Fetch a Starshipit order by its numeric order_id.

    Calls BOTH the list endpoint (GET /api/orders?order_id=...) and the
    single-order endpoint (GET /api/orders/{id}) and merges the results.
    These two endpoints have been observed returning different shapes on
    this account — e.g. one may omit per-package tracking numbers that the
    other includes — so previously only falling back to the second endpoint
    when the first returned nothing meant we sometimes kept using an
    incomplete object. Merging means whichever endpoint actually has the
    tracking data, we'll see it.
    """
    list_order, detail_order = _fetch_order_raw(order_id)
    merged: dict = {}
    merged.update(list_order)
    # Detail endpoint's non-empty values win on key conflicts — it's usually
    # the more complete response — but anything the list endpoint had that
    # detail lacks is kept too.
    for k, v in detail_order.items():
        if v not in (None, "", [], {}):
            merged[k] = v

    if not merged:
        return {"error": "Order not found via either endpoint."}
    return merged


def _deep_collect_tracking_paths(obj: Any, path: str, found: list[tuple[str, str]]) -> None:
    """
    Like _deep_collect_tracking_numbers, but records the exact JSON path
    (e.g. "packages[1].tracking_number") for every match instead of just the
    value — used by the Diagnostics page so it's unambiguous exactly where a
    tracking number was found.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = k.lower()
            new_path = f"{path}.{k}" if path else k
            if "tracking" in lk and not any(bad in lk for bad in _TRACKING_KEY_EXCLUDE):
                if isinstance(v, str) and v.strip():
                    found.append((new_path, v.strip()))
                elif isinstance(v, (int, float)) and v:
                    found.append((new_path, str(v)))
            _deep_collect_tracking_paths(v, new_path, found)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _deep_collect_tracking_paths(item, f"{path}[{i}]", found)


def find_tracking_number_paths(order: dict) -> list[tuple[str, str]]:
    """Public helper for the Diagnostics page: [(json_path, value), ...] for
    every key containing 'tracking' found anywhere in the order dict."""
    found: list[tuple[str, str]] = []
    _deep_collect_tracking_paths(order, "", found)
    return found


def get_order_details_debug(order_id: str) -> dict:
    """
    Diagnostics-only: returns the list-endpoint response, the detail-endpoint
    response, and the merged result side by side, each with its own
    tracking-number-path scan, so a human can compare the numbers shown here
    against what NZ Post's tracking site shows for the same order and see
    exactly which endpoint/path/field is authoritative.
    """
    list_order, detail_order = _fetch_order_raw(order_id)
    merged = get_order_details(order_id)
    return {
        "list_endpoint": {
            "raw": list_order,
            "tracking_paths": find_tracking_number_paths(list_order),
        },
        "detail_endpoint": {
            "raw": detail_order,
            "tracking_paths": find_tracking_number_paths(detail_order),
        },
        "merged": {
            "raw": merged,
            "tracking_paths": find_tracking_number_paths(merged),
        },
    }


def list_available_services() -> list[dict]:
    """
    GET /api/carriers — return all carrier/service combinations configured for this account.
    Use this in diagnostics to discover valid carrier_service_code values.
    """
    try:
        resp = requests.get(
            f"{STARSHIPIT_API_BASE}/carriers",
            headers=_headers(),
            timeout=30,
        )
        log.info("Starshipit GET carriers [%s]: %.3000s", resp.status_code, resp.text)
        data = resp.json()
        return data.get("carriers") or data.get("services") or [data]
    except Exception as exc:
        log.exception("Error listing carriers")
        return [{"error": str(exc)}]


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
    return _submit_for_label(order_id, reprint=True)
