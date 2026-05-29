"""
PSP - Payer App Stub
Simulates the payer's smartphone app. Fetches payment request details
from the PISP and submits consent or decline on behalf of the payer.

Serves two interfaces:
- /ui/*         HTMX mobile-viewport web UI (phone experience demo)
- /app/*        REST API (used by automated tests and the convenience endpoints)
- /admin/*      Observability
"""

import base64
import hashlib
import json as _json
import logging
import os
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse
from uuid import UUID

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("payer-app-stub")

app = FastAPI(
    title="PSP - Payer App Stub",
    version="0.1.0",
    description=(
        "Simulates the payer's smartphone app. Fetches payment request details "
        "from the PISP and submits consent or decline on behalf of the payer. "
        "In a real implementation this would be a mobile app — here it is both "
        "a browsable HTMX UI (/ui/) and a REST API (/app/) for test automation."
    ),
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PISP_URL           = os.getenv("PISP_URL", "http://pisp:8000")
PAYER_DISPLAY_NAME = os.getenv("PAYER_DISPLAY_NAME", "Alice")

# Identity resolution — three priority levels (highest to lowest):
#
#   1. PAYER_API_KEY — pre-provisioned by stubs.py via portal machine API.
#      Stub calls GET /payer/account at startup to resolve payer_uri.
#      This is the preferred mode for dynamic stub discovery (Option C).
#
#   2. PAYER_EMAIL + PAYER_PASSWORD — self-registration credentials.
#      Stub calls POST /payer/register (or POST /payer/auth on 409).
#
#   3. Static PAYER_URI — legacy fallback when no credentials are configured.
PAYER_API_KEY  = os.getenv("PAYER_API_KEY", "")
PAYER_EMAIL    = os.getenv("PAYER_EMAIL", "")
PAYER_PASSWORD = os.getenv("PAYER_PASSWORD", "")

# Static fallback URI — overridden by registration response when registration succeeds.
_PAYER_URI_DEFAULT = os.getenv("PAYER_URI", "psp://pisp.openpisp.local/payer/alice")

PSP_SIGNING_ENABLED: bool = os.getenv("PSP_SIGNING_ENABLED", "false").lower() not in (
    "false", "0", "no",
)

# ---------------------------------------------------------------------------
# Runtime state (populated at startup)
# ---------------------------------------------------------------------------

_payer_uri: str = _PAYER_URI_DEFAULT   # may be updated by _register_with_pisp()
_payer_api_key: str = ""               # Bearer token from registration / auth
_app_private_key = None                # EllipticCurvePrivateKey (signing, if enabled)
_app_kid: str = ""
_app_uri: str = ""
_mandates_supported: bool = False      # populated at startup from GET /payer/capabilities


async def _register_with_pisp() -> None:
    """Resolve this payer stub's identity against the PISP.

    Three modes, in priority order:

    1. ``PAYER_API_KEY`` set — key pre-provisioned by stubs.py via portal
       machine API.  Calls ``GET /payer/account`` to retrieve payer_uri.
    2. ``PAYER_EMAIL`` + ``PAYER_PASSWORD`` set — authenticates via
       ``POST /payer/auth``.  Payer account must already exist on the PISP.
    3. Neither set — uses the static ``PAYER_URI`` env var (legacy fallback).
    """
    global _payer_uri, _payer_api_key
    import asyncio

    # ── Mode 1: pre-provisioned API key ────────────────────────────────────
    if PAYER_API_KEY:
        _payer_api_key = PAYER_API_KEY
        log.info("Payer API key pre-configured — fetching account details from PISP")
        for attempt in range(1, 6):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{PISP_URL}/payer/account",
                        headers={"Authorization": f"Bearer {PAYER_API_KEY}"},
                        timeout=10.0,
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    _payer_uri = data["payer_uri"]
                    log.info("Payer account resolved: uri=%s", _payer_uri)
                    return
                log.warning(
                    "Attempt %d: GET /payer/account returned HTTP %s",
                    attempt, resp.status_code,
                )
            except Exception as exc:
                log.warning(
                    "Attempt %d: could not reach PISP for payer account: %s",
                    attempt, exc,
                )
            if attempt < 5:
                await asyncio.sleep(2 * attempt)   # 2 s, 4 s, 6 s, 8 s
        log.error(
            "Could not resolve payer account after 5 attempts — using static _payer_uri %s",
            _payer_uri,
        )
        return

    # ── Mode 2: email/password auth ────────────────────────────────────────
    if not PAYER_EMAIL or not PAYER_PASSWORD:
        log.info("Payer auth: no credentials configured — using static PAYER_URI env var")
        return

    for attempt in range(1, 6):
        try:
            async with httpx.AsyncClient() as client:
                auth = await client.post(
                    f"{PISP_URL}/payer/auth",
                    json={"email": PAYER_EMAIL, "password": PAYER_PASSWORD},
                    timeout=10.0,
                )
            if auth.status_code == 200:
                data = auth.json()
                _payer_uri     = data["payer_uri"]
                _payer_api_key = data["api_key"]
                log.info("Payer authenticated: uri=%s", _payer_uri)
                return
            log.warning("Attempt %d: payer auth returned HTTP %s: %s",
                        attempt, auth.status_code, auth.text[:200])
        except Exception as exc:
            log.warning("Attempt %d: could not reach PISP for payer auth: %s", attempt, exc)
        if attempt < 5:
            await asyncio.sleep(2 * attempt)   # 2 s, 4 s, 6 s, 8 s

    log.error("Payer auth failed after 5 attempts — using static _payer_uri %s", _payer_uri)


async def _fetch_pisp_capabilities() -> None:
    """Fetch feature capabilities from the PISP and update module-level flags.

    Calls ``GET /payer/capabilities`` (Protocol C).  Silently skips on any
    network error so a misconfigured or older PISP doesn't prevent startup.
    """
    global _mandates_supported
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PISP_URL}/payer/capabilities",
                timeout=10.0,
            )
        if resp.status_code == 200:
            caps = resp.json()
            _mandates_supported = bool(caps.get("mandates_supported", False))
            log.info(
                "PISP capabilities fetched: mandates_supported=%s", _mandates_supported
            )
        else:
            log.warning(
                "GET /payer/capabilities returned HTTP %s — mandates disabled",
                resp.status_code,
            )
    except Exception as exc:
        log.warning(
            "Could not fetch PISP capabilities: %s — mandates disabled", exc
        )


