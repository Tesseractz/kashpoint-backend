from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import app.services.analytics as analytics_mod
import app.services.subscriptions as subscriptions
import app.db.supabase as supabase_client
from app.services.analytics import AnalyticsSummary
from app.services.store_profile import empty_profile, read_store
from app.api.deps import RequestContext, get_current_context

router = APIRouter()


@router.get("/profile")
def get_profile(ctx: RequestContext = Depends(get_current_context)):
    supabase = supabase_client.get_supabase_client()
    try:
        prof_result = supabase.table("profiles").select("*").eq("id", ctx.user_id).execute()

        if prof_result.data and len(prof_result.data) > 0:
            profile_data = prof_result.data[0]
            store_id = profile_data.get("store_id", ctx.store_id)
            try:
                store = read_store(supabase, store_id)
            except Exception:
                store = empty_profile(store_id)
            return {
                "id": profile_data["id"],
                "name": profile_data.get("name"),
                "role": profile_data.get("role", ctx.role),
                "store_id": store_id,
                "store_name": store.get("name"),
                # The receipt profile: logo, address, VAT number, footer...
                "store": store,
            }
        else:
            return {
                "id": ctx.user_id,
                "name": None,
                "role": ctx.role,
                "store_id": ctx.store_id,
                "store_name": None,
                "store": empty_profile(ctx.store_id),
            }
    except Exception as e:
        print(f"[Profile API] Error fetching profile: {e}")
        return {
            "id": ctx.user_id,
            "name": None,
            "role": ctx.role,
            "store_id": ctx.store_id,
            "store_name": None,
            "store": empty_profile(ctx.store_id),
        }


@router.get("/plan")
def get_current_plan(ctx: RequestContext = Depends(get_current_context)):
    return subscriptions.get_plan_info(ctx.store_id)


class LowStockProduct(BaseModel):
    id: int
    sku: Optional[str] = None
    name: str
    quantity: int
    threshold: int = 10


@router.get("/alerts/low-stock", response_model=List[LowStockProduct])
def get_low_stock_alerts(threshold: int = 10, ctx: RequestContext = Depends(get_current_context)):
    limits = subscriptions.get_store_plan(ctx.store_id)
    if not limits.allow_low_stock_alerts:
        raise HTTPException(status_code=402, detail="Low-stock alerts require Pro or Business plan")

    if threshold < 0 or threshold > 1000:
        threshold = 10

    supabase = supabase_client.get_supabase_client()
    try:
        res = (
            supabase.table("products")
            .select("id,sku,name,quantity")
            .eq("store_id", ctx.store_id)
            .lte("quantity", threshold)
            .order("quantity")
            .execute()
        )
        products = res.data or []
        return [{"threshold": threshold, **p} for p in products]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AuditLogEntry(BaseModel):
    id: int
    user_id: str
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    details: Optional[str] = None
    timestamp: datetime


@router.get("/audit-logs", response_model=List[AuditLogEntry])
def get_audit_logs(limit: int = 50, ctx: RequestContext = Depends(get_current_context)):
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")

    limits = subscriptions.get_store_plan(ctx.store_id)
    if not limits.allow_audit_logs:
        raise HTTPException(status_code=402, detail="Audit logs require Business plan")

    if limit < 1 or limit > 200:
        limit = 50

    supabase = supabase_client.get_supabase_client()
    try:
        res = (
            supabase.table("audit_logs")
            .select("*")
            .eq("store_id", ctx.store_id)
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as e:
        return []


@router.get("/analytics", response_model=AnalyticsSummary)
def get_store_analytics(days: int = 30, ctx: RequestContext = Depends(get_current_context)):
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    limits = subscriptions.get_store_plan(ctx.store_id)
    if not limits.allow_advanced_reports:
        raise HTTPException(status_code=402, detail="Advanced analytics require Pro or Business plan")

    if days < 1:
        days = 1
    elif days > 365:
        days = 365

    return analytics_mod.get_analytics(ctx.store_id, days)
