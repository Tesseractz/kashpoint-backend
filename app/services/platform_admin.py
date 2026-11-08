"""Who may use the admin portal, and the bookkeeping behind it.

Two kinds of people sit above the stores: KashPoint staff (role `owner`, who
see and manage every store) and partners (role `partner`: the computer shops
that sell tills with KashPoint on them, who see the stores that signed up with
their partner code and nothing else). Both are rows in `platform_admins`,
keyed by the Supabase user id, so a partner opens the portal with the same
account they run their own shop with.

`PLATFORM_ADMIN_EMAILS` (comma-separated) bootstraps the first owners without
a SQL round trip: an email on that list is an owner even before its row
exists, and the row is written on first use so the portal can list it.
"""
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import Header, HTTPException

import app.db.supabase as supabase_client
from app.core.auth import verify_supabase_jwt
from app.db.supabase import retry_transient
from app.services.subscriptions import _limits_from_row, _parse_iso

OWNER = "owner"
PARTNER = "partner"
ROLES = (OWNER, PARTNER)

AUTH_PAGE_SIZE = 1000


@dataclass
class PlatformAdmin:
    user_id: str
    email: str
    role: str
    partner_id: Optional[str] = None

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER

    def as_dict(self) -> dict:
        return {"user_id": self.user_id, "email": self.email, "role": self.role, "partner_id": self.partner_id}


