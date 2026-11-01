import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.db.supabase import get_supabase_client

DEV_PLAN_OVERRIDE = os.getenv("DEV_PLAN_OVERRIDE", "").lower()

TRIAL_DAYS = 30


def new_store_subscription(store_id: str) -> dict:
    return {
        "store_id": store_id,
        "plan": "pro",
        "status": "trialing",
        "trial_end": (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat(),
    }


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
    ):
        normalized_plan = "pro" if plan == "business" else plan
        self.plan = normalized_plan
        st = status
        if normalized_plan in ("pro", "business") and st == "trialing" and not trial_end:
            st = "active"
        self.status = st
        self.trial_end = trial_end
        self.current_period_end = current_period_end
        # True when this object is a fail-open stand-in because the subscription
        # could not be read at all — not a statement about the store's plan.
        self.degraded = False
        self._is_trial_active = self._check_trial_active()
        self._period_expired = self._check_period_expired()

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

    @property
    def is_active(self) -> bool:
        paid_plan = self.plan in ("pro", "business")
        if self.status == "active" and paid_plan:
            # Nothing ever expires a subscription on a timer, so a lapsed
            # paid-through date must revoke access here — otherwise a store
            # whose cancellation or renewal failure never reached us keeps
            # full access forever.
            return not self._period_expired
        if self.status == "past_due" and paid_plan:
            # Paystack retries a failed charge for a while. Keep access until
            # the period they already paid for runs out, then stop.
            return bool(self.current_period_end) and not self._period_expired
        if self.status == "trialing" and self._is_trial_active:
            return True
        return False

    @property
    def is_on_trial(self) -> bool:
        return self.status == "trialing" and self._is_trial_active

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


def get_store_plan(store_id: str) -> PlanLimits:
    if DEV_PLAN_OVERRIDE in ("pro", "business"):
        normalized = "pro" if DEV_PLAN_OVERRIDE == "business" else DEV_PLAN_OVERRIDE
        return PlanLimits(normalized, status="active")

    supa = get_supabase_client()
    last_error = None
    for _attempt in range(2):
        try:
            res = supa.table("subscriptions").select("*").eq("store_id", store_id).single().execute()
            data = res.data or {}
            plan = data.get("plan", "expired")
            status = data.get("status", "expired")
            return PlanLimits(
                plan,
                status=status,
                trial_end=data.get("trial_end"),
                current_period_end=data.get("current_period_end"),
            )
        except Exception as e:
            # PGRST116 is PostgREST's "single() matched no rows": the store
            # genuinely has no subscription, which really is no plan.
            if getattr(e, "code", None) == "PGRST116":
                return PlanLimits("expired", status="expired")
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

    product_count = len((supa.table("products").select("id").eq("store_id", store_id).execute()).data or [])

    try:
        sub_res = supa.table("subscriptions").select("*").eq("store_id", store_id).single().execute()
        sub_data = sub_res.data or {}
    except Exception:
        sub_data = {}

    return {
        "plan": limits.plan,
        "status": limits.status,
        "degraded": bool(getattr(limits, "degraded", False)),
        "is_active": limits.is_active,
        "is_on_trial": limits.is_on_trial,
        "trial_end": sub_data.get("trial_end"),
        "current_period_end": sub_data.get("current_period_end"),
        "trial_consumed": bool(sub_data.get("trial_consumed_at")),
        "has_paystack_subscription": bool(sub_data.get("paystack_subscription_code")),
        "billing_provider": sub_data.get("billing_provider"),
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
