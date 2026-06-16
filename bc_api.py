"""
Fitch Autos — Business Central API Helper
==========================================
OAuth2 client-credentials flow for BC API v2.0 and OData web services.
Used by Cowork skills (invoice processing, VIE, enquiry triage, etc.)

Setup:
1. Complete Entra app registration (see SETUP_GUIDE.md)
2. Copy .env.example to .env and fill in your credentials
3. Run test_connection.py to verify

Usage:
    from bc_api import BCClient
    bc = BCClient()

    # Standard API v2.0
    customers = bc.get("customers", filter="displayName eq 'Smith'")
    vendors = bc.get("vendors", top=10)

    # GarageHive OData web services (queries)
    vehicles = bc.odata("GH1_Vehicles", filter="Registration_No eq 'AB12CDE'")
    service_headers = bc.odata("GH1_ServiceHeaders", top=50)

    # Create a purchase invoice
    invoice = bc.post("purchaseInvoices", {
        "vendorNumber": "V00123",
        "invoiceDate": "2026-04-09"
    })

    # Update a record (fetches ETag automatically)
    bc.patch("customers", customer_id, {"phoneNumber": "01244 123456"})
"""

import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

# Try to load .env file
ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


class BCClient:
    """Business Central API client with automatic OAuth2 token management."""

    REQUEST_TIMEOUT = (10, 60)
    TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
    # Shared on-disk token cache (added 11/06/26). Unattended Codex/Cowork runs
    # were hitting login.microsoftonline.com DNS failures and losing ALL BC data
    # for the run, even though tokens last ~1h and the triage runs every 30 min.
    # Caching the token on disk means a DNS blip at run N is covered by the
    # token fetched at run N-1. chmod 600; keyed by tenant+client.
    TOKEN_CACHE_PATH = Path(__file__).resolve().parent.parent / ".claude" / ".token_cache" / "bc_token.json"

    def __init__(self, tenant_id=None, client_id=None, client_secret=None,
                 environment="Production", company_name="FITCHAUTOS"):
        self.tenant_id = tenant_id or os.environ["BC_TENANT_ID"]
        self.client_id = client_id or os.environ["BC_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["BC_CLIENT_SECRET"]
        self.environment = environment
        self.company_name = company_name

        self.token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        self.api_base = f"https://api.businesscentral.dynamics.com/v2.0/{self.tenant_id}/{self.environment}"
        self.odata_base = f"{self.api_base}/ODataV4"
        self.api_v2_base = f"{self.api_base}/api/v2.0"
        # Garage Hive published API surface (same one the BC MCP proxy wraps,
        # but addressable directly — useful when the proxy has bugs e.g. the
        # ListJobsheetLines_PAG70420895 sub-entity URL that 404s).
        self.gh_api_base = f"{self.api_base}/api/garageHive/service/v2.0"

        self._token = None
        self._token_expiry = 0
        self._company_id = None

    def _request(self, method, url, *, retry=False, **kwargs):
        """requests.request with bounded timeout and read-safe retry support."""
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)
        attempts = 3 if retry else 1
        last_exc = None

        for attempt in range(1, attempts + 1):
            try:
                resp = requests.request(method, url, **kwargs)
                if (
                    retry
                    and attempt < attempts
                    and resp.status_code in self.TRANSIENT_STATUS_CODES
                ):
                    time.sleep(2 * attempt)
                    continue
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                if not retry or attempt >= attempts:
                    raise
                time.sleep(2 * attempt)

        if last_exc:
            raise last_exc
        raise RuntimeError("BC request retry loop exited without response")

    # ── Authentication ────────────────────────────────────────────────

    def _load_disk_token(self, min_remaining=120):
        """Return (token, expires_at) from the disk cache if still valid, else None."""
        try:
            data = json.loads(self.TOKEN_CACHE_PATH.read_text())
            if (data.get("tenant_id") == self.tenant_id
                    and data.get("client_id") == self.client_id
                    and data.get("expires_at", 0) > time.time() + min_remaining):
                return data["access_token"], data["expires_at"]
        except Exception:
            pass
        return None

    def _save_disk_token(self):
        """Best-effort persist of the current token (chmod 600). Never raises."""
        try:
            self.TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "tenant_id": self.tenant_id,
                "client_id": self.client_id,
                "access_token": self._token,
                "expires_at": self._token_expiry,
            })
            fd = os.open(str(self.TOKEN_CACHE_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(payload)
        except Exception:
            pass

    def _get_token(self):
        """Fetch or return cached OAuth2 access token (memory → disk → fetch)."""
        now = time.time()
        if self._token and now < self._token_expiry - 60:  # 60s buffer
            return self._token

        cached = self._load_disk_token(min_remaining=120)
        if cached:
            self._token, self._token_expiry = cached
            return self._token

        try:
            resp = self._request("POST", self.token_url, retry=True, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://api.businesscentral.dynamics.com/.default"
            })
            resp.raise_for_status()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            # Token endpoint unreachable (DNS / network). Last-ditch: accept any
            # disk token that hasn't actually expired yet, even inside the buffer.
            cached = self._load_disk_token(min_remaining=0)
            if cached:
                self._token, self._token_expiry = cached
                return self._token
            raise
        data = resp.json()

        self._token = data["access_token"]
        self._token_expiry = now + data.get("expires_in", 3600)
        self._save_disk_token()
        return self._token

    def _headers(self, extra=None):
        """Standard request headers with bearer token."""
        h = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        if extra:
            h.update(extra)
        return h

    # ── Company ID ────────────────────────────────────────────────────

    @property
    def company_id(self):
        """Get the company GUID (cached after first call)."""
        if self._company_id:
            return self._company_id

        resp = self._request(
            "GET",
            f"{self.api_v2_base}/companies",
            headers=self._headers(),
            retry=True,
        )
        resp.raise_for_status()
        companies = resp.json().get("value", [])

        for co in companies:
            if co["name"].upper() == self.company_name.upper():
                self._company_id = co["id"]
                return self._company_id

        # If exact match fails, try partial match
        for co in companies:
            if self.company_name.upper() in co["name"].upper():
                self._company_id = co["id"]
                return self._company_id

        names = [c["name"] for c in companies]
        raise ValueError(f"Company '{self.company_name}' not found. Available: {names}")

    # ── Standard API v2.0 ────────────────────────────────────────────

    def _build_api_url(self, entity, record_id=None):
        """Build API v2.0 URL for an entity."""
        base = f"{self.api_v2_base}/companies({self.company_id})/{entity}"
        if record_id:
            base += f"({record_id})"
        return base

    def _build_query_params(self, filter=None, select=None, expand=None,
                            orderby=None, top=None, skip=None):
        """Build OData query parameters."""
        params = {}
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = select if isinstance(select, str) else ",".join(select)
        if expand:
            params["$expand"] = expand if isinstance(expand, str) else ",".join(expand)
        if orderby:
            params["$orderby"] = orderby
        if top:
            params["$top"] = str(top)
        if skip:
            params["$skip"] = str(skip)
        return params

    def get(self, entity, record_id=None, **query_kwargs):
        """
        GET from standard API v2.0.

        Examples:
            bc.get("customers")
            bc.get("customers", filter="displayName eq 'Smith'")
            bc.get("vendors", top=10, select="number,displayName")
            bc.get("purchaseInvoices", record_id="some-guid")
        """
        url = self._build_api_url(entity, record_id)
        params = self._build_query_params(**query_kwargs)

        resp = self._request("GET", url, headers=self._headers(), params=params, retry=True)
        resp.raise_for_status()
        data = resp.json()

        if record_id:
            return data  # Single record
        return data.get("value", [])

    def get_all(self, entity, **query_kwargs):
        """
        GET all records, following @odata.nextLink pagination.
        Use with caution on large datasets — add filters.
        """
        records = []
        url = self._build_api_url(entity)
        params = self._build_query_params(**query_kwargs)

        while url:
            resp = self._request("GET", url, headers=self._headers(), params=params, retry=True)
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = {}  # nextLink includes params

        return records

    def post(self, entity, body):
        """
        POST (create) a new record.

        Example:
            bc.post("purchaseInvoices", {
                "vendorNumber": "V00123",
                "invoiceDate": "2026-04-09"
            })
        """
        url = self._build_api_url(entity)
        resp = self._request("POST", url, headers=self._headers(), json=body)
        resp.raise_for_status()
        return resp.json()

    def patch(self, entity, record_id, body):
        """
        PATCH (update) an existing record.
        Automatically fetches the current ETag for optimistic concurrency.
        """
        # Fetch current record to get ETag
        url = self._build_api_url(entity, record_id)
        current = self._request("GET", url, headers=self._headers(), retry=True)
        current.raise_for_status()
        etag = current.json().get("@odata.etag", current.headers.get("ETag", ""))

        resp = self._request(
            "PATCH",
            url,
            headers=self._headers({"If-Match": etag}),
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    def delete(self, entity, record_id):
        """
        DELETE a record. Fetches ETag automatically.
        """
        url = self._build_api_url(entity, record_id)
        current = self._request("GET", url, headers=self._headers(), retry=True)
        current.raise_for_status()
        etag = current.json().get("@odata.etag", current.headers.get("ETag", ""))

        resp = self._request(
            "DELETE",
            url,
            headers=self._headers({"If-Match": etag})
        )
        resp.raise_for_status()

    # ── OData Web Services (GarageHive queries, pages, codeunits) ──

    def odata(self, service_name, **query_kwargs):
        """
        GET from an OData V4 web service (published in BC Web Services page).

        Examples:
            bc.odata("GH1_Vehicles", filter="Registration_No eq 'AB12CDE'")
            bc.odata("GH1_ServiceHeaders", top=50)
            bc.odata("GH1_Customers", filter="Name eq 'Smith'")
        """
        url = f"{self.odata_base}/Company('{self.company_name}')/{service_name}"
        params = self._build_query_params(**query_kwargs)

        resp = self._request("GET", url, headers=self._headers(), params=params, retry=True)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def odata_all(self, service_name, **query_kwargs):
        """
        GET all records from an OData web service, following pagination.
        """
        records = []
        url = f"{self.odata_base}/Company('{self.company_name}')/{service_name}"
        params = self._build_query_params(**query_kwargs)

        while url:
            resp = self._request("GET", url, headers=self._headers(), params=params, retry=True)
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = {}

        return records

    def odata_call(self, service_name, method_name, params=None):
        """
        Call a method on an OData-published Codeunit.

        Example:
            bc.odata_call("GHV_MDB_ManagementWS", "SomeMethodName", {"param1": "value"})
        """
        url = f"{self.odata_base}/{service_name}_{method_name}"
        resp = self._request(
            "POST",
            url,
            headers=self._headers(),
            json=params or {}
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return resp.text

    # ── Claude_* custom pages (read-only, OData-only) ─────────────────
    #
    # Two custom GH pages built for automation. Both are read-only
    # (IsReadOnly=true; POST → 405) and NOT on the MCP proxy or gh_api
    # surface — OData is the only route.
    #   Claude_Items              — item-master projection carrying the full
    #                               commercial picture (Unit_Cost,
    #                               Last_Direct_Cost, Unit_Price,
    #                               Profit_Percent, posting groups, vendor,
    #                               inventory) plus the GHV_Placeholder_Item
    #                               flag. One read = everything to price a line.
    #   Claude_Convert_Placeholder — convert worklist (object 70420580),
    #                               currently empty + read-only (no writer yet).

    def claude_item(self, item_no):
        """Return the Claude_Items record for one Item No, or None.

        Single read returning cost / price / profit / posting groups /
        vendor / inventory / GHV_Placeholder_Item. Read-only. Returns None
        on any failure so callers can fall back to their existing path.
        """
        if not item_no:
            return None
        q = str(item_no).replace("'", "''")
        try:
            rows = self.odata("Claude_Items", filter=f"No eq '{q}'", top=1)
            return rows[0] if rows else None
        except Exception:
            return None

    def placeholder_item_nos(self, *, refresh=False):
        """Set of Item Nos BC flags as placeholders (GHV_Placeholder_Item).

        Cached per-instance. Lets callers detect placeholder lines from the
        live BC flag instead of a hardcoded code list, so a newly-added or
        renamed placeholder item is caught automatically. Returns an empty
        set on failure — callers should union with their own hardcoded floor.
        """
        cached = getattr(self, "_placeholder_nos_cache", None)
        if cached is not None and not refresh:
            return cached
        try:
            rows = self.odata(
                "Claude_Items",
                filter="GHV_Placeholder_Item eq true",
                select="No",
                top=500,
            )
            result = {r.get("No") for r in rows if r.get("No")}
        except Exception:
            result = set()
        self._placeholder_nos_cache = result
        return result

    # ── Garage Hive service v2.0 raw API ──────────────────────────────
    #
    # Three-tier API waterfall: MCP proxy → gh_api() → odata().
    # Use this when:
    #   - the MCP proxy can't see the entity, OR
    #   - the MCP proxy describes the action but the underlying URL 404s
    #     (e.g. ListJobsheetLines_PAG70420895 — the proxy hits
    #     /jobsheets({id})/jobsheetLines but GH only publishes the flat
    #     root /jobsheetLines?$filter=documentNo eq 'X').
    #
    # Entities published on this surface (verified 2026-05-13 via $metadata):
    #   estimates, estimateGroups, estimateLines,
    #   jobsheets, jobsheetGroups, jobsheetLines,
    #   serviceComments,
    #   vehicleInspectionEstimates, vehicleInspectionEstimateGroups,
    #     vehicleInspectionEstimateLines,
    #   companies
    #
    # NOT on this surface (still OData-only):
    #   vehicles, checklists, GHV_BI1_ServiceTimeLog,
    #   GH1_PostedJobsheetHeaders, items, labours, makes, models,
    #   posted purchase invoices, item ledger entries
    #   (for those, use bc.get() / MCP proxy / bc.odata() as appropriate).

    def _build_gh_api_url(self, entity, record_id=None):
        """Build Garage Hive service v2.0 raw API URL for an entity."""
        base = f"{self.gh_api_base}/companies({self.company_id})/{entity}"
        if record_id:
            base += f"({record_id})"
        return base

    def gh_api(self, entity, record_id=None, **query_kwargs):
        """
        GET from the Garage Hive service v2.0 raw API.

        Examples:
            bc.gh_api("jobsheetLines", filter=f"documentNo eq '{n}'", top=200)
            bc.gh_api("serviceComments", filter="documentType eq 'VIE'")
            bc.gh_api("jobsheets", filter="bookingDate eq 2026-05-14")
        """
        url = self._build_gh_api_url(entity, record_id)
        params = self._build_query_params(**query_kwargs)

        resp = requests.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        data = resp.json()

        if record_id:
            return data
        return data.get("value", [])

    def gh_api_all(self, entity, **query_kwargs):
        """GET all records from the GH service v2.0 raw API, following pagination."""
        records = []
        url = self._build_gh_api_url(entity)
        params = self._build_query_params(**query_kwargs)

        while url:
            resp = requests.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = {}

        return records

    def gh_api_post(self, entity, body):
        """
        POST (create) a new record on the GH service v2.0 raw API.

        Example:
            bc.gh_api_post("serviceComments", {
                "documentType": "VIE",
                "documentNo": "VHC012345",
                "comment": "OFFICE: ...",
                "extendedComment": "long body text..."
            })
        """
        url = self._build_gh_api_url(entity)
        resp = requests.post(url, headers=self._headers(), json=body)
        resp.raise_for_status()
        return resp.json()

    def gh_api_patch(self, entity, record_id, body):
        """
        PATCH (update) an existing record on the GH service v2.0 raw API.
        Automatically fetches the current ETag for optimistic concurrency.
        """
        url = self._build_gh_api_url(entity, record_id)
        current = requests.get(url, headers=self._headers())
        current.raise_for_status()
        etag = current.json().get("@odata.etag", current.headers.get("ETag", ""))

        resp = requests.patch(
            url,
            headers=self._headers({"If-Match": etag}),
            json=body
        )
        resp.raise_for_status()
        return resp.json()

    def gh_api_delete(self, entity, record_id):
        """DELETE a record from the GH service v2.0 raw API. Fetches ETag automatically."""
        url = self._build_gh_api_url(entity, record_id)
        current = requests.get(url, headers=self._headers())
        current.raise_for_status()
        etag = current.json().get("@odata.etag", current.headers.get("ETag", ""))

        resp = requests.delete(
            url,
            headers=self._headers({"If-Match": etag})
        )
        resp.raise_for_status()

    # ── Service comment chunked writer (80-char workaround) ───────────
    #
    # BC service comment lines are hard-capped at 80 chars on the `comment`
    # field across all API routes (MCP, gh_api, OData — verified 2026-05-13).
    # `extendedComment` is read-only via the API. The only way to get long
    # content in is to split it into ~75-char chunks and write each as its
    # own comment line with an incrementing lineNo.
    #
    # Also: every create MUST pass `typeCode` explicitly. If left blank, BC
    # defaults to "1 📕 BOOKED" and overwrites your text with the template
    # body. typeCode='OFFICE' is the canonical value for skill-written
    # audit-trail comments (officeOnly=True, doesn't print on docs).

    def _split_comment_chunks(self, body: str, max_chars: int = 75):
        """Split a long string into chunks <= max_chars at word boundaries.

        Preserves explicit line breaks (\\n) — each line is chunked
        separately. Returns a list of strings, each <= 80 chars total (we
        leave a 5-char headroom under BC's 80-char limit for safety).
        """
        chunks = []
        for line in (body or '').split('\n'):
            if not line.strip():
                chunks.append('')
                continue
            words = line.split(' ')
            current = ''
            for w in words:
                # If a single word is itself longer than max_chars, hard-break it
                while len(w) > max_chars:
                    if current:
                        chunks.append(current)
                        current = ''
                    chunks.append(w[:max_chars])
                    w = w[max_chars:]
                # Try to add the next word to the current chunk
                if not current:
                    current = w
                elif len(current) + 1 + len(w) <= max_chars:
                    current = current + ' ' + w
                else:
                    chunks.append(current)
                    current = w
            if current:
                chunks.append(current)
        return chunks

    def create_service_comment_chunked(self, document_type: str, document_no: str,
                                       body: str, type_code: str = 'OFFICE',
                                       office_only: bool = True,
                                       print_on_document: bool = False,
                                       attention: bool = False,
                                       start_line_no: int = None,
                                       max_chars: int = 75):
        """DEPRECATED (2026-06-09) — write narrative via set_extended_comment().

        Superseded by `set_extended_comment()`, which uses the
        `Microsoft.NAV.setComment` bound action to write the VISIBLE Extended
        Comment field as ONE row of full text (no 80-char cap, no multi-row
        clutter, replace-in-place). This chunked writer splits a body into
        ~75-char `Comment`-field rows and leaves the Extended field blank/short,
        so the BC Comments panel under-shows the content. Do NOT use it for new
        comment writes — prefer `set_extended_comment(...)`. Retained only for
        any legacy caller that genuinely needs separate short comment rows.

        Write a long body as a series of service comment lines on a BC document.
        Three-tier fallback: gh_api_post -> odata_post on `Service_Comment_Lines`.
        Returns the list of created comment dicts (or just ids on the OData path).
        Always passes typeCode explicitly to avoid the default-template trap.
        """
        chunks = self._split_comment_chunks(body, max_chars=max_chars)
        if not chunks:
            return []

        # Allocate lineNos. If start_line_no isn't supplied, find the next
        # free lineNo above any existing comments on this document.
        if start_line_no is None:
            try:
                existing = self.gh_api(
                    'serviceComments',
                    filter=f"documentType eq '{document_type}' and documentNo eq '{document_no}'",
                    select='lineNo', top=500,
                )
                max_existing = max((c.get('lineNo') or 0) for c in existing) if existing else 0
                start_line_no = ((max_existing // 10000) + 1) * 10000
            except Exception:
                # Fall back to a default starting point
                start_line_no = 900000

        created = []
        for i, chunk in enumerate(chunks):
            line_no = start_line_no + (i * 10000)
            payload = {
                'documentType':    document_type,
                'documentNo':      document_no,
                'lineNo':          line_no,
                'comment':         chunk,
                'typeCode':        type_code,
                'officeOnly':      office_only,
                'printOnDocument': print_on_document,
                'attention':       attention,
            }
            # Tier 2 — GH service v2.0 raw
            try:
                row = self.gh_api_post('serviceComments', payload)
                created.append(row)
                continue
            except Exception as e_t2:
                # Tier 3 — OData direct write to Service_Comment_Lines.
                # Field names differ on the OData route.
                try:
                    odata_url = (f"{self.odata_base}/Company('{self.company_name}')"
                                 f"/Service_Comment_Lines")
                    od_payload = {
                        'DocumentType':    document_type,
                        'DocumentNo':      document_no,
                        'LineNo':          line_no,
                        'Comment':         chunk,
                        'TypeCode':        type_code,
                        'OfficeOnly':      office_only,
                        'PrintOnDocument': print_on_document,
                        'Attention':       attention,
                    }
                    resp = requests.post(odata_url, headers=self._headers(),
                                         json=od_payload)
                    resp.raise_for_status()
                    created.append(resp.json())
                except Exception as e_t3:
                    raise RuntimeError(
                        f"Both tiers failed to write comment line {line_no} "
                        f"on {document_type}/{document_no}. "
                        f"T2: {e_t2}. T3: {e_t3}"
                    ) from e_t3

        return created

    # ── Extended comment writer (Microsoft.NAV.setComment) ────────────
    #
    # Verified 2026-06-09: the bound action `Microsoft.NAV.setComment` on the
    # GH service v2.0 `serviceComments` entity DOES write the visible multi-line
    # Extended Comment field (the one the BC UI shows) — overturning the earlier
    # "not API-writable" finding. Mechanism:
    #     POST .../serviceComments({id})/Microsoft.NAV.setComment
    #          {"newComment": "..."}  -> HTTP 200
    # Behaviour confirmed on a live estimate:
    #   - extendedComment accepts large text (5000+ chars round-tripped intact);
    #     the 80-char cap only applies to the short `comment` field.
    #   - Each call REPLACES the whole field (not append).
    #   - Line breaks (\n, \r\n, U+2028, vert tab) do NOT render in the BC UI —
    #     BC shows one continuous block. Use ' • ' inline separators instead
    #     (see format_comment()). This matches GH's own dash-separated templates.
    #   - Tier-2 only: the MCP proxy does not expose the bound action.
    #
    # Canonical AI comment-type codes (created in BC, all Office-Only — never
    # printed on customer documents). One line per type per document, rewritten
    # in place on each run.
    AI_COMMENT_TYPES = {
        "enquiry":   "AI ENQUIRY",    # full customer enquiry (enquiry-to-estimate)
        "triage":    "AI TRIAGE",     # 6-criteria verdict (enquiry-to-estimate)
        "gp":        "AI GP",         # GP / cross-sell potential per triage
        "est_build": "AI EST",        # estimate build narrative
        "est_parts": "AI EST PTS",    # estimate parts detail
        "vie_build": "AI VIE BUILD",  # VIE build / audit narrative (vie-processor)
        "vie_parts": "AI VIE PARTS",  # VIE parts research (vie-processor)
        "js_brief":  "AI JS BRIEF",   # jobsheet pre-visit briefing (one comment)
    }

    @staticmethod
    def format_comment(points, lead_bullet=True):
        """Join point strings into one BC-safe line for an extended comment.

        BC's extendedComment strips real line breaks, so points are separated
        by ' • ' on a single flowing line. Each point's internal whitespace /
        newlines are collapsed to single spaces. Empty/None points are dropped.

            format_comment(["Front pads + discs", "Pagid via Omnipart 84.50"])
            -> "• Front pads + discs • Pagid via Omnipart 84.50"
        """
        parts = []
        for p in points or []:
            if p is None:
                continue
            s = " ".join(str(p).split())
            if s:
                parts.append(s)
        if not parts:
            return ""
        joined = " • ".join(parts)
        return ("• " + joined) if lead_bullet else joined

    # A predetermined run of spaces used as a pseudo-paragraph break between
    # major sections. BC's comment box renders consecutive spaces as a real
    # horizontal gap (verified 2026-06-09 — true line breaks are impossible
    # through the API), and at 20 wide it usually also wraps the next section
    # onto a fresh line. Used by format_sections().
    SECTION_GAP = " " * 20

    @staticmethod
    def format_sections(sections, gap=None):
        """Compose a sectioned extended comment: CAPS-tagged blocks separated by
        a wide space gap (SECTION_GAP, default 20 spaces), with ' • ' between
        items inside each block. The gap is PRESERVED (unlike format_comment,
        which collapses all whitespace) — so pass the RESULT straight to
        set_extended_comment as a pre-formatted string.

        `sections` is a list whose items are either:
          - ("TAG", [points])  -> "TAG: pt • pt"   (TAG="" → no label), or
          - "already-formed block string"          (internal whitespace collapsed).

            format_sections([
                ("JUDGEMENT", ["upgraded A/C to required"]),
                ("SKIPPED",   ["washer jet — covered by booking"]),
                ("NEXT",      ["link orphans", "confirm cambelt time"]),
            ])
            -> "JUDGEMENT: upgraded A/C to required<gap>SKIPPED: washer jet — covered
                by booking<gap>NEXT: link orphans • confirm cambelt time"
        """
        g = BCClient.SECTION_GAP if gap is None else gap
        blocks = []
        for sec in sections or []:
            if (isinstance(sec, (list, tuple)) and len(sec) == 2
                    and not isinstance(sec[0], (list, tuple))):
                tag, pts = sec
                inner = (BCClient.format_comment(pts, lead_bullet=False)
                         if isinstance(pts, (list, tuple))
                         else " ".join(str(pts).split()))
                tag = " ".join(str(tag).split())
                block = f"{tag}: {inner}" if tag else inner
            else:
                block = " ".join(str(sec).split())
            if block.strip():
                blocks.append(block)
        return g.join(blocks)

    def set_extended_comment(self, document_type, document_no, type_code, text,
                             *, office_only=True, print_on_document=False,
                             attention=False, line_no=None):
        """Create-or-replace one service comment line of `type_code` on a
        document and set its visible Extended Comment via Microsoft.NAV.setComment.

        Replace-in-place: if a line with this typeCode already exists on the
        document, the lowest-lineNo match is reused; otherwise a new line is
        created at the next free 10000 boundary. The bound action then OVERWRITES
        the text, so re-running a skill keeps exactly one current line per type.

        `text` should already be a single line (use format_comment() to build it
        — line breaks won't render in BC). Returns the service comment id.

        Tier-2 (gh_api) only — the MCP proxy does not expose the bound action.
        Raises on failure so callers can flag rather than half-write.
        """
        if text is None:
            text = ""

        existing = self.gh_api(
            "serviceComments",
            filter=f"documentType eq '{document_type}' and documentNo eq '{document_no}'",
            top=200,
        )
        match = sorted(
            (c for c in existing if (c.get("typeCode") or "") == type_code),
            key=lambda c: c.get("lineNo") or 0,
        )
        if match:
            sc_id = match[0]["id"]
        else:
            if line_no is None:
                max_existing = max((c.get("lineNo") or 0) for c in existing) if existing else 0
                line_no = ((max_existing // 10000) + 1) * 10000
            created = self.gh_api_post("serviceComments", {
                "documentType":    document_type,
                "documentNo":      document_no,
                "lineNo":          line_no,
                "typeCode":        type_code,
                "officeOnly":      office_only,
                "printOnDocument": print_on_document,
                "attention":       attention,
            })
            sc_id = created["id"]

        url = (f"{self.gh_api_base}/companies({self.company_id})"
               f"/serviceComments({sc_id})/Microsoft.NAV.setComment")
        resp = requests.post(url, headers=self._headers(), json={"newComment": text})
        resp.raise_for_status()
        return sc_id

    # ── Convenience Methods ───────────────────────────────────────────

    def find_customer(self, name=None, email=None, phone=None):
        """Search for a customer by name, email, or phone."""
        filters = []
        if name:
            filters.append(f"contains(displayName, '{name}')")
        if email:
            filters.append(f"email eq '{email}'")
        if phone:
            filters.append(f"phoneNumber eq '{phone}'")

        return self.get("customers", filter=" or ".join(filters) if len(filters) > 1 else filters[0])

    def find_vendor(self, name=None, number=None):
        """Search for a vendor by name or number."""
        if number:
            return self.get("vendors", filter=f"number eq '{number}'")
        if name:
            return self.get("vendors", filter=f"contains(displayName, '{name}')")
        return []

    def find_item(self, number=None, description=None):
        """Search for an item by number or description."""
        if number:
            return self.get("items", filter=f"number eq '{number}'")
        if description:
            return self.get("items", filter=f"contains(displayName, '{description}')")
        return []

    def find_vehicle(self, registration=None, vin=None):
        """Search GarageHive vehicles by registration or VIN."""
        if registration:
            reg = registration.upper().replace(" ", "")
            return self.odata("GH1_Vehicles", filter=f"Registration_No eq '{reg}'")
        if vin:
            return self.odata("GH1_Vehicles", filter=f"VIN eq '{vin}'")
        return []

    def get_service_history(self, customer_no=None, vehicle_reg=None, top=20):
        """Get recent service headers from GarageHive."""
        filters = []
        if customer_no:
            filters.append(f"Customer_No eq '{customer_no}'")
        if vehicle_reg:
            filters.append(f"Vehicle_Registration_No eq '{vehicle_reg}'")

        filter_str = " and ".join(filters) if filters else None
        return self.odata("GH1_ServiceHeaders", filter=filter_str, top=top,
                         orderby="Order_Date desc")

    def create_purchase_invoice(self, vendor_number, invoice_date, lines=None):
        """
        Create a purchase invoice with optional lines.

        Args:
            vendor_number: BC vendor number (e.g. "V00123")
            invoice_date: Date string "YYYY-MM-DD"
            lines: List of dicts with keys like:
                   {"itemId": "...", "quantity": 1, "unitCost": 25.50}

        Returns: Created invoice record
        """
        invoice = self.post("purchaseInvoices", {
            "vendorNumber": vendor_number,
            "invoiceDate": invoice_date
        })

        if lines:
            inv_id = invoice["id"]
            for line in lines:
                self.post(f"purchaseInvoices({inv_id})/purchaseInvoiceLines", line)

        return invoice

    # ── Diagnostics ───────────────────────────────────────────────────

    def test_connection(self):
        """Test the API connection and return diagnostic info."""
        results = {"timestamp": datetime.now(timezone.utc).isoformat()}

        # Test token
        try:
            token = self._get_token()
            results["auth"] = "OK"
            results["token_preview"] = token[:20] + "..."
        except Exception as e:
            results["auth"] = f"FAILED: {e}"
            return results

        # Test company lookup
        try:
            cid = self.company_id
            results["company"] = {"name": self.company_name, "id": cid}
        except Exception as e:
            results["company"] = f"FAILED: {e}"
            return results

        # Test standard API
        try:
            customers = self.get("customers", top=1)
            results["api_v2"] = f"OK — {len(customers)} customer(s) returned"
        except Exception as e:
            results["api_v2"] = f"FAILED: {e}"

        # Test OData
        try:
            vehicles = self.odata("GH1_Vehicles", top=1)
            results["odata_gh"] = f"OK — {len(vehicles)} vehicle(s) returned"
        except Exception as e:
            results["odata_gh"] = f"FAILED: {e}"

        return results


if __name__ == "__main__":
    bc = BCClient()
    results = bc.test_connection()
    print(json.dumps(results, indent=2))