@app.on_event("startup")
async def _startup():
    """Run registration, signing credential fetch, and payment history restore at startup."""
    import asyncio

    # Registration runs first so that _payer_uri and _payer_api_key are resolved.
    await _register_with_pisp()
    await _fetch_pisp_capabilities()
    await _register_app_credential()
    await _restore_payment_history()


async def _restore_payment_history() -> None:
    """Populate payment_history from the PISP ledger on startup.

    Calls GET /payer/account/payments with the payer Bearer token so the
    history tab survives a stub restart without losing knowledge of past payments.
    Best-effort: if the PISP is unreachable or has no ledger DB, silently skips.
    """
    import asyncio
    if not _payer_api_key:
        log.info("No payer API key available — skipping payment history restore")
        return

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{PISP_URL}/payer/account/payments",
                    params={"limit": 200},
                    headers={"Authorization": f"Bearer {_payer_api_key}"},
                    timeout=10.0,
                )
            if resp.status_code == 200:
                payments = resp.json().get("payments", [])
                for p in payments:
                    pr_id = p.get("payment_request_id")
                    if pr_id and pr_id not in payment_history:
                        payment_history[pr_id] = p
                log.info("Restored %d payment(s) from PISP ledger", len(payments))
                return
            log.warning("Attempt %d: PISP returned HTTP %s for payer payments", attempt, resp.status_code)
        except Exception as exc:
            log.warning("Attempt %d: could not fetch payment history from PISP: %s", attempt, exc)
        if attempt < 3:
            await asyncio.sleep(2 * attempt)

    log.warning("Could not restore payer payment history from PISP — starting with empty cache")


async def _register_app_credential():
    """Generate a local EC key pair and register the public key with the PISP.

    Replaces the old ``_fetch_app_credential`` approach where the PISP generated
    the key and exposed the private key via an admin endpoint.  The private key
    now never leaves this process.
    """
    global _app_private_key, _app_kid, _app_uri
    if not PSP_SIGNING_ENABLED:
        return
    import asyncio
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat,
    )

    # Generate a fresh EC P-256 key pair locally — the private key stays here.
    _app_private_key = _ec.generate_private_key(_ec.SECP256R1())
    pub_pem = _app_private_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    _app_uri = f"{_payer_uri}/app" if _payer_uri else f"psp://unknown/app"

    resp = None
    for attempt in range(1, 6):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{PISP_URL}/payer/app-register",
                    json={"public_key_pem": pub_pem, "app_uri": _app_uri},
                    timeout=10.0,
                )
            if resp.status_code == 201:
                break
            if resp.status_code == 409:
                # Signing not enabled on this PISP — ok, just leave signing disabled.
                log.info("PISP signing not enabled — running without device signing")
                _app_private_key = None
                return
            log.warning(
                "Attempt %d: PISP returned HTTP %s for app-register",
                attempt, resp.status_code,
            )
        except Exception as exc:
            log.warning("Attempt %d: Could not connect to PISP for app-register: %s", attempt, exc)
            resp = None
        if attempt < 5:
            await asyncio.sleep(2 * attempt)   # 2 s, 4 s, 6 s, 8 s

    if resp is None or resp.status_code != 201:
        log.error("Could not register app credential with PISP after 5 attempts — signing disabled")
        _app_private_key = None
        return

    _app_kid = resp.json()["kid"]
    log.info("App credential registered: kid=%s uri=%s", _app_kid, _app_uri)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign_payer_request(body_dict: dict) -> dict:
    """Return request headers for a payer Protocol C call.

    Always includes ``Authorization: Bearer <api_key>`` when a key is available
    (closes T07/T15 — PISP derives payer_uri from the authenticated account).
    Also includes ``X-PSP-Signature`` when device signing is enabled.
    """
    headers: dict = {}
    if _payer_api_key:
        headers["Authorization"] = f"Bearer {_payer_api_key}"
    if _app_private_key is None:
        return headers
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    header = _b64url(
        _json.dumps({"alg": "ES256", "kid": _app_kid, "iss": _app_uri},
                    sort_keys=True, separators=(",", ":")).encode()
    )
    canonical = _json.dumps(body_dict, sort_keys=True, separators=(",", ":")).encode()
    payload = _b64url(hashlib.sha256(canonical).digest())
    signing_input = f"{header}.{payload}".encode()
    sig = _app_private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    headers["X-PSP-Signature"] = f"{header}.{payload}.{_b64url(sig)}"
    return headers

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

