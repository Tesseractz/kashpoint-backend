"""The admin portal's API: everything above a single store.

Owners (KashPoint staff) see and manage every store. Partners (the computer
shops that sell tills with KashPoint on them) see the stores that signed up
with their partner code, read-only. Every change made here is written to
`admin_actions` with who made it.

Reads load the small tables whole and join in Python: the whole customer base
is a few hundred rows, and a Postgres view would be one more thing to migrate.
"""
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import app.db.supabase as supabase_client
import app.services.platform_admin as pa
from app.core.http_config import normalize_frontend_base
from app.services.platform_admin import PlatformAdmin, get_platform_admin, require_owner
from app.services.subscriptions import _parse_iso, ensure_store_subscription

router = APIRouter(prefix="/admin", tags=["admin"])

SALES_WINDOW_DAYS = 30
SIGNUP_WEEKS = 8
# A store that has sold before but not for this long is drifting away.
AT_RISK_DAYS = 7
MAX_NOTES = 2000
PARTNER_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,19}$")
MIGRATION = "supabase/migrations/20261108120000_admin_portal.sql"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _rows(res) -> list:
    return list(getattr(res, "data", None) or [])


def _plan_price_zar() -> int:
    return int(os.getenv("PAYSTACK_PLAN_AMOUNT_CENTS") or 15000) // 100


def _schema_error(e: Exception) -> HTTPException:
    """A write named a column or table the database doesn't have yet."""
    text = str(e)
    if "PGRST204" in text or "PGRST205" in text or "does not exist" in text or "schema cache" in text:
        return HTTPException(
            status_code=500,
            detail=f"The database has not been prepared for the admin portal yet. Run {MIGRATION} in the Supabase SQL Editor.",
        )
    return HTTPException(status_code=500, detail=text)


def _public_partner(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "code": p.get("code"),
        "contact_name": p.get("contact_name"),
        "contact_phone": p.get("contact_phone"),
        "contact_email": p.get("contact_email"),
        "notes": p.get("notes"),
        "created_at": p.get("created_at"),
    }


class _Snapshot:
    """Everything the lists and the overview are computed from, read once per
    request and scoped to what the caller may see."""

    def __init__(self, supa, admin: PlatformAdmin):
        self.supa = supa
        self.admin = admin
        stores = _rows(supa.table("stores").select("*").order("created_at", desc=True).execute())
        if not admin.is_owner:
            stores = [s for s in stores if s.get("partner_id") and s.get("partner_id") == admin.partner_id]
        self.stores = stores
        self.store_ids = {s["id"] for s in stores}

        self.subs = {r.get("store_id"): r for r in _rows(supa.table("subscriptions").select("*").execute())}

        self.profiles: Dict[str, list] = defaultdict(list)
        for p in _rows(supa.table("profiles").select("id,name,role,store_id").execute()):
            self.profiles[p.get("store_id")].append(p)

        self.partners: Dict[str, dict] = {}
        try:
            self.partners = {p["id"]: p for p in _rows(supa.table("partners").select("*").execute())}
        except Exception as e:
            print(f"[admin] partners unavailable: {e!r}")

        self.products: Dict[str, int] = defaultdict(int)
        for p in _rows(supa.table("products").select("store_id").execute()):
            self.products[p.get("store_id")] += 1

        since = _iso(_now() - timedelta(days=SALES_WINDOW_DAYS))
        self.sales: Dict[str, dict] = {}
        for s in _rows(supa.table("sales").select("store_id,total_price,timestamp").gte("timestamp", since).execute()):
            a = self.sales.setdefault(s.get("store_id"), {"count": 0, "total": 0.0, "last": None})
            a["count"] += 1
            a["total"] += float(s.get("total_price") or 0)
            ts = s.get("timestamp")
            if ts and (a["last"] is None or ts > a["last"]):
                a["last"] = ts

        self.auth: Dict[str, dict] = {}
        self.auth_unavailable = False
        try:
            self.auth = pa.auth_index()
        except Exception as e:
            print(f"[admin] Supabase Auth unavailable: {e!r}")
            self.auth_unavailable = True

    def find(self, store_id: str) -> Optional[dict]:
        return next((s for s in self.stores if s["id"] == store_id), None)

    def summary(self, store: dict) -> dict:
        sid = store["id"]
        owner = self.auth.get(str(store.get("owner_id"))) or {}
        members = self.profiles.get(sid, [])
        sign_ins = [(self.auth.get(str(p.get("id"))) or {}).get("last_sign_in_at") for p in members]
        sign_ins = [x for x in sign_ins if x]
        sales = self.sales.get(sid) or {"count": 0, "total": 0.0, "last": None}
        partner = self.partners.get(store.get("partner_id")) if store.get("partner_id") else None
        return {
            "id": sid,
            "name": store.get("name"),
            "phone": store.get("phone"),
            "created_at": store.get("created_at"),
            "owner_id": store.get("owner_id"),
            "owner_email": owner.get("email"),
            "owner_last_sign_in_at": owner.get("last_sign_in_at"),
            "last_sign_in_at": max(sign_ins) if sign_ins else None,
            "partner": {"id": partner["id"], "name": partner.get("name"), "code": partner.get("code")} if partner else None,
            "subscription": pa.describe_subscription(self.subs.get(sid)),
            "users_count": len(members),
            "products_count": self.products.get(sid, 0),
            "sales_30d_count": sales["count"],
            "sales_30d_total": round(sales["total"], 2),
            "last_sale_at": sales["last"],
            # Staff notes stay with staff.
            "admin_notes": store.get("admin_notes") if self.admin.is_owner else None,
        }


