import json
import hmac
import hashlib
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.main import app
from app.api.deps import get_current_context, RequestContext


@pytest.fixture
def admin_ctx():
    return RequestContext(user_id="u1", store_id="store_1", role="admin")


@pytest.fixture
def client(admin_ctx):
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_current_context] = lambda: admin_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@patch("app.db.supabase.get_supabase_client")
@patch("app.clients.paystack.initialize_transaction")
def test_paystack_checkout_requires_email(mock_init_tx, mock_supa, client):
    mock_supa.return_value = MagicMock()
    resp = client.post("/billing/checkout", json={"plan": "pro"})
    assert resp.status_code == 400
    assert "email" in resp.json()["detail"].lower()


def _subscription_select_chain_mock(trial_consumed_at=None):
    exec_result = MagicMock(data={"trial_consumed_at": trial_consumed_at})
    chain = MagicMock()
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.single.return_value = chain
    chain.execute.return_value = exec_result
    return chain


@patch("app.db.supabase.get_supabase_client")
@patch("app.clients.paystack.initialize_transaction")
def test_paystack_checkout_returns_authorization_url(mock_init_tx, mock_supa, client, monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    mock_supa.return_value.table.return_value = _subscription_select_chain_mock(None)
    mock_init_tx.return_value = {
        "url": "https://checkout.paystack.com/abc",
        "reference": "ref_test_abc",
    }

    resp = client.post("/billing/checkout", json={"plan": "pro", "email": "a@b.com"})
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://")
    mock_init_tx.assert_called_once()
    assert "plan_code" not in mock_init_tx.call_args.kwargs


@patch("app.core.http_config.allowed_origins", ["*"])
@patch("app.db.supabase.get_supabase_client")
@patch("app.clients.paystack.initialize_transaction")
def test_paystack_checkout_callback_prefers_browser_origin(mock_init_tx, mock_supa, client, monkeypatch):
    """When FRONTEND_URL is wrong, Origin from the SPA should set Paystack callback_url."""
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:5001")
    mock_supa.return_value.table.return_value = _subscription_select_chain_mock(None)
    mock_init_tx.return_value = {
        "url": "https://checkout.paystack.com/abc",
        "reference": "ref_test_abc",
    }

    resp = client.post(
        "/billing/checkout",
        json={"plan": "pro", "email": "a@b.com"},
        headers={"Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 200
    kwargs = mock_init_tx.call_args.kwargs
    assert "http://localhost:5000" in kwargs["callback_url"]
    assert "5001" not in kwargs["callback_url"]


@patch("app.core.http_config.allowed_origins", ["http://localhost:5001"])
@patch("app.db.supabase.get_supabase_client")
@patch("app.clients.paystack.initialize_transaction")
def test_paystack_checkout_callback_uses_spa_port_when_cors_lists_other_loopback_port(
    mock_init_tx, mock_supa, client, monkeypatch
):
    """Explicit CORS for :5001 must not force Paystack redirect to :5001 when the SPA is on :5000."""
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:5001")
    mock_supa.return_value.table.return_value = _subscription_select_chain_mock(None)
    mock_init_tx.return_value = {
        "url": "https://checkout.paystack.com/abc",
        "reference": "ref_test_abc",
    }

    resp = client.post(
        "/billing/checkout",
        json={"plan": "pro", "email": "a@b.com"},
        headers={"X-App-Origin": "http://localhost:5000"},
    )
    assert resp.status_code == 200
    kwargs = mock_init_tx.call_args.kwargs
    assert kwargs["callback_url"].startswith("http://localhost:5000/billing")
    assert "5001" not in kwargs["callback_url"]


def test_get_billing_redirects_to_spa_preserving_paystack_query(client, monkeypatch):
    """If the browser hits the API /billing after Paystack, forward to FRONTEND_URL (Vite)."""
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:5000")
    resp = client.get(
        "/billing?success=1&trxref=ffg6lel07t&reference=ffg6lel07t",
        follow_redirects=False,
    )
    assert resp.status_code == 307
    assert resp.headers["location"] == (
        "http://localhost:5000/billing?success=1&trxref=ffg6lel07t&reference=ffg6lel07t"
    )


def _sign(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha512).hexdigest()


@patch("app.db.supabase.get_supabase_client")
def test_paystack_webhook_rejects_bad_signature(mock_supa, client, monkeypatch):
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY_TEST", "sk_test_x")
    mock_supa.return_value = MagicMock()
    payload = {"event": "charge.success", "data": {"id": 1}}
    raw = json.dumps(payload).encode("utf-8")

    resp = client.post("/paystack/webhook", content=raw, headers={"x-paystack-signature": "bad"})
    assert resp.status_code == 400


@patch("app.db.supabase.get_supabase_client")
def test_paystack_webhook_charge_success_upserts_subscription(mock_supa, client, monkeypatch):
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY_TEST", "sk_test_x")
    mock_client = MagicMock()
    mock_supa.return_value = mock_client

    payload = {
        "event": "charge.success",
        "data": {
            "id": 123,
            "metadata": {"store_id": "store_1", "plan": "pro"},
            "customer": {"customer_code": "CUS_x"},
            "subscription": {"subscription_code": "SUB_x", "email_token": "EMT_x"},
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = _sign("sk_test_x", raw)

    resp = client.post("/paystack/webhook", content=raw, headers={"x-paystack-signature": sig})
    assert resp.status_code == 200, resp.text

    # Dedup insert + upsert should happen
    assert mock_client.table.call_count >= 1
    # Ensure subscriptions upsert called with active status
    subs_table = mock_client.table.return_value
    upsert_calls = [c for c in subs_table.upsert.call_args_list]
    assert any((c.args and c.args[0].get("status") == "active") for c in upsert_calls)


def _supa_with_customer_row(row):
    """Supabase mock whose subscriptions table resolves a customer-code lookup to `row`."""
    subs = MagicMock()
    subs.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[row] if row else []
    )
    subs.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(data={})
    client = MagicMock()
    client.table.side_effect = lambda name: subs if name == "subscriptions" else MagicMock()
    return client, subs


@patch("app.db.supabase.get_supabase_client")
def test_webhook_subscription_create_without_metadata_resolves_store_by_customer(mock_supa, client, monkeypatch):
    """Paystack's subscription.* events carry no metadata: the customer code
    recorded at checkout is the only link back to the store."""
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY_TEST", "sk_test_x")
    mock_client, subs = _supa_with_customer_row({"store_id": "store_1", "paystack_subscription_code": None})
    mock_supa.return_value = mock_client

    payload = {
        "event": "subscription.create",
        "data": {
            "id": 555,
            "customer": {"customer_code": "CUS_x"},
            "subscription_code": "SUB_new",
            "email_token": "EMT_new",
            "next_payment_date": "2026-12-01T00:00:00.000Z",
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    resp = client.post("/paystack/webhook", content=raw, headers={"x-paystack-signature": _sign("sk_test_x", raw)})
    assert resp.status_code == 200, resp.text

    upserted = subs.upsert.call_args[0][0]
    assert upserted["store_id"] == "store_1"
    assert upserted["status"] == "active"
    assert upserted["paystack_subscription_code"] == "SUB_new"
    assert upserted["paystack_email_token"] == "EMT_new"
    assert upserted["current_period_end"] == "2026-12-01T00:00:00.000Z"


@patch("app.clients.paystack.fetch_subscription_by_code")
@patch("app.db.supabase.get_supabase_client")
def test_webhook_renewal_charge_without_metadata_advances_period_end(mock_supa, mock_fetch, client, monkeypatch):
    """Monthly renewals are charges Paystack raises itself: no metadata and no
    subscription object. The stored subscription code must drive the refresh,
    or a paying store's paid-through date never moves and it gets locked out."""
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY_TEST", "sk_test_x")
    mock_client, subs = _supa_with_customer_row({"store_id": "store_1", "paystack_subscription_code": "SUB_x"})
    mock_supa.return_value = mock_client
    mock_fetch.return_value = {"next_payment_date": "2027-01-01T00:00:00.000Z"}

    payload = {
        "event": "charge.success",
        "data": {"id": 777, "customer": {"customer_code": "CUS_x"}, "amount": 15000},
    }
    raw = json.dumps(payload).encode("utf-8")
    resp = client.post("/paystack/webhook", content=raw, headers={"x-paystack-signature": _sign("sk_test_x", raw)})
    assert resp.status_code == 200, resp.text

    upserted = subs.upsert.call_args[0][0]
    assert upserted["store_id"] == "store_1"
    assert upserted["status"] == "active"
    assert upserted["paystack_subscription_code"] == "SUB_x"
    assert upserted["current_period_end"] == "2027-01-01T00:00:00.000Z"
    mock_fetch.assert_called_once_with("SUB_x")


@patch("app.db.supabase.get_supabase_client")
def test_webhook_without_metadata_and_unknown_customer_is_ignored(mock_supa, client, monkeypatch):
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY_TEST", "sk_test_x")
    mock_client, subs = _supa_with_customer_row(None)
    mock_supa.return_value = mock_client

    payload = {
        "event": "subscription.create",
        "data": {"id": 999, "customer": {"customer_code": "CUS_stranger"}, "subscription_code": "SUB_z"},
    }
    raw = json.dumps(payload).encode("utf-8")
    resp = client.post("/paystack/webhook", content=raw, headers={"x-paystack-signature": _sign("sk_test_x", raw)})
    assert resp.status_code == 200, resp.text
    subs.upsert.assert_not_called()


@patch("app.clients.paystack.fetch_subscription_by_code")
@patch("app.clients.paystack.verify_transaction")
@patch("app.db.supabase.get_supabase_client")
def test_paystack_sync_sets_current_period_end_from_paystack(
    mock_supa, mock_verify, mock_fetch, client, monkeypatch
):
    """Resubscribe should refresh next billing date from Paystack (not keep an old DB value)."""
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    store = str(uuid.uuid4())
    mock_verify.return_value = {
        "status": "success",
        "metadata": {"store_id": store, "plan": "pro"},
        "subscription_code": "SUB_x",
        "email_token": "EMT_x",
        "customer": {"customer_code": "CUS_x"},
    }
    mock_fetch.return_value = {"next_payment_date": "2026-05-01T00:00:00.000Z"}
    mock_table = MagicMock()
    mock_table.select.return_value = _subscription_select_chain_mock(None)
    mock_table.upsert.return_value = MagicMock(execute=MagicMock())
    mock_supa.return_value.table.return_value = mock_table

    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user_id="u1", store_id=store, role="admin"
    )
    try:
        resp = client.post("/billing/paystack/sync", json={"reference": "ref_123"})
        assert resp.status_code == 200, resp.text
        upserted = mock_table.upsert.call_args[0][0]
        assert upserted.get("current_period_end") == "2026-05-01T00:00:00.000Z"
    finally:
        app.dependency_overrides.clear()


@patch("app.clients.paystack.verify_transaction")
@patch("app.db.supabase.get_supabase_client")
def test_paystack_sync_persists_subscription_codes(mock_supa, mock_verify, client, monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    store = str(uuid.uuid4())
    mock_verify.return_value = {
        "status": "success",
        "metadata": {"store_id": store, "plan": "pro"},
        "subscription_code": "SUB_x",
        "email_token": "EMT_x",
        "customer": {"customer_code": "CUS_x"},
    }
    mock_supa.return_value = MagicMock()

    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user_id="u1", store_id=store, role="admin"
    )
    try:
        resp = client.post("/billing/paystack/sync", json={"reference": "ref_123"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["synced"] is True
        assert data["has_subscription"] is True
    finally:
        app.dependency_overrides.clear()


@patch("app.clients.paystack.verify_transaction")
def test_paystack_sync_rejects_wrong_store(mock_verify, client, monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    mock_verify.return_value = {
        "status": "success",
        "metadata": {"store_id": str(uuid.uuid4()), "plan": "pro"},
        "subscription_code": "SUB_x",
        "email_token": "EMT_x",
    }
    other_store = str(uuid.uuid4())
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user_id="u1", store_id=other_store, role="admin"
    )
    try:
        resp = client.post("/billing/paystack/sync", json={"reference": "ref_123"})
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()