def _tpl_ctx(request: Request, **kwargs):
    """Base context injected into every template render."""
    return {
        "request": request,
        "payer_name": PAYER_DISPLAY_NAME,
        "mandates_supported": _mandates_supported,
        **kwargs,
    }


def _make_fiat_amount(pence: int, asset_code: str = "GBP") -> dict:
    """Build a fiat Amount dict from a minor-unit integer (e.g. pence)."""
    symbols = {"GBP": "£", "EUR": "€", "USD": "$"}
    symbol = symbols.get(asset_code, asset_code + " ")
    return {
        "value":       pence,
        "asset_kind":  "fiat",
        "asset_code":  asset_code,
        "minor_units": 2,
        "display":     f"{symbol}{pence / 100:.2f}",
    }


def _parse_qr(raw: str) -> dict:
    """
    Parse a QR payload string into a dict of known fields.

    Accepts two input forms:
      1. Full QR URI:   psp://pay?request_id=<uuid>&pisp=psp://...&pisp_url=https://...&amt=1250&cur=GBP&ref=Table+4&to=Acme+Coffee
      2. Bare UUID:     xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

    Returns a dict with keys: request_id, pisp_uri, pisp_url, amount_pence,
    asset_code, reference, requester_display_name.  Any missing field is None.
    """
    raw = raw.strip()
    if raw.startswith("psp://") or "?" in raw:
        # Treat as a URI — prepend scheme if needed so urlparse works
        if not raw.startswith(("http://", "https://", "psp://")):
            raw = "psp://" + raw
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query, keep_blank_values=False)

        def _first(key: str) -> Optional[str]:
            vals = qs.get(key)
            return vals[0] if vals else None

        amt_raw = _first("amt")
        return {
            "request_id": _first("request_id"),
            "qr_token":   _first("qr_token"),
            "mandate_id":  _first("mandate_id"),
            "pisp_uri":   _first("pisp"),
            "pisp_url":   _first("pisp_url"),
            "amount_pence": int(amt_raw) if amt_raw and amt_raw.isdigit() else None,
            "asset_code":   _first("cur") or "GBP",
            "reference":  _first("ref"),
            "requester_display_name": _first("to"),
        }
    else:
        # Treat the whole string as a bare payment_request_id
        return {
            "request_id": raw,
            "qr_token":   None,
            "mandate_id":  None,
            "pisp_uri":   None,
            "pisp_url":   None,
            "amount_pence": None,
            "asset_code":   "GBP",
            "reference":  None,
            "requester_display_name": None,
        }


def _pence_to_display(pence: int, asset_code: str = "GBP") -> str:
    """Format pence as a display string, e.g. 1250 → '£12.50'."""
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(asset_code, asset_code + " ")
    return f"{symbol}{pence / 100:.2f}"


# ---------------------------------------------------------------------------
# In-memory payment history
# ---------------------------------------------------------------------------

# payment_request_id (str) → outcome record (dict)
payment_history: dict[str, dict] = {}


