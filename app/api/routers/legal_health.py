import os
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.time_utils import now_utc_iso

router = APIRouter()


@router.get("/legal/terms")
def get_terms_of_service():
    return {
        "version": "1.0",
        "effective_date": "2026-01-01",
        "content_url": "/terms",
        "last_updated": "2026-01-01",
    }


@router.get("/legal/privacy")
def get_privacy_policy():
    return {
        "version": "1.0",
        "effective_date": "2026-01-01",
        "content_url": "/privacy",
        "last_updated": "2026-01-01",
    }


class HealthResponse(BaseModel):
    status: str
    time: str
    # The git commit this process was built from (Render sets RENDER_GIT_COMMIT),
    # so a deploy can be verified by SHA rather than by guessing from behaviour.
    commit: Optional[str] = None


@router.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok", "time": now_utc_iso(), "commit": os.getenv("RENDER_GIT_COMMIT") or None}
