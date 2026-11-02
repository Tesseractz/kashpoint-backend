import hashlib
import hmac
import os
from typing import Any, Dict, Optional, Tuple

import httpx


def _mode() -> str:
    return (os.getenv("PAYSTACK_MODE") or "live").strip().lower()


def _get(key_base: str) -> str:
    """
    Resolve secrets with optional PAYSTACK_MODE switching.
    Priority:
      1) PAYSTACK_<KEY_BASE>_<MODE>
      2) PAYSTACK_<KEY_BASE>
    """
    mode = _mode()
    v = os.getenv(f"PAYSTACK_{key_base}_{mode.upper()}")
    if v:
        return v.strip()
    v = os.getenv(f"PAYSTACK_{key_base}")
    return (v or "").strip()


def get_paystack_secret_key() -> str:
    return _get("SECRET_KEY")


def get_paystack_public_key() -> str:
    return _get("PUBLIC_KEY")


def get_paystack_plan_code() -> str:
    return _get("PLAN_CODE")


def verify_paystack_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    """Return True iff the signature is a valid Paystack HMAC-SHA512 of raw_body.

    Returns False on any failure. Use `verify_paystack_signature_with_reason`
    instead when you want a human-readable reason for diagnostic logs.
    """
    return verify_paystack_signature_with_reason(raw_body, signature)[0]


def verify_paystack_signature_with_reason(
    raw_body: bytes, signature: Optional[str]
) -> tuple[bool, str]:
    """Verify and explain. Returns (ok, reason).

    `reason` is "ok" on success, otherwise a short tag plus a body-length
    sanity check (no body bytes are logged, no secret bytes are leaked).
    """
    secret = get_paystack_secret_key()
    if not secret:
        return False, "missing PAYSTACK_SECRET_KEY"
    if not signature:
        return False, "missing X-Paystack-Signature header"
    sig = signature.strip()
    # Paystack signatures are 128 hex chars (SHA-512). A short or non-hex
    # value is almost certainly truncation/proxy mangling.
    if len(sig) != 128:
        return False, f"signature length {len(sig)} != 128"
    try:
        int(sig, 16)
    except ValueError:
        return False, "signature is not hex"
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    if hmac.compare_digest(digest, sig.lower()):
        return True, "ok"
    return False, f"hmac mismatch (body {len(raw_body)} bytes)"