async def _enrich_history_entry(payment_request_id: str) -> None:
    """Fetch full payment details from the PISP and merge into payment_history.

    The consent response only contains status/payer_token/settlement_ref.
    This call fills in amount, reference, display_name (from requester_display_name)
    so the History tab can display them.  Silently no-ops on any error.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PISP_URL}/requester/requests/{payment_request_id}",
                timeout=5.0,
            )
        if resp.status_code == 200:
            fresh = resp.json()
            # Normalise requester_display_name → display_name for the history template.
            if "requester_display_name" in fresh and "display_name" not in fresh:
                fresh["display_name"] = fresh["requester_display_name"]
            existing = dict(payment_history.get(payment_request_id, {}))
            # Fresh data wins for all keys except status — keep consent status
            # so DISPATCHED entries stay DISPATCHED until the polling confirms SETTLED.
            incoming_status = fresh.pop("status", None)
            merged = {**existing, **fresh}
            if incoming_status and existing.get("status") in (None, "DISPATCHED"):
                merged["status"] = incoming_status
            payment_history[payment_request_id] = merged
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Admin"])
def health():
    return {
        "status": "ok",
        "component": "payer-app-stub",
        "payer_uri": _payer_uri,
        "pisp_url": PISP_URL,
    }


# ---------------------------------------------------------------------------
# UI routes (HTMX / browser)
# ---------------------------------------------------------------------------

@app.get("/ui/", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_home(request: Request, qr: Optional[str] = None):
    """
    Mobile scan & pay home screen.

    Accepts an optional `qr` query param which can be either:
    - A full QR URI:  psp://pay?request_id=...&pisp=...&pisp_url=...&amt=...&ref=...&to=...
    - A bare UUID:    xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

    When provided the input field is pre-filled for the user.
    """
    return templates.TemplateResponse(
        request,
        "scan.html",
        _tpl_ctx(request, prefill=qr, error=None),
    )


@app.get("/ui/review", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_review(request: Request, qr: str):
    """
    HTMX partial: resolve a payment URI and show the review screen.

    Accepts:
      - A full ``psp://`` URI  (e.g. ``psp://pisp.openpisp.local/pay/...``)
      - A bare UUID            (treated as a same-PISP payment request ID)

    The URI is passed opaquely to the payer PISP's ``POST /payer/resolve``
    endpoint.  The payer app never parses path segments beyond the host.

    Mandate URIs (``psp://{host}/mandate/{id}``) are routed to the mandate
    draw flow instead of the normal payment review.
    """
    from urllib.parse import urlparse as _urlparse

    raw = (qr or "").strip()

    # Bare UUID → build a full psp:// URI using the local PISP host
    _uuid_re = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    if re.match(_uuid_re, raw, re.IGNORECASE):
        pisp_host = PISP_URL.replace("https://", "").replace("http://", "").rstrip("/")
        raw = f"psp://{pisp_host}/pay/{raw}"

    if not raw.startswith("psp://"):
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Invalid URI", detail="Expected a psp:// URI or bare UUID.", retry_url="/ui/"),
            status_code=400,
        )

    # Mandate URIs: draw against the mandate immediately (UC4)
    _parsed = _urlparse("http://" + raw[len("psp://"):])
    _parts = [p for p in _parsed.path.split("/") if p]
    if len(_parts) >= 2 and _parts[0] == "mandate":
        mandate_id = _parts[1]
        _iou_body = {"presenter_uri": _payer_uri}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{PISP_URL}/payer/mandates/{mandate_id}/draw",
                json=_iou_body,
                headers=_sign_payer_request(_iou_body),
                timeout=30.0,
            )
        if resp.status_code == 200:
            outcome = resp.json()
            payment_history[str(outcome.get("payment_request_id", "iou"))] = outcome
            return templates.TemplateResponse(
                request, "iou.html",
                _tpl_ctx(request, mode="result", outcome=outcome, error=None),
            )
        detail = resp.json().get("detail", resp.text) if resp.content else resp.text
        return templates.TemplateResponse(
            request, "error.html",
            _tpl_ctx(request, title="IOU Error", detail=detail, retry_url="/ui/iou"),
            status_code=resp.status_code,
        )

    # All other URIs: resolve via POST /payer/resolve — URI is opaque to the stub
    resolve_body = {"uri": raw}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PISP_URL}/payer/resolve",
            json=resolve_body,
            headers=_sign_payer_request(resolve_body),
            timeout=10.0,
        )

    if resp.status_code == 404:
        return templates.TemplateResponse(
            request, "error.html",
            _tpl_ctx(request, title="Not Found", detail="Payment request not found.", retry_url="/ui/"),
            status_code=404,
        )
    if resp.status_code == 410:
        return templates.TemplateResponse(
            request, "error.html",
            _tpl_ctx(request, title="Expired", detail="This payment request has expired.", retry_url="/ui/"),
            status_code=410,
        )
    if resp.status_code == 409:
        detail = resp.json().get("detail", "Payment already processed.")
        return templates.TemplateResponse(
            request, "error.html",
            _tpl_ctx(request, title="Already Processed", detail=detail, retry_url="/ui/"),
            status_code=409,
        )
    if resp.status_code != 200:
        return templates.TemplateResponse(
            request, "error.html",
            _tpl_ctx(request, title="Error", detail=f"PISP returned {resp.status_code}.", retry_url="/ui/"),
            status_code=502,
        )

    pr = _normalise_pr(resp.json())
    return templates.TemplateResponse(
        request, "review.html",
        _tpl_ctx(request, pr=pr, pisp_url=PISP_URL),
    )


@app.post("/ui/consent/{payment_request_id}", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_consent(
    request: Request,
    payment_request_id: str,
    approve: bool = True,
    pisp_url: Optional[str] = None,
):
    """
    HTMX partial: submit approve/decline to PISP and render the confirmation card.
    Swapped into #decision-area.

    `pisp_url` is the base URL of the PISP that holds this request — forwarded
    from the review step so cross-PISP consent works correctly.
    """
    target_pisp = pisp_url or PISP_URL
    payload = {
        "approved": approve,
    }

    # Derive our own browser-accessible base URL from the incoming request's Host header
    # so the PISP can redirect the browser back here after bank auth completes.
    _host = request.headers.get("host", "")
    _return_url = f"http://{_host}/ui/bank-callback" if _host else ""
    _consent_url = f"{target_pisp}/payer/requests/{payment_request_id}/consent"
    if _return_url:
        _consent_url += f"?return_url={quote(_return_url, safe='')}"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _consent_url,
            json=payload,
            headers=_sign_payer_request(payload),
            timeout=30.0,
        )

    if resp.status_code not in (200,):
        detail = resp.json().get("detail", resp.text) if resp.content else resp.text
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(
                request,
                title="Payment Failed",
                detail=detail,
                retry_url="/ui/",
            ),
            status_code=resp.status_code,
        )

    outcome = resp.json()
    payment_history[payment_request_id] = outcome

    action = "approved" if approve else "declined"
    log.info("UI payment %s %s → status: %s", payment_request_id, action, outcome.get("status"))

    # Enrich the history entry with full payment details (amount, reference,
    # display_name) which are not returned by the consent endpoint.
    await _enrich_history_entry(payment_request_id)

    # OBIE PSU redirect: bank requires Alice to authorise at her bank's consent screen.
    # Use HTMX full-page redirect so the browser navigates to the bank auth URL.
    if outcome.get("status") == "BANK_AUTH_REQUIRED":
        bank_auth_url = outcome.get("bank_auth_url", "")
        if bank_auth_url:
            log.info("UI redirecting to bank auth screen: %s", bank_auth_url)
            return HTMLResponse("", headers={"HX-Redirect": bank_auth_url})
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(
                request,
                title="Bank redirect unavailable",
                detail="Your bank requires authorisation but no redirect URL was provided.",
                retry_url="/ui/",
            ),
        )

    # Async settlement (B4): bank has accepted the payment but not yet settled.
    # Show a "dispatched" page that polls until SETTLED.
    if outcome.get("status") == "DISPATCHED":
        pr_id = outcome.get("payment_request_id", payment_request_id)
        return HTMLResponse(f"""<!doctype html><html><head>
<meta charset="utf-8"><title>Payment dispatched</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:sans-serif;max-width:480px;margin:4rem auto;padding:1rem;text-align:center}}
h2{{color:#555}}p{{color:#888}}.spinner{{margin:2rem auto;width:40px;height:40px;border:4px solid #ddd;
border-top-color:#666;border-radius:50%;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}</style></head>
<body><div class="spinner"></div>
<h2>Payment dispatched ⏳</h2>
<p>Awaiting bank settlement confirmation…</p>
<script>
(function poll() {{
    fetch('/api/payment/{pr_id}/status')
        .then(r => r.json())
        .then(d => {{
            if (d.status === 'SETTLED') location.href = '/ui/confirm?pr_id={pr_id}';
            else setTimeout(poll, 1500);
        }})
        .catch(() => setTimeout(poll, 2000));
}})();
</script></body></html>""")

    return templates.TemplateResponse(request, "confirm.html", _tpl_ctx(request, outcome=outcome))


@app.get("/ui/bank-callback", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_bank_callback(
    request: Request,
    payment_request_id: str = "",
    status: str = "SETTLED",
):
    """
    Landing page after bank PSU auth redirect.

    The PISP redirects the browser here after bank auth completes,
    passing ``payment_request_id`` and ``status`` as query params.
    Renders the same confirm card as the normal approve/decline flow.
    """
    # Build outcome dict for the confirm template.
    # If we previously stored a richer outcome (from the BANK_AUTH_REQUIRED response),
    # merge the updated status into it; otherwise synthesise a minimal outcome.
    outcome = payment_history.get(payment_request_id, {})
    outcome = dict(outcome)   # don't mutate the stored copy
    outcome["status"] = status
    if payment_request_id:
        payment_history[payment_request_id] = dict(outcome)  # update stored status

    log.info("UI bank-callback: payment_request_id=%s status=%s", payment_request_id, status)
    return templates.TemplateResponse(request, "confirm.html", _tpl_ctx(request, outcome=outcome))


@app.get("/ui/confirm", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_confirm(request: Request, pr_id: str = ""):
    """Confirm page for async-settled payments (B4 polling target).

    Renders the confirm card once the payment has reached SETTLED status.
    ``pr_id`` is the payment_request_id passed as a query param from the
    JS polling loop in the dispatched page.
    """
    outcome = dict(payment_history.get(pr_id, {}))
    outcome.setdefault("status", "SETTLED")
    outcome.setdefault("payment_request_id", pr_id)
    # Persist SETTLED status back so history tab reflects the final state.
    payment_history[pr_id] = outcome
    return templates.TemplateResponse(request, "confirm.html", _tpl_ctx(request, outcome=outcome))


@app.get("/ui/history", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_history(request: Request):
    """Payment history screen."""
    return templates.TemplateResponse(
        request,
        "history.html",
        _tpl_ctx(request, history=list(payment_history.values())),
    )


@app.get(
    "/api/payment/{payment_request_id}/status",
    tags=["UI"],
    include_in_schema=False,
)
async def api_payment_status(payment_request_id: str):
    """JSON status endpoint used by the B4 async-settlement polling page.

    Returns ``{"status": "...", "payment_request_id": "..."}`` by proxying
    the PISP's requester-facing status endpoint (no auth required).
    """
    cached = dict(payment_history.get(payment_request_id, {}))
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PISP_URL}/requester/requests/{payment_request_id}",
                timeout=5.0,
            )
        if resp.status_code == 200:
            fresh = resp.json()
            cached = {**cached, **fresh}
            payment_history[payment_request_id] = cached
    except Exception:
        pass
    return {
        "payment_request_id": payment_request_id,
        "status": cached.get("status", "UNKNOWN"),
    }


@app.get(
    "/ui/payment/{payment_request_id}",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_payer_payment_detail(request: Request, payment_request_id: str):
    """Detail view for a specific payment."""
    cached = dict(payment_history.get(payment_request_id, {}))
    # Try to fetch fresh status from PISP
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PISP_URL}/requester/requests/{payment_request_id}",
                timeout=10.0,
            )
        if resp.status_code == 200:
            fresh = resp.json()
            cached = {**cached, **fresh}
            payment_history[payment_request_id] = cached
    except Exception:
        pass  # fall back to cached data

    if not cached:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Not Found", detail="Payment not found in history.", retry_url="/ui/history"),
            status_code=404,
        )
    return templates.TemplateResponse(request, "payment_detail.html", _tpl_ctx(request, outcome=cached))


@app.get("/ui/request-payment", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_request_payment(request: Request):
    """UC5: payer creates a 'pay me' payment request to share with a paying party."""
    return templates.TemplateResponse(
        request,
        "request_payment.html",
        _tpl_ctx(request, result=None, error=None),
    )


@app.post("/ui/request-payment", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_create_request_payment(
    request: Request,
    amount_pounds: float = Form(...),
    reference: str = Form(...),
    description: Optional[str] = Form(None),
    expires_in_seconds: int = Form(300),
):
    """UC5: create a pay-me payment request and show the shareable URI/QR."""
    amount_pence = round(amount_pounds * 100)
    payload = {
        "payer_uri": _payer_uri,
        "display_name": PAYER_DISPLAY_NAME,
        "amount": _make_fiat_amount(amount_pence),
        "reference": reference,
        "description": description or None,
        "expires_in_seconds": expires_in_seconds,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PISP_URL}/payer/requests",
            json=payload,
            headers=_sign_payer_request(payload),
            timeout=10.0,
        )

    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "request_payment.html",
            _tpl_ctx(
                request,
                result=None,
                error=f"PISP error: {resp.text}",
            ),
            status_code=502,
        )

    result = resp.json()
    payment_history[result["payment_request_id"]] = result
    log.info("UC5 pay-me request created: %s", result["payment_request_id"])
    return templates.TemplateResponse(
        request,
        "request_payment.html",
        _tpl_ctx(request, result=result, error=None),
    )


# ---------------------------------------------------------------------------
# IOU UI routes (UC4 — pre-authorised debit / cheque)
# ---------------------------------------------------------------------------

@app.get("/ui/iou", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_iou(request: Request):
    """IOU tab: Issue or Present a pre-authorised debit token.

    Returns 501 if the connected PISP / bank does not support mandates (cVRP).
    This prevents direct navigation to the tab when it is hidden in the nav bar.
    """
    if not _mandates_supported:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(
                request,
                title="Not available",
                detail=(
                    "IOU / pre-authorised payments require Variable Recurring Payments (VRP) "
                    "support from your bank. This feature is not available with your current bank."
                ),
                retry_url="/ui/",
            ),
            status_code=501,
        )
    return templates.TemplateResponse(
        request,
        "iou.html",
        _tpl_ctx(request, mode="home", outcome=None, error=None),
    )


@app.post("/ui/iou/issue", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_iou_issue(
    request: Request,
    amount_pounds: float = Form(...),
    reference: str = Form(...),
    description: Optional[str] = Form(None),
    expires_in_seconds: int = Form(3600),
    payee_uri: Optional[str] = Form(None),
):
    """Issue a new IOU on behalf of the current payer (A's perspective)."""
    amount_pence = round(amount_pounds * 100)
    payload = {
        "drawer_uri":         _payer_uri,
        "display_name":       PAYER_DISPLAY_NAME,
        "amount":             _make_fiat_amount(amount_pence),
        "reference":          reference,
        "description":        description or None,
        "expires_in_seconds": expires_in_seconds,
        "payee_uri":          payee_uri or None,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PISP_URL}/payer/mandates",
            json=payload,
            headers=_sign_payer_request(payload),
            timeout=10.0,
        )

    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "iou.html",
            _tpl_ctx(request, mode="home", outcome=None,
                     error=f"PISP error: {resp.text}"),
            status_code=502,
        )

    result = resp.json()
    log.info("IOU issued: %s", result.get("iou_id"))
    return templates.TemplateResponse(
        request,
        "iou.html",
        _tpl_ctx(request, mode="issued", outcome=result, error=None),
    )


@app.post("/ui/iou/present", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_iou_present(
    request: Request,
    iou_input: str = Form(...),
    pisp_url_a: str = Form(PISP_URL),
):
    """Present an IOU (B's perspective): parse IOU URI or bare UUID, call fetch-iou."""
    # Accept full QR URI (psp://pay?mandate_id=...&pisp_url=...) or bare UUID
    qr_data = _parse_qr(iou_input.strip())
    iou_id  = qr_data.get("mandate_id") or iou_input.strip()
    # pisp_url from QR takes priority over the form field
    pisp_url_from_qr = qr_data.get("pisp_url")
    target_pisp_url  = pisp_url_from_qr or pisp_url_a or PISP_URL

    _present_body = {"presenter_uri": _payer_uri, "pisp_url": target_pisp_url}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PISP_URL}/payer/mandates/{iou_id}/draw",
            json=_present_body,
            headers=_sign_payer_request(_present_body),
            timeout=30.0,
        )

    if resp.status_code == 200:
        outcome = resp.json()
        payment_history[str(outcome.get("payment_request_id", "iou"))] = outcome
        log.info("IOU presented: %s → %s", iou_id, outcome.get("status"))
        return templates.TemplateResponse(
            request,
            "iou.html",
            _tpl_ctx(request, mode="result", outcome=outcome, error=None),
        )

    detail = resp.json().get("detail", resp.text) if resp.content else resp.text
    return templates.TemplateResponse(
        request,
        "iou.html",
        _tpl_ctx(request, mode="home", outcome=None, error=detail),
        status_code=resp.status_code,
    )


