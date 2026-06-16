#!/usr/bin/env python3
"""
Missing Parts Monitor v2 — shelf-stock-first detection.

For every Item-type service line on every open jobsheet in scope (on-site OR
booked next 2 days), determines whether the part is genuinely missing.

A coded part is MISSING unless we physically have it on the shelf:

  1. Shelf-stock test FIRST — do we have it in BC inventory (net of demand
     from other in-scope jobs that don't have their own PO)? If yes → CLEAR
     (not missing, never shown). Being on order does NOT make a part
     un-missing — it only changes which section it lands in.
  2. If NOT on the shelf, it's missing — categorise by PO coverage:
       Tier 1 — dedicated special-order PO (Special_Order_Purchase_No), then
       Tier 2 — general open POs by item code.

States per line:
  CLEAR              — on the shelf (or special PO fully received). Hidden.
  ON_ORDER           — not on shelf, a PO covers the shortfall.
  PARTIALLY_ON_ORDER — not on shelf, a PO exists but for less than needed.
  NO_PO              — not on shelf and no PO at all (urgent), or a placeholder.

Roll-up to jobsheet:
  Worst line state determines the jobsheet outcome.
  MISSING in BC = any non-CLEAR line (placeholder / NO_PO / PARTIAL / ON_ORDER).
  Blank = all CLEAR (and self-heals if previously MISSING).

The Teams post shows ONLY genuinely-missing lines (never in-stock parts) and
differentiates the states: NO PO raised (red), partial PO (amber), on order
(blue). Neil's call: keep BC simple with one MISSING code, show the
distinction in Teams so advisors can prioritise.

Placeholder items (MISC, FILT, CONS, BATT, TYRE, PLUG, DEALER, MISCD, MISCP)
have no real SKU, so they can't have shelf stock or PO coverage → always NO_PO.

Usage:
    python3 monitor_v2.py                 # live mode: write to BC, post to Teams
    python3 monitor_v2.py --dry-run       # read-only, print what would change
    python3 monitor_v2.py --no-teams      # write to BC but skip Teams post
    python3 monitor_v2.py --jobsheet SJ038636  # debug a single jobsheet, dry-run

Reference: .claude/skills/missing-parts-monitor/REWRITE_SCOPE.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import traceback
from collections import defaultdict
from pathlib import Path

import requests

# --- bc_api lives beside this script in the deployment package -------------
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bc_api import BCClient  # noqa: E402

# --- config -----------------------------------------------------------------
PLACEHOLDER_ITEMS = {"MISC", "FILT", "PLUG", "DEALER", "MISCD", "MISCP", "BATT", "TYRE"}

# Items that are NEVER part of the missing-parts check — fee/admin items
# that happen to be Type='Inventory' but aren't physical parts, plus known
# bulk-fluid SKUs where workshop posting of deliveries is unreliable enough
# that BC inventory consistently lags reality. Flagging them just creates
# noise; data hygiene needs to be solved by a different mechanism than this
# monitor. Add new SKUs here as they surface.
EXCLUDED_ITEMS = {
    "WARADMINFEE",     # Warranty Admin Fee — service fee miscategorised as PARTS
    "ENVIRO",          # Environmental Disposal Charge — fee, not a part
    "PRD00002262",     # Castrol Transmax ATF — bulk fluid, BC inventory unreliable
    "CONS",            # Consumables — not a real part, advisor codes directly (Neil 2026-06-02)
    "FIXEDPRICESERV",  # Fixed-price service item — not a physical part (Neil 2026-06-02)
}

# Engine oils are excluded by DESCRIPTION, not item code — there are too many
# oil SKUs to list, and BC stock for bulk fluids lags reality anyway. A line is
# treated as engine oil if its description carries a viscosity grade (e.g.
# 5W30, 0W-40, 10W40) or the literal words "engine oil". Oil FILTERS are
# explicitly excepted so they still get checked. (Neil 2026-06-02.)
OIL_GRADE_RE = re.compile(r"\b\d{1,2}w-?\d{2}\b", re.IGNORECASE)


def is_engine_oil(description: str | None) -> bool:
    d = (description or "")
    dl = d.lower()
    if "filter" in dl:
        return False
    if "engine oil" in dl:
        return True
    return bool(OIL_GRADE_RE.search(d))

MISSING_CODE = "MISSING"
LOOKAHEAD_DAYS = 2

# Fitch Autos Limited > General
TEAM_ID = "30b1b8c4-ba75-46cc-b5b5-c86e0c82e668"
CHANNEL_ID = "19:eec19375e9e8444abac3b05481198ca7@thread.tacv2"

# Garagehive API path (exposed to BC MCP proxy)
GARAGEHIVE_API_PATH = "api/garageHive/service/v2.0"

# State enums
S_CLEAR = "CLEAR"
S_ON_ORDER = "ON_ORDER"
S_PARTIAL = "PARTIALLY_ON_ORDER"
S_NO_PO = "NO_PO"
URGENT_STATES = {S_NO_PO, S_PARTIAL}
WORST_ORDER = {S_NO_PO: 4, S_PARTIAL: 3, S_ON_ORDER: 2, S_CLEAR: 1}


# --- helpers ---------------------------------------------------------------
def gh_url(bc: BCClient, entity: str, record_id: str | None = None) -> str:
    base = f"{bc.api_base}/{GARAGEHIVE_API_PATH}/companies({bc.company_id})/{entity}"
    if record_id:
        base += f"({record_id})"
    return base


def v2_url(bc: BCClient, path: str) -> str:
    """Standard BC API v2.0 endpoint (used for purchaseOrders / items)."""
    return f"{bc.api_v2_base}/companies({bc.company_id})/{path}"


def http_get(bc: BCClient, url: str, params: dict | None = None) -> dict:
    r = requests.get(url, headers=bc._headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_target_jobsheets(bc: BCClient, today: dt.date) -> list[dict]:
    """On-site now OR booked today..today+LOOKAHEAD_DAYS."""
    end = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    select = (
        "id,number,documentType,status,vehicleOnSite,bookingDate,"
        "extendedStatusCode,serviceAdvisor,vehicleRegistrationNo,"
        "sellToCustomerName,workStatusCode"
    )
    base_filter = "documentType eq 'Jobsheet' and status eq 'Open'"

    def run(filter_extra: str) -> list[dict]:
        return http_get(
            bc, gh_url(bc, "jobsheets"),
            params={
                "$filter": f"{base_filter} and {filter_extra}",
                "$select": select,
                "$top": "500",
            },
        ).get("value", [])

    on_site = run("vehicleOnSite eq true")
    upcoming = run(f"bookingDate ge {today.isoformat()} and bookingDate le {end.isoformat()}")
    merged: dict[str, dict] = {}
    for j in on_site + upcoming:
        merged[j["id"]] = j
    return list(merged.values())


def fetch_item_lines(bc: BCClient, jobsheet_numbers: list[str]) -> list[dict]:
    """Every Type='Item' line with Quantity > 0 across the given jobsheets.
    Includes placeholders (MISC etc.) — caller decides what to do with them."""
    if not jobsheet_numbers:
        return []
    out: list[dict] = []
    BATCH = 30
    for i in range(0, len(jobsheet_numbers), BATCH):
        chunk = jobsheet_numbers[i : i + BATCH]
        doc_filter = " or ".join(f"Document_No eq '{n}'" for n in chunk)
        flt = (
            f"Document_Type eq 'Jobsheet' and Type eq 'Item' and Quantity gt 0 and "
            f"({doc_filter})"
        )
        rows = bc.odata(
            "GH1_ServiceLines",
            filter=flt,
            top=1000,
            select="Document_No,Line_No,No,Description,Quantity,Special_Order_Purchase_No,Type",
        )
        out.extend(rows)
    return out


def compute_in_scope_demand(target_lines: list[dict]) -> dict[str, float]:
    """Total quantity needed per item across all in-scope jobsheet lines —
    i.e. lines on jobs that are vehicleOnSite OR booked in the lookahead
    window. Excludes lines that carry their own Special_Order_Purchase_No
    (those are covered by their dedicated PO and don't compete for shelf
    stock).

    This becomes the reservation cap when checking individual lines: for a
    given line, "reserved by others" = total in-scope demand for that item
    minus the line's own quantity. Future bookings outside the window are
    NOT counted — they'll get their own parts ordered separately.
    """
    demand: dict[str, float] = defaultdict(float)
    for L in target_lines:
        item_no = L.get("No") or ""
        if not item_no:
            continue
        if (L.get("Special_Order_Purchase_No") or "").strip():
            continue
        demand[item_no] += float(L.get("Quantity") or 0)
    return demand


def fetch_inventory(bc: BCClient, item_codes: list[str]) -> dict[str, float]:
    """Get current inventory level per item via the BC API v2.0 /items endpoint."""
    if not item_codes:
        return {}
    inv: dict[str, float] = {}
    BATCH = 20
    item_codes_list = list(set(item_codes))
    for i in range(0, len(item_codes_list), BATCH):
        chunk = item_codes_list[i : i + BATCH]
        flt = " or ".join(f"number eq '{c}'" for c in chunk)
        try:
            r = http_get(
                bc, v2_url(bc, "items"),
                params={"$filter": flt, "$select": "number,inventory", "$top": "100"},
            )
        except requests.HTTPError:
            # Fallback per-item if the OR filter chokes
            for c in chunk:
                try:
                    r1 = http_get(
                        bc, v2_url(bc, "items"),
                        params={"$filter": f"number eq '{c}'", "$select": "number,inventory"},
                    )
                    for item in r1.get("value", []):
                        inv[item["number"]] = float(item.get("inventory") or 0)
                except Exception:
                    inv[c] = 0
            continue
        for item in r.get("value", []):
            inv[item["number"]] = float(item.get("inventory") or 0)
    # Ensure every queried item has an entry (default 0 if not found)
    for c in item_codes_list:
        inv.setdefault(c, 0)
    return inv


def fetch_noninventory_codes(bc: BCClient, item_codes: list[str]) -> set[str]:
    """Return item codes whose BC item Type is not 'Inventory' (Service or
    Non-Inventory) — warranty contributions, fees and charges that aren't
    physical stock and must never be flagged as a missing part."""
    skip: set[str] = set()
    if not item_codes:
        return skip
    codes = list(set(item_codes))
    BATCH = 20
    for i in range(0, len(codes), BATCH):
        chunk = codes[i : i + BATCH]
        flt = " or ".join(f"number eq '{c}'" for c in chunk)
        try:
            r = http_get(
                bc, v2_url(bc, "items"),
                params={"$filter": flt, "$select": "number,type", "$top": "100"},
            )
        except requests.HTTPError:
            continue
        for it in r.get("value", []):
            t = str(it.get("type") or "")
            if t and t != "Inventory":
                skip.add(it["number"])
    return skip


def fetch_open_po_lines_by_items(
    bc: BCClient, item_codes: list[str]
) -> dict[str, list[dict]]:
    """For each item code, return a list of open PO lines with receiveQuantity > 0.
    Uses BC API v2.0 /purchaseOrders + nested /purchaseOrderLines.
    """
    if not item_codes:
        return {}
    items_set = set(item_codes)
    out: dict[str, list[dict]] = defaultdict(list)

    # Get all open POs first (status != Closed). status field on PO header.
    # Open == 'Open' / 'Released'; closed POs we ignore.
    pos = http_get(
        bc, v2_url(bc, "purchaseOrders"),
        params={"$select": "id,number,status", "$top": "500"},
    ).get("value", [])
    open_po_ids = [p["id"] for p in pos if (p.get("status") or "") not in ("Closed",)]

    # Batch-pull lines from each open PO. The /purchaseOrders({id})/purchaseOrderLines
    # endpoint is per-PO so we have to walk them. That's N round-trips; acceptable
    # for a typical day's volume of ~30-100 open POs.
    for po_id in open_po_ids:
        try:
            r = http_get(
                bc, v2_url(bc, f"purchaseOrders({po_id})/purchaseOrderLines"),
                params={
                    "$filter": "lineType eq 'Item'",
                    "$select": (
                        "id,documentId,lineObjectNumber,quantity,"
                        "receivedQuantity,receiveQuantity,expectedReceiptDate"
                    ),
                    "$top": "200",
                },
            )
        except requests.HTTPError:
            continue
        for L in r.get("value", []):
            item_no = L.get("lineObjectNumber") or ""
            if item_no not in items_set:
                continue
            if (L.get("receiveQuantity") or 0) <= 0:
                continue
            out[item_no].append({
                "documentId": L.get("documentId"),
                "po_id": po_id,
                "po_number": next((p["number"] for p in pos if p["id"] == po_id), ""),
                "lineObjectNumber": item_no,
                "quantity": L.get("quantity") or 0,
                "receivedQuantity": L.get("receivedQuantity") or 0,
                "receiveQuantity": L.get("receiveQuantity") or 0,
                "expectedReceiptDate": L.get("expectedReceiptDate") or "",
            })
    return out


def fetch_specific_po_line(
    bc: BCClient, po_number: str, item_no: str
) -> dict | None:
    """Look up a specific PO line by PO number + item — used when a service line
    has Special_Order_Purchase_No set."""
    try:
        pos = http_get(
            bc, v2_url(bc, "purchaseOrders"),
            params={"$filter": f"number eq '{po_number}'", "$select": "id,number,status"},
        ).get("value", [])
    except requests.HTTPError:
        return None
    if not pos:
        return None
    po = pos[0]
    try:
        lines = http_get(
            bc, v2_url(bc, f"purchaseOrders({po['id']})/purchaseOrderLines"),
            params={
                "$filter": f"lineObjectNumber eq '{item_no}'",
                "$select": (
                    "id,lineObjectNumber,quantity,receivedQuantity,"
                    "receiveQuantity,expectedReceiptDate"
                ),
            },
        ).get("value", [])
    except requests.HTTPError:
        return None
    if not lines:
        return None
    L = lines[0]
    return {
        "po_id": po["id"],
        "po_number": po["number"],
        "po_status": po.get("status"),
        "lineObjectNumber": L.get("lineObjectNumber"),
        "quantity": L.get("quantity") or 0,
        "receivedQuantity": L.get("receivedQuantity") or 0,
        "receiveQuantity": L.get("receiveQuantity") or 0,
        "expectedReceiptDate": L.get("expectedReceiptDate") or "",
    }


# --- state determination ---------------------------------------------------
def determine_line_state(
    line: dict,
    inventory: dict[str, float],
    in_scope_demand: dict[str, float],
    po_lines_by_item: dict[str, list[dict]],
    specific_po_cache: dict[tuple[str, str], dict | None],
    bc: BCClient,
) -> dict:
    """Return {state, eta?, shortfall?, po_number?, is_placeholder?} for a
    single line. `in_scope_demand` is total demand for the item across all
    in-scope jobs (caller capped scope to vehicleOnSite + lookahead window).
    """
    item_no = line["No"]
    needed = float(line.get("Quantity") or 0)

    # Placeholder items (MISC, FILT, MISCD, etc.): no real SKU, so they can't
    # have shelf stock or match GENERAL open POs by item code. BUT a placeholder
    # line can still carry its own special-order PO (Special_Order_Purchase_No) —
    # the advisor raised a PO for it even though it's coded generically. Check
    # that first; only flag NO_PO when there is genuinely no special-order PO.
    if item_no in PLACEHOLDER_ITEMS:
        sopo = (line.get("Special_Order_Purchase_No") or "").strip()
        if sopo:
            key = (sopo, item_no)
            if key not in specific_po_cache:
                specific_po_cache[key] = fetch_specific_po_line(bc, sopo, item_no)
            po_line = specific_po_cache[key]
            if po_line:
                rq = float(po_line["receiveQuantity"] or 0)
                if rq <= 0:
                    return {"state": S_CLEAR, "po_number": po_line["po_number"],
                            "reason": "placeholder special PO fully received",
                            "is_placeholder": True}
                if rq >= needed:
                    return {"state": S_ON_ORDER, "eta": po_line["expectedReceiptDate"],
                            "po_number": po_line["po_number"],
                            "reason": "placeholder on special-order PO",
                            "is_placeholder": True}
                return {"state": S_PARTIAL, "shortfall": needed - rq,
                        "eta": po_line["expectedReceiptDate"],
                        "po_number": po_line["po_number"],
                        "reason": f"placeholder special PO covers {rq}/{needed}",
                        "is_placeholder": True}
        return {"state": S_NO_PO, "shortfall": needed,
                "reason": "placeholder item, no special-order PO",
                "is_placeholder": True}

    # --- Shelf-stock test FIRST (the primary "not missing" rule) ----------
    # Per Neil 2026-06-02: a coded part is only NOT missing if we physically
    # have it on the shelf. Being on order does NOT make it un-missing — it
    # only changes which section it lands in below. Demand from THIS line
    # shouldn't reserve against itself; only OTHER in-scope demand competes
    # for the shelf.
    inv = inventory.get(item_no, 0)
    total_in_scope = in_scope_demand.get(item_no, 0)
    reserved_by_others = max(0, total_in_scope - needed)
    available = inv - reserved_by_others
    if available >= needed:
        return {"state": S_CLEAR,
                "reason": (f"shelf {inv}, in-scope demand {total_in_scope}, "
                           f"{reserved_by_others} reserved by others, "
                           f"{available} available")}

    remaining_need = needed - max(0, available)

    # --- Not on the shelf → it's MISSING. Categorise by PO coverage. ------
    # Tier 1 — dedicated special-order PO line for this jobsheet.
    sopo = (line.get("Special_Order_Purchase_No") or "").strip()
    if sopo:
        key = (sopo, item_no)
        if key not in specific_po_cache:
            specific_po_cache[key] = fetch_specific_po_line(bc, sopo, item_no)
        po_line = specific_po_cache[key]
        if po_line:
            rq = float(po_line["receiveQuantity"] or 0)
            if rq <= 0:
                # PO fully received — the part has come in even if BC stock
                # hasn't caught up yet. Not a NO-PO blocker; treat as covered.
                return {"state": S_CLEAR, "po_number": po_line["po_number"],
                        "reason": "special PO fully received"}
            elif rq >= remaining_need:
                return {"state": S_ON_ORDER, "eta": po_line["expectedReceiptDate"],
                        "po_number": po_line["po_number"],
                        "reason": f"special PO covers shortfall {remaining_need}"}
            else:
                return {"state": S_PARTIAL, "shortfall": remaining_need - rq,
                        "eta": po_line["expectedReceiptDate"],
                        "po_number": po_line["po_number"],
                        "reason": f"special PO covers {rq}/{remaining_need}"}
        # Fall through if the specific PO line couldn't be fetched.

    # Tier 2 — general open POs by item code.
    po_lines = po_lines_by_item.get(item_no, [])
    if po_lines:
        total_outstanding = sum(p["receiveQuantity"] for p in po_lines)
        eta = max((p["expectedReceiptDate"] for p in po_lines if p["expectedReceiptDate"]), default="")
        po_numbers = sorted({p["po_number"] for p in po_lines})
        if total_outstanding >= remaining_need:
            return {"state": S_ON_ORDER, "eta": eta, "po_number": ", ".join(po_numbers),
                    "reason": f"open POs {total_outstanding} cover need {remaining_need}"}
        elif total_outstanding > 0:
            return {"state": S_PARTIAL, "shortfall": remaining_need - total_outstanding,
                    "eta": eta, "po_number": ", ".join(po_numbers),
                    "reason": f"open POs {total_outstanding} partial, need {remaining_need}"}

    # No shelf stock and no PO anywhere → urgent NO PO.
    return {"state": S_NO_PO, "shortfall": remaining_need,
            "reason": (f"shelf {inv}, in-scope demand {total_in_scope}, "
                       f"{reserved_by_others} reserved by others, "
                       f"{available} available; no open PO")}


def roll_up_jobsheet(line_states: list[dict]) -> str:
    """Worst state across ALL lines, including placeholders (MISC, FILT,
    CONS, DEALER, BATT, TYRE, PLUG, MISCD, MISCP). Placeholders carry
    state NO_PO, so a job whose only outstanding items are placeholders
    still rolls up to MISSING — an uncoded part the technician can't fit is
    a genuine missing part. If there are no lines at all the job is CLEAR.
    """
    if not line_states:
        return S_CLEAR
    return max(line_states, key=lambda s: WORST_ORDER[s["state"]])["state"]


# --- writes ---------------------------------------------------------------
def patch_extended_status(bc: BCClient, job: dict, new_code: str) -> None:
    url = gh_url(bc, "jobsheets", job["id"])
    headers = bc._headers({"If-Match": job["@odata.etag"]})
    r = requests.patch(url, headers=headers, json={"extendedStatusCode": new_code}, timeout=20)
    r.raise_for_status()


def post_missing_parts_to_board(on_site_jobs: list[dict], ran_at: dt.datetime) -> None:
    """POST the current missing-parts snapshot to the Fitch Board
    (/api/missing-parts, X-Api-Key). The board upserts each job, PRESERVES the
    human note, and removes any job no longer in the snapshot — so resolved jobs
    self-clear. Replaces the old Teams post."""
    base = os.environ.get("BOARD_URL", "").strip().rstrip("/")
    key = os.environ.get("BOARD_API_KEY", "").strip()
    if not base or not key:
        raise RuntimeError("BOARD_URL / BOARD_API_KEY not set — cannot post to board")
    payload = {
        "ran_at": ran_at.isoformat(timespec="seconds"),
        "jobs": [_board_job(j) for j in on_site_jobs],
    }
    r = requests.post(
        f"{base}/api/missing-parts",
        json=payload,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()


def _board_job(j: dict) -> dict:
    states = j["_line_states"]
    pos = sorted({s.get("po_number", "") for s in states if s.get("po_number")})
    etas = sorted({s.get("eta", "") for s in states
                   if s.get("eta") and not str(s.get("eta")).startswith("0001")})
    return {
        "jobsheet": j["number"],
        "reg": j.get("vehicleRegistrationNo") or "",
        "customer_name": j.get("sellToCustomerName") or "",
        "advisor": j.get("serviceAdvisor") or "",
        "state": j["_overall_state"],
        "parts": _missing_parts_label(j),
        "po_numbers": ", ".join(pos),
        "system_eta": etas[0] if etas else "",
        "lines": _board_lines(j),
        "newly_flagged": bool(j.get("_newly_flagged")),
    }


def _board_lines(j: dict) -> list[dict]:
    """Per-line detail so a mixed job (some lines on order, one with no PO) is
    legible on the board instead of collapsing to a single status + PO."""
    out, seen = [], set()
    missing = [x for x in j["_line_states"] if x["state"] != S_CLEAR]
    for s in sorted(missing, key=lambda x: (-WORST_ORDER.get(x["state"], 0), x["item_no"])):
        try:
            q = float(s.get("needed"))
            qty = str(int(q)) if q == int(q) else f"{q:g}"
        except (TypeError, ValueError):
            qty = str(s.get("needed"))
        desc = (s.get("description") or "").strip() or s["item_no"]
        key = (desc, qty, s["state"])
        if key in seen:
            continue
        seen.add(key)
        eta = s.get("eta") or ""
        if str(eta).startswith("0001"):
            eta = ""
        out.append({"no": s["item_no"], "desc": desc, "qty": qty, "state": s["state"],
                    "po": s.get("po_number") or "", "eta": eta})
    return out


def _missing_parts_label(j: dict) -> str:
    """Comma-joined 'description ×qty' for every non-CLEAR line on a job."""
    missing = [s for s in j["_line_states"] if s["state"] != S_CLEAR]
    seen, parts = set(), []
    for s in sorted(missing, key=lambda x: x["item_no"]):
        try:
            q = float(s.get("needed"))
            qty = str(int(q)) if q == int(q) else f"{q:g}"
        except (TypeError, ValueError):
            qty = str(s.get("needed"))
        key = (s["item_no"], qty)
        if key in seen:
            continue
        seen.add(key)
        desc = (s.get("description") or "").strip() or s["item_no"]
        parts.append(f"{desc} ×{qty}")
    return ", ".join(parts)


# --- main ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Don't write to BC")
    ap.add_argument("--no-teams", action="store_true", help="Skip Teams post")
    ap.add_argument("--jobsheet", help="Debug a single jobsheet (forces dry-run)")
    args = ap.parse_args()
    if args.jobsheet:
        args.dry_run = True
        args.no_teams = True

    bc = BCClient()
    today = dt.date.today()
    ran_at = dt.datetime.now()
    errors: list[str] = []

    # Step 1 — fetch in-scope jobsheets
    try:
        jobs = fetch_target_jobsheets(bc, today)
    except requests.HTTPError as e:
        print(f"FAIL fetching jobsheets: {e}\n{getattr(e.response, 'text', '')[:500]}")
        return 2

    if args.jobsheet:
        jobs = [j for j in jobs if j["number"] == args.jobsheet]
        if not jobs:
            print(f"Jobsheet {args.jobsheet} not in scope (must be on-site or booked next 2d).")
            return 1

    print(f"Target jobsheets: {len(jobs)} (on-site or booked {today} .. +{LOOKAHEAD_DAYS}d)")
    numbers = [j["number"] for j in jobs]

    # Step 2 — fetch all Item lines on these jobsheets
    all_lines_raw = fetch_item_lines(bc, numbers)

    # Non-inventory / service items (warranty contributions, fees, charges) are
    # not physical parts and must never show as missing — skip by BC item Type.
    _real_codes = sorted({L["No"] for L in all_lines_raw if L["No"] not in PLACEHOLDER_ITEMS})
    noninv_codes = fetch_noninventory_codes(bc, _real_codes)

    def _is_excluded(L: dict) -> bool:
        return (L["No"] in EXCLUDED_ITEMS
                or L["No"] in noninv_codes
                or is_engine_oil(L.get("Description")))

    excluded_count = sum(1 for L in all_lines_raw if _is_excluded(L))
    all_lines = [L for L in all_lines_raw if not _is_excluded(L)]
    lines_by_job: dict[str, list[dict]] = defaultdict(list)
    for L in all_lines:
        lines_by_job[L["Document_No"]].append(L)
    print(f"Total Item lines (Quantity > 0): {len(all_lines)} "
          f"({excluded_count} EXCLUDED_ITEMS skipped)")

    # Step 3 — gather supporting data
    items_in_scope = sorted({L["No"] for L in all_lines if L["No"] not in PLACEHOLDER_ITEMS})
    print(f"Distinct non-placeholder items to check: {len(items_in_scope)}")

    # Inventory levels
    print("Fetching inventory levels...")
    inventory = fetch_inventory(bc, items_in_scope)

    # In-scope demand — total qty needed across all on-site / imminent jobs
    # per item. Used to size reservations realistically. Future bookings
    # outside the lookahead window are deliberately excluded — they'll get
    # their own parts ordered separately, not drawn from today's shelf.
    in_scope_demand = compute_in_scope_demand(all_lines)

    # Open PO lines (general match by item)
    print("Fetching open PO lines...")
    po_lines_by_item = fetch_open_po_lines_by_items(bc, items_in_scope)

    # Step 4 — determine state per line, roll up per jobsheet
    specific_po_cache: dict[tuple[str, str], dict | None] = {}
    for j in jobs:
        line_states = []
        for L in lines_by_job.get(j["number"], []):
            st = determine_line_state(
                L, inventory, in_scope_demand, po_lines_by_item, specific_po_cache, bc,
            )
            st["item_no"] = L["No"]
            st["needed"] = L.get("Quantity")
            st["service_line_no"] = L["Line_No"]
            st["description"] = L.get("Description") or ""
            line_states.append(st)
        j["_line_states"] = line_states
        j["_overall_state"] = roll_up_jobsheet(line_states)

    # Step 5 — decide writes
    flag: list[dict] = []
    clear: list[dict] = []
    skip: list[dict] = []
    for j in jobs:
        current = (j.get("extendedStatusCode") or "").strip()
        overall = j["_overall_state"]
        if overall in URGENT_STATES or overall == S_ON_ORDER:
            if current == MISSING_CODE:
                skip.append(j)
            else:
                j["_overwritten"] = current
                flag.append(j)
        else:  # S_CLEAR
            if current == MISSING_CODE:
                clear.append(j)
            else:
                skip.append(j)

    # Step 6 — apply BC writes
    if not args.dry_run:
        for j in flag:
            try:
                patch_extended_status(bc, j, MISSING_CODE)
            except Exception as e:
                errors.append(f"set MISSING {j['number']}: {e}")
        for j in clear:
            try:
                patch_extended_status(bc, j, "")
            except Exception as e:
                errors.append(f"clear MISSING {j['number']}: {e}")

    flagged_ids = {j["id"] for j in flag}
    on_site_with_missing = [
        j for j in jobs
        if j.get("vehicleOnSite") and j["_overall_state"] in (S_NO_PO, S_PARTIAL, S_ON_ORDER)
    ]
    on_site_with_missing.sort(key=lambda x: (
        WORST_ORDER[x["_overall_state"]] * -1, x["number"]
    ))
    for j in on_site_with_missing:
        j["_newly_flagged"] = j["id"] in flagged_ids

    # Step 7 — console summary
    print(f"\n=== Missing Parts Monitor v2 — {ran_at.isoformat(timespec='seconds')} ===")
    print(f"Checked:     {len(jobs)}")
    print(f"Flag:        {len(flag)}  (set MISSING)")
    print(f"Clear:       {len(clear)} (clear MISSING)")
    print(f"Skip:        {len(skip)}")
    print(f"On-site MISSING (all states): {len(on_site_with_missing)}")
    state_breakdown = defaultdict(int)
    for j in on_site_with_missing:
        state_breakdown[j["_overall_state"]] += 1
    for state, count in state_breakdown.items():
        print(f"  {state}: {count}")
    print(f"Errors:      {len(errors)}")

    print("\nFlagged this run:")
    for j in flag:
        urgent_items = sorted({s["item_no"] for s in j["_line_states"] if s["state"] in URGENT_STATES})
        on_order_items = sorted({s["item_no"] for s in j["_line_states"] if s["state"] == S_ON_ORDER})
        scope = "ON SITE" if j.get("vehicleOnSite") else f"due {j.get('bookingDate','?')}"
        over = j.get("_overwritten", "")
        over_tag = f" (was {over})" if over else ""
        print(f"  + {j['number']:<10} {j.get('vehicleRegistrationNo',''):<8} "
              f"{scope:<14} state={j['_overall_state']}{over_tag}")
        for s in j["_line_states"]:
            if s["state"] != S_CLEAR:
                eta = f", ETA {s['eta']}" if s.get("eta") else ""
                po = f", PO {s['po_number']}" if s.get("po_number") else ""
                desc = (s.get("description") or "")[:34]
                print(f"      {s['item_no']:<14} {desc:<34} qty={s['needed']:<5} {s['state']:<22}{po}{eta}  -- {s['reason']}")

    if args.jobsheet:
        # Verbose dump for debug
        print(f"\n=== DEBUG dump of {args.jobsheet} ===")
        for j in jobs:
            print(json.dumps({k: v for k, v in j.items() if k != "@odata.etag"}, default=str, indent=2))

    print("\nCleared this run:")
    for j in clear:
        print(f"  - {j['number']:<10} now CLEAR (no missing parts)")

    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  ! {e}")

    if args.dry_run:
        print("\n(Dry-run mode — no BC writes performed.)")

    # Step 8 — Teams post
    if not args.dry_run and not args.no_teams:
        try:
            post_missing_parts_to_board(on_site_with_missing, ran_at)
            print("Posted missing-parts snapshot to the Fitch Board.")
        except Exception as e:
            print(f"Board post failed: {e}")
            traceback.print_exc()

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