def initialize_transaction(
    *,
    email: str,
    amount_kobo: int,
    callback_url: str,
    metadata: dict,
) -> Dict[str, str]:
    """Create a Paystack hosted payment page.

    Returns {"url": authorization_url, "reference": transaction_reference}.
    The reference is what /billing/paystack/sync uses to verify the
    transaction directly with Paystack — required for the mobile flow
    where we can't read the callback URL.
    """
    secret = get_paystack_secret_key()
    if not secret:
        raise Exception("Missing PAYSTACK_SECRET_KEY")

    pc = get_paystack_plan_code()
    if not pc:
        raise Exception("Missing PAYSTACK_PLAN_CODE")

    resp = httpx.post(
        "https://api.paystack.co/transaction/initialize",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        json={
            "email": email,
            "amount": int(amount_kobo),
            "plan": pc,
            "callback_url": callback_url,
            "metadata": metadata,
        },
        timeout=15.0,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("status"):
        raise Exception(data.get("message") or f"Paystack init failed: HTTP {resp.status_code}")
    body = data.get("data") or {}
    url = body.get("authorization_url")
    reference = body.get("reference")
    if not url or not reference:
        raise Exception("Paystack response missing authorization_url or reference")
    return {"url": url, "reference": reference}


def initialize_card_check(
    *,
    email: str,
    amount_cents: int,
    callback_url: str,
    metadata: dict,
) -> Dict[str, str]:
    """Hosted payment page for the small card check that saves a card.

    No plan here: a plan would charge the full monthly amount now. The check
    only obtains a reusable card authorization; it is refunded straight after
    and the subscription is created separately with a future first debit.
    Card only, because only cards can be billed again each month.
    """
    secret = get_paystack_secret_key()
    if not secret:
        raise Exception("Missing PAYSTACK_SECRET_KEY")
    resp = httpx.post(
        "https://api.paystack.co/transaction/initialize",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        json={
            "email": email,
            "amount": int(amount_cents),
            "currency": "ZAR",
            "channels": ["card"],
            "callback_url": callback_url,
            "metadata": metadata,
        },
        timeout=15.0,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("status"):
        raise Exception(data.get("message") or f"Paystack init failed: HTTP {resp.status_code}")
    body = data.get("data") or {}
    url, reference = body.get("authorization_url"), body.get("reference")
    if not url or not reference:
        raise Exception("Paystack response missing authorization_url or reference")
    return {"url": url, "reference": reference}


def create_subscription(*, customer: str, authorization: str, start_date: str) -> Dict[str, Any]:
    """Subscribe a saved card to the Pro plan, first debit on `start_date`.

    Verified in Paystack test mode: the response's next_payment_date equals
    start_date, and nothing is charged before it.
    """
    secret = get_paystack_secret_key()
    if not secret:
        raise Exception("Missing PAYSTACK_SECRET_KEY")
    plan = get_paystack_plan_code()
    if not plan:
        raise Exception("Missing PAYSTACK_PLAN_CODE")
    resp = httpx.post(
        "https://api.paystack.co/subscription",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        json={"customer": customer, "plan": plan, "authorization": authorization, "start_date": start_date},
        timeout=20.0,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("status"):
        raise Exception(data.get("message") or f"Paystack subscription failed: HTTP {resp.status_code}")
    body = data.get("data") or {}
    if not body.get("subscription_code"):
        raise Exception("Paystack response missing subscription_code")
    return body


def refund_transaction(*, reference: str) -> bool:
    """Refund a transaction in full. Returns False instead of raising, so a
    refund hiccup never undoes a card that was saved correctly."""
    secret = get_paystack_secret_key()
    if not secret or not reference:
        return False
    try:
        resp = httpx.post(
            "https://api.paystack.co/refund",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            json={"transaction": reference},
            timeout=15.0,
        )
        data = resp.json() if resp.content else {}
    except Exception as e:
        print(f"[Paystack] refund of {reference} failed: {e!r}")
        return False
    if resp.status_code >= 400 or not data.get("status"):
        # Already refunded is a success for our purposes.
        msg = str(data.get("message") or "")
        if "reversed" in msg.lower() or "refunded" in msg.lower():
            return True
        print(f"[Paystack] refund of {reference} refused: {msg or resp.status_code}")
        return False
    return True


def verify_transaction(*, reference: str) -> dict:
    """
    Verify a Paystack transaction by reference (from checkout redirect).
    Returns the `data` object from Paystack (includes metadata, subscription_code, email_token when applicable).
    """
    secret = get_paystack_secret_key()
    if not secret:
        raise Exception("Missing PAYSTACK_SECRET_KEY")
    ref = (reference or "").strip()
    if not ref:
        raise Exception("Missing transaction reference")

    resp = httpx.get(
        f"https://api.paystack.co/transaction/verify/{ref}",
        headers={"Authorization": f"Bearer {secret}"},
        timeout=20.0,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("status"):
        raise Exception(data.get("message") or f"Paystack verify failed: HTTP {resp.status_code}")
    return data.get("data") or {}


def fetch_subscription_by_code(subscription_code: str) -> Dict[str, Any]:
    """
    GET /subscription/:code — includes next_payment_date for the billing UI.
    """
    secret = get_paystack_secret_key()
    if not secret or not subscription_code:
        return {}
    code = subscription_code.strip()
    resp = httpx.get(
        f"https://api.paystack.co/subscription/{code}",
        headers={"Authorization": f"Bearer {secret}"},
        timeout=15.0,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("status"):
        return {}
    return data.get("data") or {}


def list_customer_subscriptions(*, customer_code: str) -> list:
    """
    Fetch all subscriptions for a Paystack customer.
    Useful for retrieving subscription_code/email_token after a plan-based checkout,
    since the verify-transaction response doesn't include them.
    """
    secret = get_paystack_secret_key()
    if not secret:
        return []
    if not customer_code:
        return []
    resp = httpx.get(
        f"https://api.paystack.co/subscription",
        params={"customer": customer_code},
        headers={"Authorization": f"Bearer {secret}"},
        timeout=15.0,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("status"):
        return []
    return data.get("data") or []


def find_subscription_for_customer(
    verify_data: dict, customer_code: Optional[str]
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract subscription_code + email_token + next_payment_date (ISO) from verify response
    or Paystack subscription APIs. next_payment_date powers current_period_end in our DB.
    """
    sub_code = verify_data.get("subscription_code")
    email_tok = verify_data.get("email_token")
    next_pay: Optional[str] = None

    sub_obj = verify_data.get("subscription") or {}
    if isinstance(sub_obj, dict):
        if not sub_code:
            sub_code = sub_obj.get("subscription_code") or sub_code
        if not email_tok:
            email_tok = sub_obj.get("email_token") or email_tok
        next_pay = sub_obj.get("next_payment_date") or next_pay

    if sub_code:
        if not next_pay:
            details = fetch_subscription_by_code(sub_code)
            if details:
                next_pay = details.get("next_payment_date")
        return sub_code, email_tok, next_pay

    if not customer_code:
        return None, None, None

    try:
        subs = list_customer_subscriptions(customer_code=customer_code)
        target_plan = get_paystack_plan_code()
        for s in subs:
            s_status = s.get("status", "")
            s_plan = s.get("plan", {})
            s_plan_code = s_plan.get("plan_code") if isinstance(s_plan, dict) else None
            sc = s.get("subscription_code")
            et = s.get("email_token")
            npd = s.get("next_payment_date")
            if s_status in ("active", "non-renewing", "attention"):
                if s_plan_code == target_plan or not target_plan:
                    if sc:
                        return sc, et, npd
        if subs:
            first = subs[0]
            sc = first.get("subscription_code")
            et = first.get("email_token")
            npd = first.get("next_payment_date")
            if sc:
                return sc, et, npd
    except Exception:
        pass

    return None, None, None


def disable_subscription(*, subscription_code: str, email_token: str) -> None:
    """
    Disable an active Paystack subscription.
    """
    secret = get_paystack_secret_key()
    if not secret:
        raise Exception("Missing PAYSTACK_SECRET_KEY")
    if not subscription_code or not email_token:
        raise Exception("Missing Paystack subscription_code/email_token")

    resp = httpx.post(
        "https://api.paystack.co/subscription/disable",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        json={"code": subscription_code, "token": email_token},
        timeout=15.0,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("status"):
        raise Exception(data.get("message") or f"Paystack disable failed: HTTP {resp.status_code}")