def _load_store(supa, admin: PlatformAdmin, store_id: str) -> dict:
    """The store row, or 404. A partner's view does not reveal that a store
    outside their list exists."""
    rows = _rows(supa.table("stores").select("*").eq("id", store_id).limit(1).execute())
    store = rows[0] if rows else None
    if not store or (not admin.is_owner and store.get("partner_id") != admin.partner_id):
        raise HTTPException(status_code=404, detail="Store not found")
    return store


def _read_sub(supa, store_id: str) -> dict:
    rows = _rows(supa.table("subscriptions").select("*").eq("store_id", store_id).limit(1).execute())
    return rows[0] if rows else {}


def _ensure_sub(supa, store_id: str) -> dict:
    """A store whose signup was cut short has no subscription row; give it the
    trial it was due before writing an override onto it."""
    try:
        return ensure_store_subscription(supa, store_id) or _read_sub(supa, store_id)
    except Exception as e:
        raise _schema_error(e)


def _update_sub(supa, store_id: str, payload: dict) -> dict:
    try:
        supa.table("subscriptions").update(payload).eq("store_id", store_id).execute()
    except Exception as e:
        raise _schema_error(e)
    return _read_sub(supa, store_id)


# ---------------------------------------------------------------------------
# Who am I, overview
# ---------------------------------------------------------------------------

