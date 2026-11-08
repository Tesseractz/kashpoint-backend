import app.db.supabase as supabase_client
import app.services.subscriptions as subscriptions
from app.core.time_utils import now_utc_iso


def log_audit_event(
    store_id: str,
    user_id: str,
    action: str,
    resource_type: str,
    resource_id: str = None,
    details: str = None,
):
    """Record a store-level change (product edited, user invited...).

    The table's columns are entity_type / entity_id / created_at (see
    backend/scripts/migrations.sql); the callers' vocabulary of resource_type /
    resource_id / timestamp is mapped here. Writing the callers' names, as this
    did before, failed on every call and left the audit log empty.
    """
    try:
        supabase = supabase_client.get_supabase_client()
        limits = subscriptions.get_store_plan(store_id)
        if limits.allow_audit_logs:
            supabase.table("audit_logs").insert(
                {
                    "store_id": store_id,
                    "user_id": user_id,
                    "action": action,
                    "entity_type": resource_type,
                    "entity_id": None if resource_id is None else str(resource_id),
                    "details": details,
                    "created_at": now_utc_iso(),
                }
            ).execute()
    except Exception:
        pass
