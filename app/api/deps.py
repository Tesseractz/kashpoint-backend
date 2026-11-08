from typing import Optional

from fastapi import Header, HTTPException, status

from app.core.auth import verify_supabase_jwt
from app.core.phone import normalize_phone
from app.db.supabase import get_supabase_client, retry_transient
from app.services.subscriptions import ensure_store_subscription


class RequestContext:
    def __init__(self, user_id: str, store_id: str, role: str, email: Optional[str] = None):
        self.user_id = user_id
        self.store_id = store_id
        self.role = role
        self.email = email


def _missing_column(err: Exception, column: str) -> bool:
    """PostgREST's answer when a write names a column the table doesn't have."""
    text = str(err)
    return f"'{column}'" in text and ("PGRST204" in text or "column" in text.lower())


# Columns later migrations add to `stores`. A signup must never fail over one
# of them being absent: the phone number is also in the user's auth metadata,
# and a partner link can be set from the admin portal afterwards.
OPTIONAL_STORE_COLUMNS = ("phone", "partner_id")


def _insert_store(supa, payload: dict):
    """Insert the store, dropping optional columns the table doesn't have yet."""
    current = dict(payload)
    for _ in range(len(OPTIONAL_STORE_COLUMNS)):
        try:
            return supa.table("stores").insert(current).execute()
        except Exception as e:
            missing = [c for c in OPTIONAL_STORE_COLUMNS if c in current and _missing_column(e, c)]
            if not missing:
                raise
            print(f"[signup] stores.{missing[0]} column missing; saving the store without it")
            current = {k: v for k, v in current.items() if k not in missing}
    return supa.table("stores").insert(current).execute()


def _partner_id_for_code(supa, code) -> Optional[str]:
    """The partner a signup code belongs to, or None.

    The code is what a computer shop gives the stores it sets up, so their
    stores appear in that partner's view of the admin portal. A wrong or
    unknown code never blocks a signup: the store is created without a partner
    and staff can link it later.
    """
    code = str(code or "").strip().upper()
    if not code:
        return None
    try:
        res = supa.table("partners").select("id").eq("code", code).limit(1).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]["id"]
        print(f"[signup] unknown partner code {code!r}; store created without a partner")
    except Exception as e:
        print(f"[signup] partner lookup for {code!r} failed: {e!r}")
    return None


def _create_store_and_profile(supa, user_id: str, user_metadata: dict) -> dict:
    """Create — or finish creating — the store, profile and trial subscription
    of a new account.

    Every request of a fresh account's first page load lands here at the same
    time, so each step is idempotent and resumable: a request cut short
    half-way is completed by the next one, and all of them converge on one
    store, one profile and one subscription row. Before this, a signup
    interrupted after the profile step left a store with no subscription row
    at all, which read as "expired" and later let checkout plant a free row
    in place of the 30-day trial.
    """
    store_name = user_metadata.get("store_name", "My Store")
    store_payload = {"name": store_name, "owner_id": user_id}
    phone = normalize_phone(user_metadata.get("phone"))
    if phone:
        store_payload["phone"] = phone
    partner_id = _partner_id_for_code(supa, user_metadata.get("partner_code"))
    if partner_id:
        store_payload["partner_id"] = partner_id

    def find_store() -> Optional[str]:
        res = (
            supa.table("stores")
            .select("id")
            .eq("owner_id", user_id)
            .order("created_at")
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0]["id"] if rows else None

    store_id = retry_transient(find_store)
    if not store_id:
        try:
            res = retry_transient(lambda: _insert_store(supa, store_payload))
            rows = getattr(res, "data", None) or []
            store_id = rows[0]["id"] if rows else None
        except Exception:
            # Another request of the same burst may have created it meanwhile.
            store_id = retry_transient(find_store)
            if not store_id:
                raise
    if not store_id:
        raise HTTPException(status_code=500, detail="Failed to create store")

    profile = {
        "id": user_id,
        "name": user_metadata.get("email", "User"),
        "role": "admin",
        "store_id": store_id,
    }
    # The signup form's optional "product updates and tips" box. It was shown
    # but never stored before.
    if "marketing_opt_in" in user_metadata:
        profile["marketing_emails_enabled"] = bool(user_metadata.get("marketing_opt_in"))

    def write_profile() -> Optional[dict]:
        res = supa.table("profiles").upsert(profile, on_conflict="id", ignore_duplicates=True).execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    def read_profile() -> Optional[dict]:
        res = supa.table("profiles").select("*").eq("id", user_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    try:
        row = retry_transient(write_profile)
    except Exception:
        row = None
    if row is None:
        # ignore-duplicates returns nothing when the profile already existed.
        row = retry_transient(read_profile)
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create profile")

    # The subscription is the step most often cut short, and it must not fail
    # the request: the profile exists now, and get_store_plan creates the
    # trial on the next read if this attempt does not get there either.
    try:
        retry_transient(lambda: ensure_store_subscription(supa, store_id))
    except Exception as e:
        print(f"[signup] store {store_id}: trial subscription not created yet: {e!r}")

    return row


def get_current_context(authorization: Optional[str] = Header(None)) -> RequestContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    token = authorization.split(" ", 1)[1]

    try:
        payload = verify_supabase_jwt(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    supa = get_supabase_client()

    try:
        prof_result = retry_transient(
            lambda: supa.table("profiles").select("*").eq("id", user_id).execute()
        )
    except Exception as e:
        print(f"[get_current_context] Error querying profiles: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    if prof_result.data and len(prof_result.data) > 0:
        profile = prof_result.data[0]
    else:
        user_metadata = payload.get("user_metadata", {})
        if not user_metadata:
            user_metadata = {"store_name": "My Store", "email": payload.get("email", "User")}

        try:
            profile = _create_store_and_profile(supa, user_id, user_metadata)
        except Exception as e:
            err = str(e)
            if "23505" in err or "duplicate" in err.lower():
                prof_result = supa.table("profiles").select("*").eq("id", user_id).execute()
                if prof_result.data and len(prof_result.data) > 0:
                    profile = prof_result.data[0]
                else:
                    raise HTTPException(status_code=500, detail="Failed to get or create profile")
            elif "23503" in err or "foreign key" in err.lower() or "violates foreign key" in err.lower():
                # auth.users(id) is gone — JWT belongs to a deleted account.
                # Force a clean re-auth instead of leaking a 500.
                raise HTTPException(status_code=401, detail="Account no longer exists")
            else:
                print(f"[get_current_context] Error creating profile: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to create profile: {str(e)}")

    store_id = profile.get("store_id")
    role = profile.get("role", "cashier")

    if not store_id:
        raise HTTPException(status_code=500, detail="Profile missing store_id")

    return RequestContext(user_id=user_id, store_id=store_id, role=role, email=payload.get("email"))
