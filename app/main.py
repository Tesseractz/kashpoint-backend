from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)

from app.api.routers import billing, customers, expenses, legal_health, notifications, privacy, products, profile_plan, reports, store, users
from app.core.http_config import allowed_origins
from app.core.password_crypto import _decrypt_password, _encrypt_password
from app.services.rate_limit import get_rate_limiter

app = FastAPI(title="KashPoint API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Endpoints that get rate-limited. Everything else is unmetered — these
# four are the ones that touch external paid services (Paystack) or are
# obvious abuse targets.
_RL_PATHS = {
    ("POST", "/billing/checkout"),
    ("POST", "/billing/paystack/sync"),
    ("POST", "/paystack/webhook"),
}


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    method = request.method.upper()
    path = request.url.path

    if (method, path) in _RL_PATHS:
        forwarded_for = request.headers.get("x-forwarded-for")
        ip = (forwarded_for.split(",")[0].strip() if forwarded_for else None) or (
            request.client.host if request.client else "unknown"
        )
        key = f"{ip}:{method}:{path}"

        limiter = get_rate_limiter()
        allowed, retry_after = limiter.check(key)
        if not allowed:
            return Response(
                content='{"detail":"Too many requests"}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

    return await call_next(request)


app.include_router(products.router)
app.include_router(billing.router)
app.include_router(profile_plan.router)
app.include_router(store.router)
app.include_router(users.router)
app.include_router(reports.router)
app.include_router(customers.router)
app.include_router(notifications.router)
app.include_router(expenses.router)
app.include_router(privacy.router)
app.include_router(legal_health.router)
