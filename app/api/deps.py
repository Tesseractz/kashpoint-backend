from typing import Optional

from fastapi import Header, HTTPException, status

from app.core.auth import verify_supabase_jwt
from app.db.supabase import get_supabase_client, retry_transient
from app.services.subscriptions import ensure_store_subscription


class RequestContext:
    def __init__(self, user_id: str, store_id: str, role: str):
        self.user_id = user_id
        self.store_id = store_id
        self.role = role


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
            res = retry_transient(
                lambda: supa.table("stores").insert({"name": store_name, "owner_id": user_id}).execute()
            )
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

    return RequestContext(user_id=user_id, store_id=store_id, role=role)
