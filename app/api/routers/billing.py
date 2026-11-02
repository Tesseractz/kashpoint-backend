import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import unquote_plus, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

import app.clients.paystack as paystack_client
import app.db.supabase as supabase_client
from app.api.deps import RequestContext, get_current_context
from app.core.http_config import normalize_frontend_base, resolve_frontend_base_url
from app.core.time_utils import now_utc_iso
from app.services.subscriptions import _parse_iso, trial_running

router = APIRouter()

# Metadata marker on the small card-check transaction that saves a card during
# the trial. Those charges must never be read as a Pro payment.
CARD_SETUP = "card_setup"


class CheckoutRequest(BaseModel):
    plan: Literal["pro"]
    email: Optional[str] = None


class CardSetupRequest(BaseModel):
    email: Optional[str] = None


class PaystackSyncRequest(BaseModel):
    reference: str


def _card_check_cents() -> int:
    """The card check amount in ZAR cents (refunded right after). Paystack
    accepted R1 for a card check in test mode; never go below it."""
    try:
        value = int(os.getenv("PAYSTACK_CARD_CHECK_CENTS") or 100)
    except ValueError:
        value = 100
    return max(value, 100)


def _read_subscription(supa, store_id) -> dict:
    """The store's subscription row, or {} if there is none. Errors propagate:
    callers decide about money on this, so a failed read must not look like an
    empty row."""
    res = supabase_client.retry_transient(
        lambda: supa.table("subscriptions").select("*").eq("store_id", store_id).limit(1).execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else {}


def _read_subscription_or_empty(supa, store_id) -> dict:
    try:
        return _read_subscription(supa, store_id)
    except Exception:
        return {}


def _claim(supa, key: str) -> bool:
    """One-time claim on `key`, using webhook_events' primary key as the lock.
    False means another request already holds it."""
    try:
        supa.table("webhook_events").insert(
            {"id": key, "type": "kashpoint:card_setup", "received_at": now_utc_iso()}
        ).execute()
        return True
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            return False
        raise


def _release(supa, key: str) -> None:
    try:
        supa.table("webhook_events").delete().eq("id", key).execute()
    except Exception as e:
        print(f"[card setup] could not release {key}: {e!r}")


def _saved(sub: dict) -> dict:
    return {"card_saved": True, "first_charge_date": sub.get("current_period_end") or sub.get("trial_end")}


def _complete_card_setup(supa, store_id: str, pdata: dict, *, wait: bool) -> dict:
    """Turn a successful card check into a Pro subscription whose first R150
    debit falls on the day the trial ends, then refund the check.

    Both the browser's return to /billing and Paystack's charge.success
    webhook land here, usually within the same second. Exactly one of them
    may create the subscription (two would mean two monthly charges), so the
    work is claimed first; the loser waits for the winner's result (redirect)
    or simply returns (webhook).
    """
    reference = (pdata.get("reference") or "").strip()
    sub = _read_subscription(supa, store_id)
    if sub.get("paystack_subscription_code"):
        paystack_client.refund_transaction(reference=reference)
        return _saved(sub)

    key = f"card_setup:{reference}"
    if not _claim(supa, key):
        if wait:
            for _ in range(10):
                time.sleep(1)
                sub = _read_subscription(supa, store_id)
                if sub.get("paystack_subscription_code"):
                    return _saved(sub)
        return {"card_saved": False, "pending": True}

    try:
        if sub.get("status") != "trialing":
            paystack_client.refund_transaction(reference=reference)
            raise HTTPException(
                status_code=400,
                detail="Your free trial is no longer running, so the card was not saved for later. "
                "The check amount is being refunded. Use Subscribe instead.",
            )

        auth = pdata.get("authorization") or {}
        auth_code = auth.get("authorization_code")
        if not auth_code or not auth.get("reusable"):
            paystack_client.refund_transaction(reference=reference)
            raise HTTPException(
                status_code=400,
                detail="This card can't be used for monthly payments. "
                "The check amount is being refunded. Please try a different card.",
            )

        cust = pdata.get("customer") or {}
        cust_code = cust.get("customer_code") if isinstance(cust, dict) else None
        if not cust_code:
            paystack_client.refund_transaction(reference=reference)
            raise HTTPException(status_code=502, detail="Paystack did not return a customer for this card. Please try again.")

        # The customer goes in first: every later Paystack event — the first
        # R150 debit included — arrives without our metadata and finds the
        # store only through this code.
        supabase_client.retry_transient(
            lambda: supa.table("subscriptions")
            .update({"billing_provider": "paystack", "paystack_customer_code": cust_code})
            .eq("store_id", store_id)
            .execute()
        )

        earliest = datetime.now(timezone.utc) + timedelta(minutes=30)
        trial_end = _parse_iso(sub.get("trial_end"))
        start = trial_end if trial_end and trial_end > earliest else earliest
        try:
            created = paystack_client.create_subscription(
                customer=cust_code, authorization=auth_code, start_date=start.isoformat()
            )
        except Exception as e:
            paystack_client.refund_transaction(reference=reference)
            raise HTTPException(
                status_code=502,
                detail=f"Your card was checked but Paystack could not set up the subscription ({e}). "
                "The check amount is being refunded. Please try again.",
            )

        first = created.get("next_payment_date") or start.isoformat()
        code, token = created["subscription_code"], created.get("email_token")
        try:
            supabase_client.retry_transient(
                lambda: supa.table("subscriptions")
                .update(
                    {
                        "paystack_subscription_code": code,
                        "paystack_email_token": token,
                        "current_period_end": first,
                    }
                )
                .eq("store_id", store_id)
                .execute()
            )
        except Exception as e:
            # A subscription we can't record must not stay live at Paystack:
            # the store would be billed while the app knows nothing about it,
            # and a retry would create a second one.
            print(f"[card setup] store {store_id}: saving {code} failed ({e!r}); disabling it at Paystack")
            try:
                if token:
                    paystack_client.disable_subscription(subscription_code=code, email_token=token)
            except Exception as e2:
                print(f"[card setup] store {store_id}: disabling {code} ALSO failed: {e2!r} — cancel it in the Paystack dashboard")
            paystack_client.refund_transaction(reference=reference)
            raise HTTPException(status_code=502, detail="We couldn't save your card. Nothing will be charged; please try again.")
    except Exception:
        _release(supa, key)
        raise

    refunded = paystack_client.refund_transaction(reference=reference)
    return {"card_saved": True, "first_charge_date": first, "check_refunded": refunded}


def _ensure_trial_consumed_once(supa, store_id, update: dict) -> None:
    """First successful Paystack payment/subscription for this store consumes the one-time trial.

    Silently no-ops if the `trial_consumed_at` column does not exist in the DB —
    the rest of the upsert still proceeds so customers don't get a 500 mid-checkout.
    Run backend/scripts/migrations.sql in Supabase to add the column.
    """
    try:
        r = supa.table("subscriptions").select("trial_consumed_at").eq("store_id", store_id).single().execute()
        data = getattr(r, "data", None) or {}
        if isinstance(data, dict) and data.get("trial_consumed_at"):
            return
    except Exception:
        return
    update["trial_consumed_at"] = now_utc_iso()


def _safe_subscription_upsert(supa, payload: dict) -> None:
    """Upsert subscriptions, stripping columns Postgres reports as missing.

    Postgres (via PGRST204) tells us about one missing column per error, so we
    loop a few times — each error strips one more key and we try again. This
    lets the app keep working on a stale schema while a deployer runs
    migrations.sql.
    """
    import re

    current = dict(payload)
    for _ in range(8):  # bounded — DB has ~10 columns total; way more than enough
        try:
            supa.table("subscriptions").upsert(current).execute()
            return
        except Exception as e:
            msg = str(e)
            missing = set(re.findall(r"'([^']+)' column", msg))
            if not missing:
                raise
            stripped = {k: v for k, v in current.items() if k not in missing}
            if not stripped or stripped == current:
                raise
            current = stripped
    # If we exhausted the retry budget, let the next attempt fail loudly.
    supa.table("subscriptions").upsert(current).execute()


def _webhook_next_payment_iso(data: dict, subscription_code: Optional[str] = None) -> Optional[str]:
    """Paystack next charge date for subscription webhooks / charge.success payloads."""
    sub = data.get("subscription")
    if isinstance(sub, dict) and sub.get("next_payment_date"):
        return sub.get("next_payment_date")
    if data.get("next_payment_date"):
        return data.get("next_payment_date")
    sc = data.get("subscription_code") or subscription_code
    if isinstance(sub, dict):
        sc = sc or sub.get("subscription_code")
    if sc:
        details = paystack_client.fetch_subscription_by_code(sc)
        if details:
            return details.get("next_payment_date")
    return None


def _store_for_customer(supa, data: dict) -> Optional[dict]:
    # Only the first checkout's charge.success carries our metadata. The
    # subscription.* events and every monthly renewal arrive without it, so the
    # customer code saved at checkout is the only way back to the store.
    cust = data.get("customer")
    code = cust.get("customer_code") if isinstance(cust, dict) else None
    if not code:
        return None
    try:
        r = (
            supa.table("subscriptions")
            .select("store_id, paystack_subscription_code")
            .eq("paystack_customer_code", code)
            .limit(1)
            .execute()
        )
        rows = getattr(r, "data", None) or []
        return rows[0] if rows else None
    except Exception:
        return None


@router.get("/billing")
def redirect_paystack_return_to_spa(request: Request):
    q = request.url.query
    suffix = f"?{q}" if q else ""
    base = normalize_frontend_base(os.getenv("FRONTEND_URL", "http://localhost:5000"))
    if not base:
        base = "http://localhost:5000"
    location = f"{base}/billing{suffix}"
    loc_host = (urlparse(location).netloc or "").lower()
    req_host = (request.url.netloc or "").lower()
    if loc_host == req_host:
        h = (request.url.hostname or "").lower()
        if h in ("localhost", "127.0.0.1", "::1"):
            location = f"http://localhost:5000/billing{suffix}"
        else:
            raise HTTPException(
                status_code=500,
                detail="FRONTEND_URL must be the SPA origin (not this API). Set FRONTEND_URL to your frontend URL.",
            )
    return RedirectResponse(url=location, status_code=307)


@router.get("/billing/config")
def get_billing_config():
    return {
        "provider": "paystack",
        "paystack": {
            "public_key": paystack_client.get_paystack_public_key(),
            "plan_code": paystack_client.get_paystack_plan_code(),
            "currency": "ZAR",
        },
    }


@router.post("/billing/checkout")
def create_checkout_session(
    body: CheckoutRequest,
    request: Request,
    ctx: RequestContext = Depends(get_current_context),
):
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")

    email = (getattr(body, "email", None) or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Missing email for Paystack checkout")

    supa = supabase_client.get_supabase_client()

    frontend_url = resolve_frontend_base_url(request)
    callback_url = f"{frontend_url}/billing?success=1"
    # Charge amount in the currency's smallest unit (ZAR cents). This must match
    # the amount on PAYSTACK_PLAN_CODE in the Paystack dashboard, so it is
    # configurable rather than hardcoded — changing the plan price in Paystack
    # without changing it here would otherwise silently mismatch.
    try:
        amount_kobo = int(os.getenv("PAYSTACK_PLAN_AMOUNT_CENTS") or 15000)
    except ValueError:
        amount_kobo = 15000

    init = paystack_client.initialize_transaction(
        email=email,
        amount_kobo=amount_kobo,
        callback_url=callback_url,
        metadata={"store_id": str(ctx.store_id), "plan": body.plan},
    )
    # Note the provider on the store's existing row. An update, never an
    # upsert: a store whose signup was cut short has no row yet, and an upsert
    # here would create one from the column defaults — free and active — in
    # place of the 30-day trial it was due.
    try:
        supa.table("subscriptions").update({"billing_provider": "paystack"}).eq(
            "store_id", ctx.store_id
        ).execute()
    except Exception:
        pass
    # Returning the reference lets the mobile client verify the transaction
    # directly after checkout (it can't read the callback URL the way a
    # browser does), and lets the web flow recover if Paystack's redirect
    # somehow drops the ?reference query param.
    return {"url": init["url"], "reference": init["reference"]}


@router.post("/billing/card-setup")
def start_card_setup(
    body: CardSetupRequest,
    request: Request,
    ctx: RequestContext = Depends(get_current_context),
):
    """Start saving a card during the free trial: nothing is charged until the
    trial ends, apart from a small card check that is refunded at once."""
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    email = (body.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Missing email for Paystack checkout")

    supa = supabase_client.get_supabase_client()
    sub = _read_subscription(supa, ctx.store_id)
    if sub.get("paystack_subscription_code"):
        raise HTTPException(status_code=409, detail="A card is already saved for this store.")
    if not trial_running(sub):
        raise HTTPException(
            status_code=400,
            detail="Adding a card for later is only available during the free trial. Use Subscribe instead.",
        )

    frontend_url = resolve_frontend_base_url(request)
    cents = _card_check_cents()
    init = paystack_client.initialize_card_check(
        email=email,
        amount_cents=cents,
        callback_url=f"{frontend_url}/billing?success=1&card=1",
        metadata={"store_id": str(ctx.store_id), "purpose": CARD_SETUP},
    )
    return {"url": init["url"], "reference": init["reference"], "check_amount_cents": cents}


@router.post("/billing/paystack/sync")
def paystack_sync_after_checkout(body: PaystackSyncRequest, ctx: RequestContext = Depends(get_current_context)):
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")

    try:
        pdata = paystack_client.verify_transaction(reference=body.reference)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    meta = pdata.get("metadata") or {}
    store_id = meta.get("store_id")
    if not store_id or str(store_id) != str(ctx.store_id):
        raise HTTPException(status_code=403, detail="This payment does not belong to your store.")

    plan = meta.get("plan") or "pro"
    if plan == "business":
        plan = "pro"

    if pdata.get("status") != "success":
        raise HTTPException(status_code=400, detail="Transaction is not successful")

    if meta.get("purpose") == CARD_SETUP:
        # A card check, not a payment: save the card for the end of the trial.
        result = _complete_card_setup(supabase_client.get_supabase_client(), ctx.store_id, pdata, wait=True)
        return {"synced": True, "has_subscription": bool(result.get("card_saved")), **result}

    cust_code = None
    cust = pdata.get("customer")
    if isinstance(cust, dict):
        cust_code = cust.get("customer_code")

    sub_code, email_tok, next_payment_iso = paystack_client.find_subscription_for_customer(pdata, cust_code)

    update = {
        "store_id": ctx.store_id,
        "billing_provider": "paystack",
        "plan": plan,
        "status": "active",
    }
    if cust_code:
        update["paystack_customer_code"] = cust_code
    if sub_code:
        update["paystack_subscription_code"] = sub_code
    if email_tok:
        update["paystack_email_token"] = email_tok
    # Replace stale period end (e.g. after cancel + resubscribe) with Paystack's next charge date.
    if sub_code:
        update["current_period_end"] = next_payment_iso

    supa = supabase_client.get_supabase_client()
    _ensure_trial_consumed_once(supa, ctx.store_id, update)
    _safe_subscription_upsert(supa, update)
    return {"synced": True, "has_subscription": bool(sub_code)}


@router.post("/billing/cancel")
def cancel_subscription(ctx: RequestContext = Depends(get_current_context)):
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    supa = supabase_client.get_supabase_client()

    try:
        sub_res = supa.table("subscriptions").select("*").eq("store_id", ctx.store_id).single().execute()
        sub = sub_res.data or {}
    except Exception:
        sub = {}

    if not sub:
        raise HTTPException(status_code=400, detail="No subscription found for this store.")

    # If the user is on a trial (no Paystack subscription yet), allow canceling locally.
    status = (sub.get("status") or "").lower()
    if status == "trialing" and not sub.get("paystack_subscription_code"):
        supa.table("subscriptions").update(
            {
                "billing_provider": "paystack",
                "status": "canceled",
                "plan": "expired",
                "current_period_end": None,
            }
        ).eq("store_id", ctx.store_id).execute()
        return {"canceled": True, "note": "Trial canceled locally (no Paystack subscription to disable)."}

    code = sub.get("paystack_subscription_code")
    token = sub.get("paystack_email_token")

    if not code or not token:
        cust_code = sub.get("paystack_customer_code")
        if cust_code:
            found_code, found_token, _ = paystack_client.find_subscription_for_customer({}, cust_code)
            if found_code and found_token:
                code, token = found_code, found_token
                supa.table("subscriptions").update(
                    {"paystack_subscription_code": code, "paystack_email_token": token}
                ).eq("store_id", ctx.store_id).execute()

    if trial_running(sub):
        # A card saved during the trial: stop the scheduled first charge, but
        # the store keeps the rest of the trial it was promised.
        if not code or not token:
            raise HTTPException(
                status_code=502,
                detail="We couldn't reach your saved card at Paystack to remove it. Please try again, or contact support.",
            )
        try:
            paystack_client.disable_subscription(subscription_code=code, email_token=token)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Paystack could not remove the card: {e}")
        supa.table("subscriptions").update(
            {"paystack_subscription_code": None, "paystack_email_token": None, "current_period_end": None}
        ).eq("store_id", ctx.store_id).execute()
        return {"canceled": True, "card_removed": True, "trial_end": sub.get("trial_end")}

    if not code or not token:
        # We don't have enough Paystack identifiers to disable billing via API.
        # Still allow the user to cancel access locally so the app state is consistent.
        supa.table("subscriptions").update(
            {
                "billing_provider": "paystack",
                "status": "canceled",
                "plan": "expired",
                "current_period_end": None,
            }
        ).eq("store_id", ctx.store_id).execute()
        return {
            "canceled": True,
            "warning": "No Paystack subscription identifiers found for this store. Your plan was canceled in-app, but if Paystack is billing you, you must cancel from your Paystack dashboard or Paystack subscription emails.",
        }

    try:
        paystack_client.disable_subscription(subscription_code=code, email_token=token)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    supa.table("subscriptions").update(
        {
            "billing_provider": "paystack",
            "status": "canceled",
            "plan": "expired",
            "current_period_end": None,
        }
    ).eq("store_id", ctx.store_id).execute()
    return {"canceled": True}


@router.post("/paystack/webhook")
async def paystack_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-paystack-signature")
    ok, reason = paystack_client.verify_paystack_signature_with_reason(raw, signature)
    if not ok:
        # Log the reason so operators can tell apart "wrong secret",
        # "no signature header" (likely a misconfigured webhook source),
        # "body altered by a proxy", etc. The body itself is never logged.
        print(f"[Paystack webhook] signature rejected: {reason}")
        raise HTTPException(status_code=400, detail=f"Invalid Paystack signature: {reason}")

    try:
        try:
            payload = await request.json()
        except Exception:
            try:
                payload = json.loads(raw)
            except Exception:
                if isinstance(raw, (bytes, bytearray)):
                    s = raw.decode("utf-8", errors="ignore")
                else:
                    s = str(raw)
                if s.startswith("payload="):
                    payload = json.loads(unquote_plus(s[len("payload=") :]))
                else:
                    raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event")
    data = payload.get("data") or {}
    meta = data.get("metadata") or {}
    store_id = meta.get("store_id")
    plan = meta.get("plan") or "pro"

    supa = supabase_client.get_supabase_client()

    event_id = data.get("id") or payload.get("id")
    if event_id:
        try:
            # The key must include the event type. Paystack's `data.id` is the
            # id of the object the event is about, not the id of the event, so
            # subscription.create / subscription.enable / subscription.disable
            # for one subscription all arrive with the SAME data.id. Keying on
            # the id alone made every later event look like a replay of the
            # first, and cancellations were silently discarded.
            supa.table("webhook_events").insert(
                {
                    "id": f"paystack:{event_type}:{event_id}",
                    "type": f"paystack:{event_type}",
                    "received_at": now_utc_iso(),
                }
            ).execute()
        except Exception as e:
            msg = str(e).lower()
            if "duplicate" in msg or "unique" in msg or "23505" in msg:
                return {"received": True, "duplicate": True}
            raise

    known = None
    if not store_id:
        known = _store_for_customer(supa, data)
        if known:
            store_id = known.get("store_id")
    if not store_id:
        return {"received": True}

    update = {
        "store_id": store_id,
        "billing_provider": "paystack",
        "plan": "pro" if plan == "business" else plan,
    }

    if event_type == "charge.success" and meta.get("purpose") == CARD_SETUP:
        # The refundable card check, not a payment. If the browser's return
        # hasn't saved the card yet, do it here (the claim stops a double).
        try:
            _complete_card_setup(supa, store_id, data, wait=False)
        except HTTPException as e:
            print(f"[Paystack webhook] card setup for store {store_id} not completed: {e.detail}")
        return {"received": True}

    if event_type == "charge.success":
        update["status"] = "active"
        cust = data.get("customer") or {}
        if isinstance(cust, dict):
            update["paystack_customer_code"] = cust.get("customer_code")
        sub_code = data.get("subscription_code")
        email_tok = data.get("email_token")
        sub = data.get("subscription") or {}
        if isinstance(sub, dict):
            sub_code = sub_code or sub.get("subscription_code")
            email_tok = email_tok or sub.get("email_token")
        if not sub_code and known:
            sub_code = known.get("paystack_subscription_code")
        if sub_code:
            update["paystack_subscription_code"] = sub_code
            update["current_period_end"] = _webhook_next_payment_iso(data, sub_code)
        if email_tok:
            update["paystack_email_token"] = email_tok
        _ensure_trial_consumed_once(supa, store_id, update)
        _safe_subscription_upsert(supa, update)

    elif event_type in ("subscription.create", "subscription.enable"):
        update["status"] = "active"
        update["paystack_subscription_code"] = data.get("subscription_code")
        update["paystack_email_token"] = data.get("email_token")
        npd = data.get("next_payment_date")
        if not npd and isinstance(data.get("subscription"), dict):
            npd = data["subscription"].get("next_payment_date")
        sc = update.get("paystack_subscription_code")
        if sc and not npd:
            details = paystack_client.fetch_subscription_by_code(sc)
            npd = details.get("next_payment_date") if details else None
        if sc:
            update["current_period_end"] = npd
        if trial_running(_read_subscription_or_empty(supa, store_id)):
            # A card saved during the trial: the subscription exists but its
            # first debit is on trial_end. The store stays on its trial.
            update.pop("status", None)
        _ensure_trial_consumed_once(supa, store_id, update)
        _safe_subscription_upsert(supa, update)

    elif event_type in ("subscription.disable", "subscription.not_renew") and trial_running(
        _read_subscription_or_empty(supa, store_id)
    ):
        # The card was removed during the trial. Forget the subscription,
        # keep the rest of the trial.
        supa.table("subscriptions").update(
            {"paystack_subscription_code": None, "paystack_email_token": None, "current_period_end": None}
        ).eq("store_id", store_id).execute()

    elif event_type in ("subscription.disable", "subscription.not_renew"):
        supa.table("subscriptions").update(
            {
                "billing_provider": "paystack",
                "status": "canceled",
                "plan": "expired",
                "current_period_end": None,
            }
        ).eq("store_id", store_id).execute()

    elif event_type in ("invoice.payment_failed",):
        supa.table("subscriptions").update(
            {
                "billing_provider": "paystack",
                "status": "past_due",
                "plan": update["plan"],
            }
        ).eq("store_id", store_id).execute()

    return {"received": True}