@router.get("/me")
def me(admin: PlatformAdmin = Depends(get_platform_admin)):
    supa = supabase_client.get_supabase_client()
    partner = None
    tables_ready = True
    try:
        if admin.partner_id:
            rows = _rows(supa.table("partners").select("*").eq("id", admin.partner_id).limit(1).execute())
            partner = _public_partner(rows[0]) if rows else None
        else:
            supa.table("partners").select("id").limit(1).execute()
    except Exception as e:
        print(f"[admin] partners table not readable: {e!r}")
        tables_ready = False
    return {**admin.as_dict(), "partner": partner, "tables_ready": tables_ready, "plan_price_zar": _plan_price_zar()}


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@router.get("/overview")
def overview(admin: PlatformAdmin = Depends(get_platform_admin)):
    supa = supabase_client.get_supabase_client()
    snap = _Snapshot(supa, admin)
    items = [snap.summary(s) for s in snap.stores]
    now = _now()

    states = Counter(i["subscription"]["state"] for i in items)
    paying = [i for i in items if i["subscription"]["paying"]]

    def created(i):
        return _parse_iso(i.get("created_at"))

    new_7d = sum(1 for i in items if created(i) and created(i) >= now - timedelta(days=7))
    new_30d = sum(1 for i in items if created(i) and created(i) >= now - timedelta(days=30))

    this_week = _week_start(now.date())
    weeks = [this_week - timedelta(weeks=k) for k in range(SIGNUP_WEEKS - 1, -1, -1)]
    per_week = Counter()
    for i in items:
        c = created(i)
        if c:
            per_week[_week_start(c.date())] += 1
    signups_by_week = [{"week_start": w.isoformat(), "count": per_week.get(w, 0)} for w in weeks]

    trials_ending = sorted(
        [i for i in items if i["subscription"]["state"] in ("trial", "trial_ending") and (i["subscription"]["days_left"] or 99) <= 7],
        key=lambda i: i["subscription"]["days_left"] or 0,
    )

    cutoff = now - timedelta(days=AT_RISK_DAYS)
    at_risk = []
    for i in items:
        if i["subscription"]["state"] not in ("trial", "trial_ending", "active", "comped"):
            continue
        c = created(i)
        if not c or c > cutoff:
            continue
        last = _parse_iso(i.get("last_sale_at"))
        if last is None or last < cutoff:
            at_risk.append(i)
    at_risk.sort(key=lambda i: i.get("last_sale_at") or "")

    return {
        "stores_total": len(items),
        "stores_new_7d": new_7d,
        "stores_new_30d": new_30d,
        "states": dict(states),
        "paying": len(paying),
        "mrr_zar": len(paying) * _plan_price_zar(),
        "sales_30d_count": sum(i["sales_30d_count"] for i in items),
        "sales_30d_total": round(sum(i["sales_30d_total"] for i in items), 2),
        "signups_by_week": signups_by_week,
        "trials_ending": trials_ending[:10],
        "at_risk": at_risk[:10],
        "auth_unavailable": snap.auth_unavailable,
    }


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

@router.get("/stores")
def list_stores(
    q: str = "",
    state: str = "",
    partner_id: str = "",
    admin: PlatformAdmin = Depends(get_platform_admin),
):
    supa = supabase_client.get_supabase_client()
    snap = _Snapshot(supa, admin)
    items = [snap.summary(s) for s in snap.stores]

    needle = (q or "").strip().lower()
    if needle:
        # Phones are stored as +27..., people type 082...: compare digits and
        # let a leading 0 stand for the country code.
        digits = re.sub(r"\D", "", needle)
        digit_forms = {digits, digits[1:] if digits.startswith("0") and len(digits) > 1 else digits} if digits else set()

        def phone_matches(phone):
            have = re.sub(r"\D", "", phone or "")
            return bool(have) and any(d and d in have for d in digit_forms)

        items = [
            i for i in items
            if needle in (i.get("name") or "").lower()
            or needle in (i.get("owner_email") or "").lower()
            or phone_matches(i.get("phone"))
        ]
    wanted = {s.strip() for s in (state or "").split(",") if s.strip()}
    if wanted:
        items = [i for i in items if i["subscription"]["state"] in wanted]
    if partner_id:
        if partner_id == "none":
            items = [i for i in items if not i.get("partner")]
        else:
            items = [i for i in items if (i.get("partner") or {}).get("id") == partner_id]

    partners = [_public_partner(p) for p in snap.partners.values()] if admin.is_owner else []
    return {"items": items, "total": len(items), "partners": partners, "auth_unavailable": snap.auth_unavailable}