# ---------------------------------------------------------------------------
# App API (REST — used by tests and CI)
# ---------------------------------------------------------------------------

@app.get(
    "/app/scan/{payment_request_id}",
    tags=["App"],
    summary="Scan a QR code / enter a payment request ID (simulates payer scanning terminal)",
)
async def scan_payment_request(payment_request_id: str):
    """
    Simulates the payer scanning a QR code or tapping NFC on the terminal.
    Fetches the payment request details from the PISP for the payer to review.
    Returns the details needed to show the payer the 'confirm payment?' screen.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PISP_URL}/payer/requests/{payment_request_id}",
            timeout=10.0,
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Payment request not found")
    if resp.status_code == 410:
        raise HTTPException(status_code=410, detail="Payment request has expired")
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=resp.json().get("detail"))
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"PISP error: {resp.text}")

    data = resp.json()
    log.info(
        "Scanned payment request %s — %s from %s",
        payment_request_id,
        data["amount"]["display"],
        data["requester_display_name"],
    )
    return data


class PaymentDecisionBody(BaseModel):
    approved: bool


@app.post(
    "/app/payment/{payment_request_id}/decision",
    tags=["App"],
    summary="Approve or decline a payment (simulates payer tapping Approve/Decline)",
)
async def payment_decision(payment_request_id: str, body: PaymentDecisionBody):
    """
    Simulates the payer tapping Approve or Decline on the payment confirmation screen.
    Submits consent (or decline) to the PISP and returns the outcome.
    Payer identity is derived from the Bearer token sent in the Authorization header.
    """
    payload = {
        "approved": body.approved,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PISP_URL}/payer/requests/{payment_request_id}/consent",
            json=payload,
            headers=_sign_payer_request(payload),
            timeout=30.0,  # longer timeout — settlement happens synchronously in prototype
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Payment request not found")
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=resp.json().get("detail"))
    if resp.status_code == 410:
        raise HTTPException(status_code=410, detail="Payment request has expired")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"PISP error: {resp.text}")

    data = resp.json()
    payment_history[payment_request_id] = data

    action = "approved" if body.approved else "declined"
    log.info(
        "Payment %s %s → status: %s",
        payment_request_id,
        action,
        data.get("status"),
    )
    return data


@app.post(
    "/app/pay/{payment_request_id}",
    tags=["App"],
    summary="Scan and immediately approve a payment (convenience endpoint)",
)
async def scan_and_pay(payment_request_id: str):
    """
    Convenience endpoint combining scan + approve in a single call.
    Simulates the payer scanning and immediately tapping Approve.
    Payer identity is derived from the Bearer token in the Authorization header.
    Useful for driving the happy path quickly in tests or demos.
    """
    # First fetch the details (validates the request exists and is payable)
    async with httpx.AsyncClient() as client:
        scan_resp = await client.get(
            f"{PISP_URL}/payer/requests/{payment_request_id}",
            timeout=10.0,
        )

    if scan_resp.status_code != 200:
        raise HTTPException(
            status_code=scan_resp.status_code,
            detail=scan_resp.json().get("detail", scan_resp.text),
        )

    scan_data = scan_resp.json()

    # Then immediately approve
    payload = {
        "approved": True,
    }

    async with httpx.AsyncClient() as client:
        consent_resp = await client.post(
            f"{PISP_URL}/payer/requests/{payment_request_id}/consent",
            json=payload,
            headers=_sign_payer_request(payload),
            timeout=30.0,
        )

    if consent_resp.status_code != 200:
        raise HTTPException(
            status_code=consent_resp.status_code,
            detail=consent_resp.json().get("detail", consent_resp.text),
        )

    outcome = consent_resp.json()
    payment_history[payment_request_id] = outcome

    log.info(
        "Scan-and-pay %s: %s → %s",
        payment_request_id,
        scan_data["amount"]["display"],
        outcome.get("status"),
    )

    return {
        "scanned": scan_data,
        "outcome": outcome,
    }


# ---------------------------------------------------------------------------
# Payer Dispute UI  (Protocol C — authenticated via _payer_api_key)
# ---------------------------------------------------------------------------

_REASON_LABELS = {
    "FRAUD":               "Fraud / Unauthorised",
    "GOODS_NOT_RECEIVED":  "Goods not received",
    "GOODS_NOT_AS_DESCRIBED": "Goods not as described",
    "DUPLICATE_PAYMENT":   "Duplicate payment",
    "AMOUNT_INCORRECT":    "Incorrect amount",
    "OTHER":               "Other",
}


@app.get("/ui/disputes", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_disputes(request: Request):
    """List all disputes filed by this payer."""
    disputes = []
    error = None
    if _payer_api_key:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{PISP_URL}/payer/disputes",
                    headers={"Authorization": f"Bearer {_payer_api_key}"},
                    timeout=10.0,
                )
            if resp.status_code == 200:
                disputes = resp.json().get("disputes", [])
            else:
                error = f"Could not load disputes ({resp.status_code})"
        except Exception as exc:
            error = f"Connection error: {exc}"
    else:
        error = "Not registered with PISP yet."
    return templates.TemplateResponse(
        request,
        "disputes.html",
        _tpl_ctx(request, disputes=disputes, error=error, reason_labels=_REASON_LABELS),
    )


@app.get(
    "/ui/disputes/{dispute_id}",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_dispute_detail(request: Request, dispute_id: str):
    """Detail view for a specific dispute."""
    dispute = None
    error = None
    if _payer_api_key:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{PISP_URL}/payer/disputes/{dispute_id}",
                    headers={"Authorization": f"Bearer {_payer_api_key}"},
                    timeout=10.0,
                )
            if resp.status_code == 200:
                dispute = resp.json()
            elif resp.status_code == 404:
                error = "Dispute not found."
            elif resp.status_code == 403:
                error = "You do not have access to this dispute."
            else:
                error = f"Error loading dispute ({resp.status_code})"
        except Exception as exc:
            error = f"Connection error: {exc}"
    else:
        error = "Not registered with PISP yet."
    # Flash notice from evidence-submission redirect (surfaced as ?notice=...)
    notice = request.query_params.get("notice")
    return templates.TemplateResponse(
        request,
        "dispute_detail.html",
        _tpl_ctx(
            request,
            dispute=dispute,
            error=error,
            notice=notice,
            reason_labels=_REASON_LABELS,
        ),
    )


@app.get(
    "/ui/payment/{payment_request_id}/dispute",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_dispute_form(request: Request, payment_request_id: str):
    """Dispute filing form for a specific settled payment."""
    payment = payment_history.get(payment_request_id, {})
    return templates.TemplateResponse(
        request,
        "dispute_form.html",
        _tpl_ctx(
            request,
            payment=payment,
            payment_request_id=payment_request_id,
            reason_labels=_REASON_LABELS,
            error=None,
        ),
    )


@app.post(
    "/ui/payment/{payment_request_id}/dispute",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_submit_dispute(
    request: Request,
    payment_request_id: str,
    reason_category: str = Form(...),
    reason_text: str = Form(...),
):
    """Submit a dispute for a settled payment via the PISP Protocol C endpoint."""
    payment = payment_history.get(payment_request_id, {})
    error = None
    dispute = None

    if not _payer_api_key:
        error = "Not registered with PISP — cannot file a dispute."
    else:
        body = {
            "pr_id": payment_request_id,
            "reason_category": reason_category,
            "reason_text": reason_text,
        }
        headers = _sign_payer_request(body)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{PISP_URL}/payer/disputes",
                    json=body,
                    headers=headers,
                    timeout=10.0,
                )
            if resp.status_code == 201:
                dispute = resp.json()
            elif resp.status_code == 409:
                detail = resp.json().get("detail", "")
                error = f"Cannot file dispute: {detail}"
            elif resp.status_code == 404:
                error = "Payment not found on PISP."
            else:
                error = f"Unexpected error from PISP ({resp.status_code}): {resp.text}"
        except Exception as exc:
            error = f"Connection error: {exc}"

    if error:
        return templates.TemplateResponse(
            request,
            "dispute_form.html",
            _tpl_ctx(
                request,
                payment=payment,
                payment_request_id=payment_request_id,
                reason_labels=_REASON_LABELS,
                error=error,
            ),
            status_code=422,
        )

    # Success — redirect to the new dispute's detail page
    from fastapi.responses import RedirectResponse as _Redir
    return _Redir(f"/ui/disputes/{dispute['dispute_id']}", status_code=303)


@app.post(
    "/ui/disputes/{dispute_id}/evidence",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_submit_dispute_evidence(
    request: Request,
    dispute_id: str,
    kind: str = Form("TEXT"),
    content: str = Form(...),
):
    """Submit evidence for an open dispute via the PISP Protocol C endpoint."""
    from fastapi.responses import RedirectResponse as _Redir
    from urllib.parse import quote as _url_quote

    notice = None
    if _payer_api_key:
        body = {"kind": kind, "content": content}

        async def _post_evidence() -> "httpx.Response":
            async with httpx.AsyncClient() as client:
                return await client.post(
                    f"{PISP_URL}/payer/disputes/{dispute_id}/evidence",
                    json=body,
                    headers=_sign_payer_request(body),
                    timeout=10.0,
                )

        try:
            resp = await _post_evidence()
            if resp.status_code == 401 and _app_private_key is not None:
                # PISP may have restarted and cleared app_cert_registry.
                # Re-register our public key and retry once.
                log.info("Got 401 on evidence — re-registering app credential and retrying")
                await _register_app_credential()
                resp = await _post_evidence()
            if resp.status_code not in (200, 201):
                try:
                    detail = resp.json().get("detail", f"PISP returned {resp.status_code}")
                except Exception:
                    detail = f"PISP returned {resp.status_code}"
                notice = str(detail)
                log.warning("Evidence submission failed %d: %s", resp.status_code, detail)
            else:
                log.info("Evidence submitted OK for dispute %s", dispute_id)
        except Exception as exc:
            notice = str(exc)
            log.warning("Evidence submission failed: %s", exc)
    else:
        notice = "Not registered with PISP — cannot submit evidence."

    if notice:
        return _Redir(
            f"/ui/disputes/{dispute_id}?notice={_url_quote(notice)}",
            status_code=303,
        )
    return _Redir(f"/ui/disputes/{dispute_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin / observability
# ---------------------------------------------------------------------------

@app.get(
    "/admin/history",
    tags=["Admin"],
    summary="Payment history for this payer",
)
def payment_history_list():
    """Shows all payment decisions made in this session."""
    return list(payment_history.values())
