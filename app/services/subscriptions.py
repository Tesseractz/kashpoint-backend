import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.db.supabase import get_supabase_client, retry_transient

DEV_PLAN_OVERRIDE = os.getenv("DEV_PLAN_OVERRIDE", "").lower()

TRIAL_DAYS = 30

# A store with a card on file is charged by Paystack on its billing date, but
# the charge and its webhook can land hours after the exact timestamp we hold.
# For that window the store keeps Pro instead of locking a paying customer out
# of their till. A failed charge (invoice.payment_failed -> past_due) or a
# cancellation ends it at once; this only covers "nothing has arrived yet".
PAYMENT_GRACE = timedelta(days=3)


def new_store_subscription(store_id: str) -> dict:
    return {
        "store_id": store_id,
        "plan": "pro",
        "status": "trialing",
        "trial_end": (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat(),
    }


def ensure_store_subscription(supa, store_id: str) -> dict:
    """Give a store its trial subscription if it has none, and return the row.

    Never touches an existing row: an interrupted signup being finished, or two
    first requests racing each other, must not reset a store that is already
    on a plan. ignore-duplicates makes Postgres skip the insert on conflict and
    return nothing, so the existing row is read back in that case.
    """
    payload = new_store_subscription(store_id)
    res = (
        supa.table("subscriptions")
        .upsert(payload, on_conflict="store_id", ignore_duplicates=True)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if rows:
        return rows[0]
    res = supa.table("subscriptions").select("*").eq("store_id", store_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else payload


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored ISO timestamp, tolerating 'Z' and naive values."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class PlanLimits:
    def __init__(
        self,
        plan: str,
        status: str = "active",
        trial_end: Optional[str] = None,
        current_period_end: Optional[str] = None,
        has_card: bool = False,
        comp_until: Optional[str] = None,
        suspended_at: Optional[str] = None,
    ):
        normalized_plan = "pro" if plan == "business" else plan
        # Staff overrides from the admin portal. Neither is touched by Paystack:
        # a suspension outranks everything, a complimentary period grants Pro
        # whatever the billing state says.
        self.suspended = bool(suspended_at)
        self.comp_until = comp_until
        self._comp_active = self._check_comp_active()
        if self._comp_active:
            normalized_plan = "pro"
        self.plan = normalized_plan
        st = status
        if normalized_plan in ("pro", "business") and st == "trialing" and not trial_end:
            st = "active"
        if self.suspended:
            st = "suspended"
        self.status = st
        self.trial_end = trial_end
        self.current_period_end = current_period_end
        # A Paystack subscription exists: Paystack will charge on its own.
        self.has_card = bool(has_card)
        # True when this object is a fail-open stand-in because the subscription
        # could not be read at all — not a statement about the store's plan.
        self.degraded = False
        self._is_trial_active = self._check_trial_active()
        self._period_expired = self._check_period_expired()

    def _check_comp_active(self) -> bool:
        if self.suspended:
            return False
        until = _parse_iso(self.comp_until)
        return bool(until) and datetime.now(timezone.utc) < until

    def _check_trial_active(self) -> bool:
        if self.status != "trialing":
            return False
        trial_dt = _parse_iso(self.trial_end)
        if not trial_dt:
            return False
        return datetime.now(timezone.utc) < trial_dt

    def _check_period_expired(self) -> bool:
        """True only when we know the paid-through date and it has passed.

        An unset or unparseable value is never treated as expired, so a store
        can't be locked out by missing or malformed billing data.
        """
        period_dt = _parse_iso(self.current_period_end)
        if not period_dt:
            return False
        return datetime.now(timezone.utc) >= period_dt

    def _charge_pending(self, due: Optional[str]) -> bool:
        """True while a store with a card on file waits for Paystack's charge.

        Only for stores Paystack will bill by itself, and only for a few days
        after the date the charge was due.
        """
        if not self.has_card:
            return False
        due_dt = _parse_iso(due)
        if not due_dt:
            return False
        now = datetime.now(timezone.utc)
        return due_dt <= now < due_dt + PAYMENT_GRACE

    @property
    def is_active(self) -> bool:
        if self.suspended:
            return False
        if self._comp_active:
            return True
        paid_plan = self.plan in ("pro", "business")
        if self.status == "active" and paid_plan:
            # Nothing ever expires a subscription on a timer, so a lapsed
            # paid-through date must revoke access here — otherwise a store
            # whose cancellation or renewal failure never reached us keeps
            # full access forever. A renewal Paystack is about to charge gets
            # a short grace instead of a lockout on the morning it is due.
            return not self._period_expired or self._charge_pending(self.current_period_end)
        if self.status == "past_due" and paid_plan:
            # Paystack retries a failed charge for a while. Keep access until
            # the period they already paid for runs out, then stop.
            return bool(self.current_period_end) and not self._period_expired
        if self.status == "trialing" and self._is_trial_active:
            return True
        if self.status == "trialing" and paid_plan:
            # Trial over, card on file: the first R150 is being charged.
            return self._charge_pending(self.trial_end)
        return False

    @property
    def is_on_trial(self) -> bool:
        return self.status == "trialing" and self._is_trial_active and not self.suspended

    @property
    def max_products(self) -> Optional[int]:
        if self.is_on_trial:
            return None
        if self.is_active and self.plan in ("pro", "business"):
            return None
        return 10

    @property
    def max_users(self) -> int:
        if self.is_on_trial:
            return 999
        if self.is_active and self.plan in ("pro", "business"):
            return 999
        return 1

    @property
    def allow_multiple_users(self) -> bool:
        if self.is_on_trial:
            return True
        return self.is_active and self.plan in ("pro", "business")

    @property
    def allow_csv_export(self) -> bool:
        if self.is_on_trial:
            return True
        return self.is_active and self.plan in ("pro", "business")

    @property
    def allow_low_stock_alerts(self) -> bool:
        if self.is_on_trial:
            return True
        return self.is_active and self.plan in ("pro", "business")

    @property
    def allow_audit_logs(self) -> bool:
        if self.is_on_trial:
            return True
        return self.is_active and self.plan in ("pro", "business")

    @property
    def allow_advanced_reports(self) -> bool:
        if self.is_on_trial:
            return True
        return self.is_active and self.plan in ("pro", "business")


def _limits_from_row(data: dict) -> PlanLimits:
    return PlanLimits(
        data.get("plan", "expired"),
        status=data.get("status", "expired"),
        trial_end=data.get("trial_end"),
        current_period_end=data.get("current_period_end"),
        has_card=bool(data.get("paystack_subscription_code")),
        comp_until=data.get("comp_until"),
        suspended_at=data.get("suspended_at"),
    )


def trial_running(row: Optional[dict]) -> bool:
    """The store is inside its 30-day trial right now."""
    if not row or row.get("status") != "trialing":
        return False
    end = _parse_iso(row.get("trial_end"))
    return bool(end) and datetime.now(timezone.utc) < end


def _heal_missing_subscription(supa, store_id: str) -> dict:
    try:
        return ensure_store_subscription(supa, store_id)
    except Exception as e:
        print(f"[plan] store {store_id} has no subscription row and creating one failed: {e!r}")
        return {"plan": "expired", "status": "expired"}


def get_store_plan(store_id: str) -> PlanLimits:
    if DEV_PLAN_OVERRIDE in ("pro", "business"):
        normalized = "pro" if DEV_PLAN_OVERRIDE == "business" else DEV_PLAN_OVERRIDE
        return PlanLimits(normalized, status="active")

    supa = get_supabase_client()
    last_error = None
    for _attempt in range(2):
        try:
            res = supa.table("subscriptions").select("*").eq("store_id", store_id).single().execute()
            return _limits_from_row(res.data or {})
        except Exception as e:
            # PGRST116 is PostgREST's "single() matched no rows". No row at
            # all is not a store with no plan — the app never deletes these
            # rows — it is a signup whose setup was cut short before the
            # subscription step (a fresh account's first page load fires
            # several requests at once). Give it the trial it was due.
            if getattr(e, "code", None) == "PGRST116":
                return _limits_from_row(_heal_missing_subscription(supa, store_id))
            last_error = e

    # The lookup itself failed — a transient Supabase or network error, not a
    # statement about the store. Reporting "expired" here told paying
    # customers their plan had lapsed every time the database hiccuped, and
    # the client cannot tell that apart from the real thing. Fail open: the
    # worst case is a lapsed store keeping features for one request, against
    # a paying store being locked out of its own till.
    print(f"[plan] subscriptions lookup failed twice for store {store_id}: {last_error!r}; failing open")
    limits = PlanLimits("pro", status="active")
    limits.degraded = True
    return limits


def enforce_limits_on_create_product(store_id: str):
    limits = get_store_plan(store_id)
    max_products = limits.max_products
    if max_products is None:
        return
    supa = get_supabase_client()
    count = supa.table("products").select("id").eq("store_id", store_id).execute()
    total = len(count.data or [])
    if total >= max_products:
        raise HTTPException(status_code=402, detail="Product limit reached for current plan")


def get_plan_info(store_id: str) -> dict:
    limits = get_store_plan(store_id)
    supa = get_supabase_client()

    # Both reads are part of a fresh account's first page load, which arrives
    # as a burst; a transient transport error here must not become a 500.
    products = retry_transient(
        lambda: supa.table("products").select("id").eq("store_id", store_id).execute()
    )
    product_count = len(products.data or [])

    try:
        sub_res = retry_transient(
            lambda: supa.table("subscriptions").select("*").eq("store_id", store_id).single().execute()
        )
        sub_data = sub_res.data or {}
    except Exception:
        sub_data = {}

    return {
        "plan": limits.plan,
        "status": limits.status,
        "degraded": bool(getattr(limits, "degraded", False)),
        "is_active": limits.is_active,
        "is_on_trial": limits.is_on_trial,
        # Right after a trial is created, this re-read can lag the plan lookup
        # by a few milliseconds; fall back to what the lookup itself saw.
        "trial_end": sub_data.get("trial_end") or limits.trial_end,
        "current_period_end": sub_data.get("current_period_end") or limits.current_period_end,
        "trial_consumed": bool(sub_data.get("trial_consumed_at")),
        "has_paystack_subscription": bool(sub_data.get("paystack_subscription_code")),
        # A card is saved and Paystack will bill it: during the trial that
        # means "first R150 on trial_end", afterwards it is the renewal card.
        "card_on_file": bool(sub_data.get("paystack_subscription_code"))
        and (sub_data.get("status") in ("trialing", "active", "past_due")),
        "billing_provider": sub_data.get("billing_provider"),
        # Staff overrides (admin portal): Pro on the house until a date, or a
        # lock-out the owner cannot lift by paying.
        "comp_until": sub_data.get("comp_until"),
        "suspended": bool(sub_data.get("suspended_at")),
        "limits": {
            "max_products": limits.max_products,
            "max_users": limits.max_users,
            "csv_export": limits.allow_csv_export,
            "low_stock_alerts": limits.allow_low_stock_alerts,
            "audit_logs": limits.allow_audit_logs,
            "advanced_reports": limits.allow_advanced_reports,
        },
        "usage": {
            "products": product_count,
        },
    }
