"""Selling airtime and data from the till, through SIMcloud.

Each shop has its own SIMcloud wallet, created here the first time the owner
switches airtime on. A sale is paid from that wallet, never from KashPoint's.
SIMcloud sells at retail value with no reseller discount, so the shop earns
the service fee it sets (charged to the customer on top of the airtime).

There is no test mode at SIMcloud: every sale placed here is real.
"""
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import app.clients.simcloud as simcloud
import app.core.password_crypto as password_crypto
import app.db.supabase as supabase_client
from app.api.deps import RequestContext, get_current_context
from app.clients.simcloud import SimcloudError

router = APIRouter(prefix="/airtime", tags=["airtime"])

MIGRATION = "supabase/migrations/20261111120000_airtime.sql"
MAX_SERVICE_FEE = 50
FINAL_STATES = ("delivered", "failed")

# Where a shop pays to fund its wallet (from SIMcloud's "How to pay").
TOP_UP = {
    "account_name": "FREEPAID (PTY) LTD",
    "bank": "FNB",
    "branch_code": "201410",
    "account_number": "62057217400",
    "note": "Use ONLY the shop's registered cell number as the reference. FNB payments reflect immediately; other banks take longer.",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(res) -> list:
    return list(getattr(res, "data", None) or [])


def _schema_error(e: Exception) -> HTTPException:
    text = str(e)
    if "PGRST204" in text or "PGRST205" in text or "does not exist" in text or "schema cache" in text:
        return HTTPException(status_code=503, detail=f"Airtime is not switched on for KashPoint yet ({MIGRATION} has not been run).")
    return HTTPException(status_code=500, detail=text)


def _upstream(e: SimcloudError) -> HTTPException:
    body = e.body or {}
    if "required_balance" in body or "insufficient" in str(e).lower():
        return HTTPException(
            status_code=402,
            detail="Not enough money in your airtime wallet for this sale. Top up the wallet and try again.",
        )
    if e.status == 409:
        return HTTPException(status_code=409, detail="The same number and amount was sold in the last 5 minutes. Wait a few minutes before repeating it.")
    return HTTPException(status_code=502, detail=f"SIMcloud: {e}")


def _account(supa, store_id: str) -> Optional[dict]:
    try:
        rows = _rows(supa.table("airtime_accounts").select("*").eq("store_id", store_id).limit(1).execute())
    except Exception as e:
        raise _schema_error(e)
    return rows[0] if rows else None


def _token(account: dict) -> str:
    return password_crypto._decrypt_password(account["api_token_encrypted"])


def _require_account(supa, store_id: str) -> dict:
    account = _account(supa, store_id)
    if not account:
        raise HTTPException(status_code=409, detail="Airtime has not been switched on for this shop yet.")
    return account


def _admin(ctx: RequestContext) -> None:
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")


@router.get("/status")
def status(ctx: RequestContext = Depends(get_current_context)):
    supa = supabase_client.get_supabase_client()
    try:
        simcloud.parent_token()
        available = True
    except SimcloudError:
        available = False
    account = _account(supa, ctx.store_id) if available else None
    out = {
        "available": available,
        "activated": bool(account),
        "cell": account.get("cell") if account else None,
        "service_fee": float(account.get("service_fee") or 0) if account else 0,
        "balance": None,
        "balance_updated_at": None,
        "top_up": TOP_UP,
        "limits": {"airtime_min": simcloud.AIRTIME_MIN, "airtime_max": simcloud.AIRTIME_MAX, "max_service_fee": MAX_SERVICE_FEE},
        "networks": list(simcloud.AIRTIME_NETWORKS),
    }
    if account:
        try:
            bal = simcloud.balance(_token(account))
            out["balance"] = float(bal.get("balance") or 0)
            out["balance_updated_at"] = bal.get("updated_at")
        except SimcloudError as e:
            out["balance_error"] = str(e)
    return out


class ActivateBody(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=60)
    last_name: str = Field(..., min_length=1, max_length=60)
    cell: str = Field(..., min_length=9, max_length=20)
    accept_terms: bool = False


@router.post("/activate")
def activate(body: ActivateBody, ctx: RequestContext = Depends(get_current_context)):
    """Create the shop's own SIMcloud wallet. Owner only, once per shop."""
    _admin(ctx)
    if not body.accept_terms:
        raise HTTPException(status_code=400, detail="You need to accept SIMcloud's terms and conditions to sell airtime.")
    cell = simcloud.normalize_cell(body.cell)
    if not cell:
        raise HTTPException(status_code=400, detail="Enter a valid South African cell number, e.g. 082 123 4567.")
    if not ctx.email:
        raise HTTPException(status_code=400, detail="Your account has no email address; airtime needs one.")

    supa = supabase_client.get_supabase_client()
    if _account(supa, ctx.store_id):
        raise HTTPException(status_code=409, detail="Airtime is already switched on for this shop.")

    store = _rows(supa.table("stores").select("name").eq("id", ctx.store_id).limit(1).execute())
    # The shop never needs this password: it works through KashPoint. It can
    # be reset at SIMcloud if the owner ever wants the portal as well.
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    try:
        res = simcloud.register_child(
            first_name=body.first_name.strip(),
            last_name=body.last_name.strip(),
            cell=cell,
            email=ctx.email,
            password=password,
            business_name=(store[0].get("name") if store else None),
        )
    except SimcloudError as e:
        if e.status == 409:
            raise HTTPException(
                status_code=409,
                detail="That cell number already has a SIMcloud account that is not linked to KashPoint. Use another number, or ask KashPoint support to link it.",
            )
        raise HTTPException(status_code=502, detail=f"SIMcloud could not create the wallet: {e}")

    token = res.get("api_token")
    if not token:
        raise HTTPException(status_code=502, detail="SIMcloud created the account but returned no token. Contact KashPoint support.")
    row = {
        "store_id": ctx.store_id,
        "simcloud_user_id": str(res.get("user_id") or ""),
        "cell": cell,
        "api_token_encrypted": password_crypto._encrypt_password(token),
        "service_fee": 0,
        "terms_accepted_at": _now(),
        "terms_accepted_by": ctx.user_id,
        "created_at": _now(),
    }
    try:
        supa.table("airtime_accounts").upsert(row, on_conflict="store_id").execute()
    except Exception as e:
        raise _schema_error(e)
    return {"activated": True, "cell": cell, "top_up": TOP_UP}


class SettingsBody(BaseModel):
    service_fee: float = Field(..., ge=0, le=MAX_SERVICE_FEE)


@router.put("/settings")
def settings(body: SettingsBody, ctx: RequestContext = Depends(get_current_context)):
    _admin(ctx)
    supa = supabase_client.get_supabase_client()
    _require_account(supa, ctx.store_id)
    fee = round(body.service_fee, 2)
    supa.table("airtime_accounts").update({"service_fee": fee}).eq("store_id", ctx.store_id).execute()
    return {"service_fee": fee}


@router.delete("/account")
def switch_off(ctx: RequestContext = Depends(get_current_context)):
    """Disconnect the shop from its airtime wallet. Owner only.

    SIMcloud has no way to delete an account through its API, so the wallet
    (and any money in it) stays at SIMcloud; KashPoint just forgets the link
    and the token. Switching on again with the same cell number reconnects
    the same wallet. Past airtime sales stay in the shop's history.
    """
    _admin(ctx)
    supa = supabase_client.get_supabase_client()
    account = _require_account(supa, ctx.store_id)
    balance_left = None
    try:
        balance_left = float(simcloud.balance(_token(account)).get("balance") or 0)
    except SimcloudError:
        pass
    supa.table("airtime_accounts").delete().eq("store_id", ctx.store_id).execute()
    return {"switched_off": True, "cell": account.get("cell"), "balance_left": balance_left}


@router.get("/network")
def network(msisdn: str, ctx: RequestContext = Depends(get_current_context)):
    """Which network a customer's number is on, so the till can pick it."""
    cell = simcloud.normalize_cell(msisdn)
    if not cell:
        raise HTTPException(status_code=400, detail="Enter a valid cell number.")
    supa = supabase_client.get_supabase_client()
    account = _require_account(supa, ctx.store_id)
    try:
        return {"msisdn": cell, "network": simcloud.lookup_network(_token(account), cell)}
    except SimcloudError:
        # A lookup that fails must never block a sale; the cashier picks by hand.
        return {"msisdn": cell, "network": None}


@router.get("/products")
def products(network: str = "", ctx: RequestContext = Depends(get_current_context)):
    """Data bundles SIMcloud sells, optionally for one network."""
    supa = supabase_client.get_supabase_client()
    account = _require_account(supa, ctx.store_id)
    try:
        items = simcloud.data_products(_token(account), network or None)
    except SimcloudError as e:
        raise _upstream(e)
    return {
        "items": [
            {
                "network": str(p.get("network") or "").replace("pd-", ""),
                "amount": float(p.get("sellvalue") or p.get("amount") or 0),
                "description": p.get("description"),
                "group": p.get("groupname"),
            }
            for p in items
        ]
    }


class SellBody(BaseModel):
    kind: str = "airtime"
    msisdn: str
    network: str
    amount: float = Field(..., gt=0)
    description: Optional[str] = Field(None, max_length=120)


@router.post("/sell")
def sell(body: SellBody, ctx: RequestContext = Depends(get_current_context)):
    """Send airtime or a data bundle to a phone, paid from the shop's wallet.

    The customer pays the shop `amount + service_fee` at the till; only
    `amount` leaves the wallet.
    """
    if body.kind not in ("airtime", "data"):
        raise HTTPException(status_code=400, detail="Choose airtime or data.")
    network = body.network.strip().lower()
    if network not in simcloud.AIRTIME_NETWORKS:
        raise HTTPException(status_code=400, detail="Choose MTN, Vodacom, Cell C or Telkom.")
    msisdn = simcloud.normalize_cell(body.msisdn)
    if not msisdn:
        raise HTTPException(status_code=400, detail="Enter the customer's cell number, e.g. 082 123 4567.")
    if body.kind == "airtime":
        if body.amount != int(body.amount) or not (simcloud.AIRTIME_MIN <= body.amount <= simcloud.AIRTIME_MAX):
            raise HTTPException(status_code=400, detail=f"Airtime is sold in whole rands from R{simcloud.AIRTIME_MIN} to R{simcloud.AIRTIME_MAX}.")

    supa = supabase_client.get_supabase_client()
    account = _require_account(supa, ctx.store_id)
    token = _token(account)
    reference = f"KP-{secrets.token_hex(6)}"
    amount = int(body.amount) if body.kind == "airtime" else body.amount

    try:
        if body.kind == "airtime":
            res = simcloud.order_airtime(token, msisdn=msisdn, network=network, amount=amount, reference=reference)
        else:
            res = simcloud.order_data(token, msisdn=msisdn, network=network, amount=amount, reference=reference)
    except SimcloudError as e:
        raise _upstream(e)

    sale = {
        "store_id": ctx.store_id,
        "sold_by": ctx.user_id,
        "kind": body.kind,
        "network": network,
        "msisdn": msisdn,
        "amount": float(amount),
        "service_fee": float(account.get("service_fee") or 0),
        "description": body.description,
        "reference": reference,
        "request_id": str(res.get("request_id") or ""),
        "status": str(res.get("queue_status") or res.get("status") or "queued").lower(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        rows = _rows(supa.table("airtime_sales").insert(sale).execute())
        sale = rows[0] if rows else sale
    except Exception as e:
        # The airtime has been sent; losing the record must not look like a failed sale.
        print(f"[airtime] store {ctx.store_id}: sale {reference} sent but not recorded: {e!r}")
    return sale


def _refresh(supa, account: dict, sale: dict) -> dict:
    """Ask SIMcloud where a sale that isn't final yet has got to."""
    if sale.get("status") in FINAL_STATES or not sale.get("request_id"):
        return sale
    try:
        fn = simcloud.airtime_status if sale.get("kind") == "airtime" else simcloud.data_status
        res = fn(_token(account), sale["request_id"])
    except SimcloudError:
        return sale
    new = str(res.get("status") or res.get("queue_status") or "").lower()
    if new and new != sale.get("status"):
        supa.table("airtime_sales").update({"status": new, "updated_at": _now()}).eq("id", sale["id"]).eq("store_id", sale["store_id"]).execute()
        sale = {**sale, "status": new}
    return sale


@router.get("/sales")
def sales(limit: int = 30, ctx: RequestContext = Depends(get_current_context)):
    supa = supabase_client.get_supabase_client()
    account = _require_account(supa, ctx.store_id)
    limit = 30 if limit < 1 or limit > 200 else limit
    rows = _rows(
        supa.table("airtime_sales").select("*").eq("store_id", ctx.store_id).order("created_at", desc=True).limit(limit).execute()
    )
    # Only the newest few can still be in flight; don't call SIMcloud for the whole list.
    rows = [_refresh(supa, account, r) if i < 5 else r for i, r in enumerate(rows)]
    return {
        "items": rows,
        "fees_total": round(sum(float(r.get("service_fee") or 0) for r in rows if r.get("status") != "failed"), 2),
    }


@router.get("/sales/{sale_id}")
def sale_status(sale_id: int, ctx: RequestContext = Depends(get_current_context)):
    supa = supabase_client.get_supabase_client()
    account = _require_account(supa, ctx.store_id)
    rows = _rows(supa.table("airtime_sales").select("*").eq("id", sale_id).eq("store_id", ctx.store_id).limit(1).execute())
    if not rows:
        raise HTTPException(status_code=404, detail="Sale not found")
    return _refresh(supa, account, rows[0])