def bootstrap_emails() -> set:
    raw = os.getenv("PLATFORM_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _row_to_admin(row: dict) -> PlatformAdmin:
    role = row.get("role") if row.get("role") in ROLES else OWNER
    return PlatformAdmin(
        user_id=str(row.get("user_id")),
        email=str(row.get("email") or "").lower(),
        role=role,
        partner_id=row.get("partner_id") if role == PARTNER else None,
    )


def lookup_platform_admin(supa, user_id: str, email: Optional[str] = None) -> Optional[PlatformAdmin]:
    """The admin row for this user, or None.

    An email on the bootstrap list is an owner even without a row, and the row
    is written so the portal lists them. A failed read (the migration has not
    been run yet, a transient error) is treated as "no row": the portal stays
    closed rather than the request failing.
    """
    row = None
    try:
        res = retry_transient(
            lambda: supa.table("platform_admins").select("*").eq("user_id", user_id).limit(1).execute()
        )
        rows = getattr(res, "data", None) or []
        row = rows[0] if rows else None
    except Exception as e:
        print(f"[admin] platform_admins lookup failed: {e!r}")
    if row:
        return _row_to_admin(row)

    if email and email.lower() in bootstrap_emails():
        admin = PlatformAdmin(user_id=user_id, email=email.lower(), role=OWNER)
        try:
            supa.table("platform_admins").upsert(
                {"user_id": user_id, "email": admin.email, "role": OWNER}, on_conflict="user_id"
            ).execute()
        except Exception as e:
            print(f"[admin] could not record bootstrap owner {admin.email}: {e!r}")
        return admin
    return None


def get_platform_admin(authorization: Optional[str] = Header(None)) -> PlatformAdmin:
    """FastAPI dependency: the caller must be a platform admin."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = verify_supabase_jwt(token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    admin = lookup_platform_admin(supabase_client.get_supabase_client(), user_id, payload.get("email"))
    if not admin:
        raise HTTPException(status_code=403, detail="This account is not a KashPoint platform admin.")
    return admin


def require_owner(admin: PlatformAdmin) -> None:
    if not admin.is_owner:
        raise HTTPException(status_code=403, detail="Only KashPoint staff can do this.")


def log_admin_action(
    supa,
    admin: PlatformAdmin,
    action: str,
    store_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Record what an admin did. Never fails the action it describes."""
    try:
        supa.table("admin_actions").insert(
            {
                "actor_id": admin.user_id,
                "actor_email": admin.email,
                "action": action,
                "store_id": store_id,
                "partner_id": partner_id,
                "details": details or {},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except Exception as e:
        print(f"[admin] could not log {action} by {admin.email}: {e!r}")


# ---------------------------------------------------------------------------
# Supabase Auth: the accounts behind the profiles (emails, last sign-in).
# ---------------------------------------------------------------------------

def _auth_base() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def _auth_headers() -> dict:
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def list_auth_users() -> List[dict]:
    """Every account in Supabase Auth. Pages of 1000; a few hundred stores is
    one call."""
    out: List[dict] = []
    page = 1
    with httpx.Client(timeout=15) as client:
        while True:
            resp = client.get(
                f"{_auth_base()}/auth/v1/admin/users",
                headers=_auth_headers(),
                params={"page": page, "per_page": AUTH_PAGE_SIZE},
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Could not read accounts from Supabase Auth (HTTP {resp.status_code}).",
                )
            users = (resp.json() or {}).get("users") or []
            out.extend(users)
            if len(users) < AUTH_PAGE_SIZE:
                return out
            page += 1


def auth_index() -> dict:
    """Auth users keyed by id, trimmed to what the portal shows."""
    index = {}
    for u in list_auth_users():
        index[str(u.get("id"))] = {
            "id": str(u.get("id")),
            "email": u.get("email"),
            "created_at": u.get("created_at"),
            "last_sign_in_at": u.get("last_sign_in_at"),
            "email_confirmed": bool(u.get("email_confirmed_at")),
        }
    return index


def get_auth_user(user_id: str) -> Optional[dict]:
    """One account by id, or None when Supabase Auth has no such user."""
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{_auth_base()}/auth/v1/admin/users/{user_id}", headers=_auth_headers())
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    raise HTTPException(status_code=502, detail=f"Could not read the account from Supabase Auth (HTTP {resp.status_code}).")


def find_auth_user_by_email(email: str) -> Optional[dict]:
    wanted = (email or "").strip().lower()
    if not wanted:
        return None
    for u in list_auth_users():
        if (u.get("email") or "").lower() == wanted:
            return u
    return None


def send_password_reset(email: str, redirect_to: str) -> None:
    """Ask Supabase Auth to email a password-reset link, the same one the
    'Forgot password' form sends."""
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{_auth_base()}/auth/v1/recover",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            params={"redirect_to": redirect_to} if redirect_to else None,
            json={"email": email},
        )
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"Supabase Auth did not accept the reset request (HTTP {resp.status_code}).")


# ---------------------------------------------------------------------------
# Where a store stands, in one line.
# ---------------------------------------------------------------------------

TRIAL_ENDING_DAYS = 7

STATE_LABELS = {
    "suspended": "Suspended",
    "comped": "Complimentary Pro",
    "trial": "On trial",
    "trial_ending": "Trial ending",
    "active": "Paying",
    "past_due": "Payment failed",
    "trial_ended": "Trial ended",
    "canceled": "Canceled",
    "expired": "Expired",
    "none": "No subscription",
}


def describe_subscription(row: Optional[dict], now: Optional[datetime] = None) -> dict:
    """The portal's one-line view of a subscription row: a `state` from
    STATE_LABELS plus the dates behind it. Derived the same way the app
    decides what a store may use (PlanLimits), so the portal never says
    'paying' about a store the till treats as expired."""
    now = now or datetime.now(timezone.utc)
    row = row or {}
    limits = _limits_from_row(row) if row else None
    trial_end = _parse_iso(row.get("trial_end"))
    comp_until = _parse_iso(row.get("comp_until"))
    status = (row.get("status") or "").lower()
    card = bool(row.get("paystack_subscription_code"))

    days_left = None
    if not row:
        state = "none"
    elif row.get("suspended_at"):
        state = "suspended"
    elif comp_until and comp_until > now:
        state = "comped"
        days_left = max(0, (comp_until - now).days)
    elif limits.is_on_trial:
        days_left = max(1, math.ceil((trial_end - now).total_seconds() / 86400))
        state = "trial_ending" if days_left <= TRIAL_ENDING_DAYS else "trial"
    elif status == "past_due":
        state = "past_due"
    elif limits.is_active:
        state = "active"
    elif status == "trialing" and trial_end and trial_end <= now:
        state = "trial_ended"
    elif status == "canceled":
        state = "canceled"
    else:
        state = "expired"

    return {
        "state": state,
        "label": STATE_LABELS[state],
        "days_left": days_left,
        "plan": row.get("plan"),
        "status": status or None,
        "trial_end": row.get("trial_end"),
        "paid_through": row.get("current_period_end"),
        "comp_until": row.get("comp_until"),
        "suspended_at": row.get("suspended_at"),
        "card_on_file": card and status in ("trialing", "active", "past_due"),
        "is_active": bool(limits.is_active) if limits else False,
        "paying": state == "active" and card,
    }