@router.get("/stores/{store_id}")
def store_detail(store_id: str, admin: PlatformAdmin = Depends(get_platform_admin)):
    supa = supabase_client.get_supabase_client()
    snap = _Snapshot(supa, admin)
    store = snap.find(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    summary = snap.summary(store)

    users = []
    for p in snap.profiles.get(store_id, []):
        acct = snap.auth.get(str(p.get("id"))) or {}
        users.append(
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "role": p.get("role"),
                "email": acct.get("email"),
                "created_at": acct.get("created_at"),
                "last_sign_in_at": acct.get("last_sign_in_at"),
                "is_owner": p.get("id") == store.get("owner_id"),
            }
        )
    users.sort(key=lambda u: (not u["is_owner"], u.get("role") != "admin", u.get("name") or ""))

    sub = dict(snap.subs.get(store_id) or {})
    sub.pop("paystack_email_token", None)
    if not admin.is_owner:
        # A partner sees where the store stands, not its billing identifiers.
        for key in ("paystack_customer_code", "paystack_subscription_code"):
            sub.pop(key, None)

    all_sales = _rows(supa.table("sales").select("id,total_price").eq("store_id", store_id).execute())
    customers = _rows(supa.table("customers").select("id").eq("store_id", store_id).execute())

    actions = []
    if admin.is_owner:
        try:
            actions = _rows(
                supa.table("admin_actions").select("*").eq("store_id", store_id).order("created_at", desc=True).limit(50).execute()
            )
        except Exception as e:
            print(f"[admin] admin_actions unavailable: {e!r}")

    return {
        "store": {k: v for k, v in store.items() if admin.is_owner or k != "admin_notes"},
        "summary": summary,
        "users": users,
        "subscription": sub,
        "usage": {
            "products": summary["products_count"],
            "customers": len(customers),
            "sales_total_count": len(all_sales),
            "sales_total_amount": round(sum(float(s.get("total_price") or 0) for s in all_sales), 2),
            "sales_30d_count": summary["sales_30d_count"],
            "sales_30d_total": summary["sales_30d_total"],
            "last_sale_at": summary["last_sale_at"],
        },
        "actions": actions,
        "partners": [_public_partner(p) for p in snap.partners.values()] if admin.is_owner else [],
        "auth_unavailable": snap.auth_unavailable,
    }


class TrialBody(BaseModel):
    days: int = Field(..., ge=1, le=365)


