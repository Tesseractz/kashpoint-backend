"""The store's receipt profile: the business details printed on every slip."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import app.db.supabase as supabase_client
from app.api.deps import RequestContext, get_current_context
from app.services.store_profile import InvalidField, MissingColumns, read_store, update_store

router = APIRouter()

MIGRATION_NEEDED = (
    "Receipt details aren't switched on in the database yet. "
    "KashPoint needs to run the 'stores' update (migration 20261102130000)."
)


class StoreProfileUpdate(BaseModel):
    name: Optional[str] = None
    logo_url: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    vat_number: Optional[str] = None
    registration_number: Optional[str] = None
    receipt_footer: Optional[str] = None
    website: Optional[str] = None


@router.get("/store")
def get_store(ctx: RequestContext = Depends(get_current_context)):
    supa = supabase_client.get_supabase_client()
    try:
        return read_store(supa, ctx.store_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read store details: {e}")


@router.put("/store")
def put_store(body: StoreProfileUpdate, ctx: RequestContext = Depends(get_current_context)):
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    supa = supabase_client.get_supabase_client()
    # Only the fields that were sent: an omitted field stays as it is, an
    # empty string clears it.
    changes = body.model_dump(exclude_unset=True)
    try:
        return update_store(supa, ctx.store_id, changes)
    except InvalidField as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MissingColumns:
        raise HTTPException(status_code=409, detail=MIGRATION_NEEDED)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save store details: {e}")