@router.post("/stores/{store_id}/trial")
def extend_trial(store_id: str, body: TrialBody, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _load_store(supa, admin, store_id)
    sub = _ensure_sub(supa, store_id)
    if pa.describe_subscription(sub)["paying"]:
        raise HTTPException(status_code=400, detail="This store is paying already; there is no trial to extend.")

    now = _now()
    current_end = _parse_iso(sub.get("trial_end"))
    base = current_end if current_end and current_end > now else now
    new_end = base + timedelta(days=body.days)
    updated = _update_sub(supa, store_id, {"plan": "pro", "status": "trialing", "trial_end": _iso(new_end)})

    note = None
    if sub.get("paystack_subscription_code") and current_end:
        note = (
            f"A card is saved for this store and Paystack still has its first charge scheduled for "
            f"{current_end.date().isoformat()}. The store keeps Pro until {new_end.date().isoformat()} either way."
        )
    pa.log_admin_action(
        supa, admin, "trial.extend", store_id=store_id,
        details={"days": body.days, "from": sub.get("trial_end"), "to": _iso(new_end)},
    )
    return {"ok": True, "trial_end": _iso(new_end), "note": note, "summary": pa.describe_subscription(updated)}


class CompBody(BaseModel):
    until: Optional[str] = None
    months: Optional[int] = Field(None, ge=1, le=36)
    reason: Optional[str] = Field(None, max_length=300)


def _comp_until(body: CompBody) -> datetime:
    now = _now()
    if body.until:
        until = _parse_iso(body.until)
        if until and len(body.until.strip()) == 10:
            # A bare date means the whole of that day.
            until = until.replace(hour=23, minute=59, second=59)
        if not until:
            raise HTTPException(status_code=400, detail="Give the end date as YYYY-MM-DD.")
    elif body.months:
        until = now + relativedelta(months=body.months)
    else:
        raise HTTPException(status_code=400, detail="Say until when: a date or a number of months.")
    if until <= now:
        raise HTTPException(status_code=400, detail="The end date must be in the future.")
    return until


@router.post("/stores/{store_id}/comp")
def grant_comp(store_id: str, body: CompBody, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _load_store(supa, admin, store_id)
    _ensure_sub(supa, store_id)
    until = _comp_until(body)
    updated = _update_sub(supa, store_id, {"comp_until": _iso(until)})
    pa.log_admin_action(supa, admin, "comp.grant", store_id=store_id, details={"until": _iso(until), "reason": body.reason})
    return {"ok": True, "comp_until": _iso(until), "summary": pa.describe_subscription(updated)}


@router.delete("/stores/{store_id}/comp")
def revoke_comp(store_id: str, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _load_store(supa, admin, store_id)
    updated = _update_sub(supa, store_id, {"comp_until": None})
    pa.log_admin_action(supa, admin, "comp.revoke", store_id=store_id)
    return {"ok": True, "summary": pa.describe_subscription(updated)}


class SuspendBody(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)


@router.post("/stores/{store_id}/suspend")
def suspend_store(store_id: str, body: SuspendBody, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _load_store(supa, admin, store_id)
    _ensure_sub(supa, store_id)
    updated = _update_sub(supa, store_id, {"suspended_at": _iso(_now())})
    pa.log_admin_action(supa, admin, "store.suspend", store_id=store_id, details={"reason": body.reason})
    return {"ok": True, "summary": pa.describe_subscription(updated)}


@router.delete("/stores/{store_id}/suspend")
def unsuspend_store(store_id: str, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _load_store(supa, admin, store_id)
    updated = _update_sub(supa, store_id, {"suspended_at": None})
    pa.log_admin_action(supa, admin, "store.unsuspend", store_id=store_id)
    return {"ok": True, "summary": pa.describe_subscription(updated)}


class StorePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    partner_id: Optional[str] = None
    admin_notes: Optional[str] = Field(None, max_length=MAX_NOTES)


@router.patch("/stores/{store_id}")
def update_store(store_id: str, body: StorePatch, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _load_store(supa, admin, store_id)

    payload = {}
    if "name" in body.model_fields_set and body.name:
        payload["name"] = body.name.strip()
    if "partner_id" in body.model_fields_set:
        if body.partner_id:
            if not _rows(supa.table("partners").select("id").eq("id", body.partner_id).limit(1).execute()):
                raise HTTPException(status_code=404, detail="Partner not found")
            payload["partner_id"] = body.partner_id
        else:
            payload["partner_id"] = None
    if "admin_notes" in body.model_fields_set:
        payload["admin_notes"] = (body.admin_notes or "").strip() or None
    if not payload:
        raise HTTPException(status_code=400, detail="Nothing to change.")

    try:
        supa.table("stores").update(payload).eq("id", store_id).execute()
    except Exception as e:
        raise _schema_error(e)
    pa.log_admin_action(supa, admin, "store.update", store_id=store_id, details={"fields": sorted(payload.keys())})
    return {"ok": True, "store": _load_store(supa, admin, store_id)}


@router.post("/stores/{store_id}/password-reset")
def owner_password_reset(store_id: str, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    store = _load_store(supa, admin, store_id)
    acct = pa.get_auth_user(str(store.get("owner_id")))
    email = (acct or {}).get("email")
    if not email:
        raise HTTPException(status_code=404, detail="The owner's account could not be found.")
    redirect_to = normalize_frontend_base(os.getenv("FRONTEND_URL", "")) + "/auth"
    pa.send_password_reset(email, redirect_to)
    pa.log_admin_action(supa, admin, "owner.password_reset", store_id=store_id, details={"email": email})
    return {"ok": True, "email": email}


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------

def _normalize_code(code: str) -> str:
    code = (code or "").strip().upper().replace(" ", "-")
    if not PARTNER_CODE.match(code):
        raise HTTPException(status_code=400, detail="A partner code is 2 to 20 letters, digits or dashes, e.g. TECHZONE.")
    return code


def _partner_or_404(supa, partner_id: str) -> dict:
    try:
        rows = _rows(supa.table("partners").select("*").eq("id", partner_id).limit(1).execute())
    except Exception as e:
        raise _schema_error(e)
    if not rows:
        raise HTTPException(status_code=404, detail="Partner not found")
    return rows[0]


class PartnerBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    code: str = Field(..., min_length=2, max_length=24)
    contact_name: Optional[str] = Field(None, max_length=80)
    contact_phone: Optional[str] = Field(None, max_length=30)
    contact_email: Optional[str] = Field(None, max_length=120)
    notes: Optional[str] = Field(None, max_length=MAX_NOTES)


class PartnerPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    code: Optional[str] = Field(None, min_length=2, max_length=24)
    contact_name: Optional[str] = Field(None, max_length=80)
    contact_phone: Optional[str] = Field(None, max_length=30)
    contact_email: Optional[str] = Field(None, max_length=120)
    notes: Optional[str] = Field(None, max_length=MAX_NOTES)


def _partners_with_counts(supa) -> List[dict]:
    try:
        partners = _rows(supa.table("partners").select("*").order("created_at", desc=True).execute())
    except Exception as e:
        raise _schema_error(e)
    counts = Counter(s.get("partner_id") for s in _rows(supa.table("stores").select("id,partner_id").execute()))
    logins = defaultdict(list)
    for a in _rows(supa.table("platform_admins").select("user_id,email,role,partner_id").execute()):
        if a.get("role") == pa.PARTNER and a.get("partner_id"):
            logins[a["partner_id"]].append({"user_id": a.get("user_id"), "email": a.get("email")})
    out = []
    for p in partners:
        item = _public_partner(p)
        item["stores_count"] = counts.get(p["id"], 0)
        item["admins"] = logins.get(p["id"], [])
        out.append(item)
    return out


@router.get("/partners")
def list_partners(admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    return {"items": _partners_with_counts(supa)}


@router.post("/partners", status_code=201)
def create_partner(body: PartnerBody, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    code = _normalize_code(body.code)
    try:
        if _rows(supa.table("partners").select("id").eq("code", code).limit(1).execute()):
            raise HTTPException(status_code=409, detail=f"The code {code} is taken.")
        payload = {
            "name": body.name.strip(),
            "code": code,
            "contact_name": (body.contact_name or "").strip() or None,
            "contact_phone": (body.contact_phone or "").strip() or None,
            "contact_email": (body.contact_email or "").strip().lower() or None,
            "notes": (body.notes or "").strip() or None,
            "created_at": _iso(_now()),
        }
        rows = _rows(supa.table("partners").insert(payload).execute())
    except HTTPException:
        raise
    except Exception as e:
        raise _schema_error(e)
    partner = rows[0] if rows else payload
    pa.log_admin_action(supa, admin, "partner.create", partner_id=partner.get("id"), details={"code": code, "name": payload["name"]})
    item = _public_partner(partner)
    item["stores_count"] = 0
    item["admins"] = []
    return item


@router.patch("/partners/{partner_id}")
def update_partner(partner_id: str, body: PartnerPatch, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _partner_or_404(supa, partner_id)
    payload = {}
    for key in ("name", "contact_name", "contact_phone", "contact_email", "notes"):
        if key in body.model_fields_set:
            value = (getattr(body, key) or "").strip()
            if key == "name" and not value:
                raise HTTPException(status_code=400, detail="A partner needs a name.")
            payload[key] = (value.lower() if key == "contact_email" else value) or None
    if "code" in body.model_fields_set and body.code:
        code = _normalize_code(body.code)
        clash = _rows(supa.table("partners").select("id").eq("code", code).limit(1).execute())
        if clash and clash[0].get("id") != partner_id:
            raise HTTPException(status_code=409, detail=f"The code {code} is taken.")
        payload["code"] = code
    if not payload:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    try:
        supa.table("partners").update(payload).eq("id", partner_id).execute()
    except Exception as e:
        raise _schema_error(e)
    pa.log_admin_action(supa, admin, "partner.update", partner_id=partner_id, details={"fields": sorted(payload.keys())})
    return next(p for p in _partners_with_counts(supa) if p["id"] == partner_id)


class EmailBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=120)


@router.post("/partners/{partner_id}/admins", status_code=201)
def add_partner_admin(partner_id: str, body: EmailBody, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    _partner_or_404(supa, partner_id)
    acct = pa.find_auth_user_by_email(body.email)
    if not acct:
        raise HTTPException(status_code=404, detail="No KashPoint account uses that email. They need to sign up first, then add them here.")
    user_id = str(acct.get("id"))
    existing = _rows(supa.table("platform_admins").select("*").eq("user_id", user_id).limit(1).execute())
    if existing and existing[0].get("role") == pa.OWNER:
        raise HTTPException(status_code=400, detail="That account is KashPoint staff already.")
    row = {"user_id": user_id, "email": (acct.get("email") or body.email).lower(), "role": pa.PARTNER, "partner_id": partner_id}
    try:
        supa.table("platform_admins").upsert(row, on_conflict="user_id").execute()
    except Exception as e:
        raise _schema_error(e)
    pa.log_admin_action(supa, admin, "partner.admin.add", partner_id=partner_id, details={"email": row["email"]})
    return {"user_id": user_id, "email": row["email"]}


@router.delete("/partners/{partner_id}/admins/{user_id}", status_code=204)
def remove_partner_admin(partner_id: str, user_id: str, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    supa.table("platform_admins").delete().eq("user_id", user_id).eq("partner_id", partner_id).execute()
    pa.log_admin_action(supa, admin, "partner.admin.remove", partner_id=partner_id, details={"user_id": user_id})
    return None


# ---------------------------------------------------------------------------
# Staff accounts and the action log
# ---------------------------------------------------------------------------

@router.get("/admins")
def list_admins(admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    try:
        rows = _rows(supa.table("platform_admins").select("*").order("created_at").execute())
        partners = {p["id"]: p for p in _rows(supa.table("partners").select("id,name,code").execute())}
    except Exception as e:
        raise _schema_error(e)
    items = []
    for r in rows:
        p = partners.get(r.get("partner_id")) if r.get("partner_id") else None
        items.append(
            {
                "user_id": r.get("user_id"),
                "email": r.get("email"),
                "role": r.get("role"),
                "partner": {"id": p["id"], "name": p.get("name"), "code": p.get("code")} if p else None,
                "created_at": r.get("created_at"),
                "is_you": r.get("user_id") == admin.user_id,
            }
        )
    return {"items": items, "bootstrap_emails": sorted(pa.bootstrap_emails())}


@router.post("/admins", status_code=201)
def add_admin(body: EmailBody, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    acct = pa.find_auth_user_by_email(body.email)
    if not acct:
        raise HTTPException(status_code=404, detail="No KashPoint account uses that email. They need to sign up first.")
    row = {"user_id": str(acct.get("id")), "email": (acct.get("email") or body.email).lower(), "role": pa.OWNER, "partner_id": None}
    try:
        supa.table("platform_admins").upsert(row, on_conflict="user_id").execute()
    except Exception as e:
        raise _schema_error(e)
    pa.log_admin_action(supa, admin, "admin.add", details={"email": row["email"]})
    return row


@router.delete("/admins/{user_id}", status_code=204)
def remove_admin(user_id: str, admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot remove yourself.")
    supa = supabase_client.get_supabase_client()
    supa.table("platform_admins").delete().eq("user_id", user_id).execute()
    pa.log_admin_action(supa, admin, "admin.remove", details={"user_id": user_id})
    return None


@router.get("/actions")
def list_actions(limit: int = 100, store_id: str = "", admin: PlatformAdmin = Depends(get_platform_admin)):
    require_owner(admin)
    supa = supabase_client.get_supabase_client()
    limit = 100 if limit < 1 or limit > 500 else limit
    try:
        query = supa.table("admin_actions").select("*")
        if store_id:
            query = query.eq("store_id", store_id)
        rows = _rows(query.order("created_at", desc=True).limit(limit).execute())
    except Exception as e:
        raise _schema_error(e)
    names = {s["id"]: s.get("name") for s in _rows(supa.table("stores").select("id,name").execute())}
    partners = {}
    try:
        partners = {p["id"]: p.get("name") for p in _rows(supa.table("partners").select("id,name").execute())}
    except Exception:
        pass
    for r in rows:
        r["store_name"] = names.get(r.get("store_id"))
        r["partner_name"] = partners.get(r.get("partner_id"))
    return {"items": rows}
