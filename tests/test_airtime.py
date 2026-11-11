"""Airtime through SIMcloud: switching it on for a shop, selling, and the
service fee that is the shop's only profit. SIMcloud itself is replaced by
fakes; nothing here places a real order.
"""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.clients import simcloud
from app.clients.simcloud import SimcloudError
from app.core import password_crypto
from tests.test_admin_portal import FakeSupabase


@pytest.fixture
def db():
    d = FakeSupabase()
    d.keys = {**FakeSupabase.keys, "airtime_accounts": "store_id", "airtime_sales": "id"}
    d.tables["airtime_accounts"] = []
    d.tables["airtime_sales"] = []
    d.add("stores", id="s1", name="Corner Cafe", owner_id="u1")
    return d


@pytest.fixture(autouse=True)
def env(db):
    with patch("app.db.supabase.get_supabase_client", return_value=db), patch.dict(os.environ, {"SIMCLOUD_API_TOKEN": "parent-token"}):
        yield


def _client(role="admin"):
    from app.main import app
    from app.api.deps import RequestContext, get_current_context

    app.dependency_overrides[get_current_context] = lambda: RequestContext("u1", "s1", role, email="owner@example.com")
    return TestClient(app)


@pytest.fixture
def owner():
    from app.main import app

    yield _client("admin")
    app.dependency_overrides.clear()


@pytest.fixture
def cashier():
    from app.main import app

    yield _client("cashier")
    app.dependency_overrides.clear()


def _activate(db, fee=0):
    db.add(
        "airtime_accounts", store_id="s1", simcloud_user_id="77", cell="0821234567",
        api_token_encrypted=password_crypto._encrypt_password("child-token"), service_fee=fee,
        terms_accepted_at="2026-11-11T10:00:00+00:00",
    )


def test_normalize_cell():
    assert simcloud.normalize_cell("082 123 4567") == "0821234567"
    assert simcloud.normalize_cell("+27821234567") == "0821234567"
    assert simcloud.normalize_cell("12345") is None


def test_status_before_activation(owner):
    body = owner.get("/airtime/status").json()
    assert body["available"] is True and body["activated"] is False
    assert body["top_up"]["account_number"] == "62057217400"


def test_status_when_server_has_no_token(owner):
    with patch.dict(os.environ, {"SIMCLOUD_API_TOKEN": ""}):
        assert owner.get("/airtime/status").json()["available"] is False


def test_activation_needs_terms_and_a_valid_cell(owner):
    base = {"first_name": "Thabo", "last_name": "M", "cell": "0821234567"}
    assert owner.post("/airtime/activate", json={**base, "accept_terms": False}).status_code == 400
    assert owner.post("/airtime/activate", json={**base, "cell": "123456789", "accept_terms": True}).status_code == 400


def test_activation_creates_the_shops_own_wallet(owner, db):
    with patch("app.clients.simcloud.register_child", return_value={"user_id": 77, "api_token": "child-token", "existing": False}) as reg:
        res = owner.post("/airtime/activate", json={"first_name": "Thabo", "last_name": "M", "cell": "+27 82 123 4567", "accept_terms": True})
    assert res.status_code == 200, res.text
    kwargs = reg.call_args.kwargs
    assert kwargs["cell"] == "0821234567" and kwargs["email"] == "owner@example.com" and kwargs["business_name"] == "Corner Cafe"
    row = db.rows("airtime_accounts")[0]
    assert row["cell"] == "0821234567" and row["terms_accepted_by"] == "u1" and row["terms_accepted_at"]
    # The token is stored encrypted, never as it came.
    assert row["api_token_encrypted"] != "child-token"
    assert password_crypto._decrypt_password(row["api_token_encrypted"]) == "child-token"
    # Twice is refused.
    assert owner.post("/airtime/activate", json={"first_name": "T", "last_name": "M", "cell": "0821234567", "accept_terms": True}).status_code == 409


def test_cashier_cannot_activate_or_change_the_fee(cashier, db):
    assert cashier.post("/airtime/activate", json={"first_name": "T", "last_name": "M", "cell": "0821234567", "accept_terms": True}).status_code == 403
    _activate(db)
    assert cashier.put("/airtime/settings", json={"service_fee": 2}).status_code == 403


def test_status_shows_the_shops_wallet_balance(owner, db):
    _activate(db, fee=1.5)
    with patch("app.clients.simcloud.balance", return_value={"balance": 42.5, "updated_at": "2026-11-11 12:00:00"}) as bal:
        body = owner.get("/airtime/status").json()
    assert bal.call_args.args[0] == "child-token"
    assert body["activated"] and body["balance"] == 42.5 and body["service_fee"] == 1.5 and body["cell"] == "0821234567"


def test_service_fee_setting(owner, db):
    _activate(db)
    assert owner.put("/airtime/settings", json={"service_fee": 2}).json()["service_fee"] == 2
    assert db.rows("airtime_accounts")[0]["service_fee"] == 2
    assert owner.put("/airtime/settings", json={"service_fee": 500}).status_code == 422
    assert owner.put("/airtime/settings", json={"service_fee": -1}).status_code == 422


def test_selling_airtime_uses_the_shops_token_and_records_the_fee(cashier, db):
    _activate(db, fee=2)
    with patch("app.clients.simcloud.order_airtime", return_value={"request_id": 9001, "queue_status": "queued"}) as order:
        res = cashier.post("/airtime/sell", json={"kind": "airtime", "msisdn": "083 555 0000", "network": "MTN", "amount": 10})
    assert res.status_code == 200, res.text
    assert order.call_args.args[0] == "child-token"
    assert order.call_args.kwargs["msisdn"] == "0835550000" and order.call_args.kwargs["amount"] == 10 and order.call_args.kwargs["network"] == "mtn"
    sale = db.rows("airtime_sales")[0]
    assert sale["amount"] == 10.0 and sale["service_fee"] == 2.0 and sale["status"] == "queued" and sale["sold_by"] == "u1"
    assert sale["reference"].startswith("KP-") and sale["request_id"] == "9001"


def test_sell_validation_never_reaches_simcloud(cashier, db):
    _activate(db)
    with patch("app.clients.simcloud.order_airtime") as order:
        assert cashier.post("/airtime/sell", json={"msisdn": "0835550000", "network": "mtn", "amount": 1}).status_code == 400
        assert cashier.post("/airtime/sell", json={"msisdn": "0835550000", "network": "mtn", "amount": 10.5}).status_code == 400
        assert cashier.post("/airtime/sell", json={"msisdn": "0835550000", "network": "o2", "amount": 10}).status_code == 400
        assert cashier.post("/airtime/sell", json={"msisdn": "555", "network": "mtn", "amount": 10}).status_code == 400
    order.assert_not_called()


def test_sell_before_activation_is_refused(cashier):
    assert cashier.post("/airtime/sell", json={"msisdn": "0835550000", "network": "mtn", "amount": 10}).status_code == 409


def test_empty_wallet_and_duplicate_are_explained(cashier, db):
    _activate(db)
    low = SimcloudError("Insufficient balance", status=400, body={"required_balance": 10, "available_balance": 0})
    with patch("app.clients.simcloud.order_airtime", side_effect=low):
        res = cashier.post("/airtime/sell", json={"msisdn": "0835550000", "network": "mtn", "amount": 10})
    assert res.status_code == 402 and "Top up" in res.json()["detail"]
    with patch("app.clients.simcloud.order_airtime", side_effect=SimcloudError("dup", status=409)):
        res = cashier.post("/airtime/sell", json={"msisdn": "0835550000", "network": "mtn", "amount": 10})
    assert res.status_code == 409 and "5 minutes" in res.json()["detail"]
    assert db.rows("airtime_sales") == []


def test_sales_list_refreshes_pending_ones_and_totals_fees(owner, db):
    _activate(db, fee=2)
    db.add("airtime_sales", id=1, store_id="s1", kind="airtime", network="mtn", msisdn="0835550000", amount=10.0, service_fee=2.0,
           reference="KP-a", request_id="9001", status="queued", created_at="2026-11-11T10:00:00+00:00")
    db.add("airtime_sales", id=2, store_id="s1", kind="airtime", network="mtn", msisdn="0835550001", amount=5.0, service_fee=2.0,
           reference="KP-b", request_id="9002", status="failed", created_at="2026-11-11T09:00:00+00:00")
    db.add("airtime_sales", id=3, store_id="other", kind="airtime", network="mtn", msisdn="0", amount=5.0, service_fee=9.0,
           reference="KP-c", request_id="1", status="delivered", created_at="2026-11-11T09:30:00+00:00")
    with patch("app.clients.simcloud.airtime_status", return_value={"status": "delivered"}) as st:
        body = owner.get("/airtime/sales").json()
    assert [s["id"] for s in body["items"]] == [1, 2]
    assert body["items"][0]["status"] == "delivered"
    assert st.call_count == 1  # the failed one is final; another shop's is never seen
    assert body["fees_total"] == 2.0
    assert db.rows("airtime_sales")[0]["status"] == "delivered"


def test_products_lists_data_bundles(owner, db):
    _activate(db)
    raw = [{"network": "pd-mtn", "sellvalue": 80, "description": "1GB-R80", "groupname": "Data Bundle"}]
    with patch("app.clients.simcloud.data_products", return_value=raw):
        items = owner.get("/airtime/products", params={"network": "mtn"}).json()["items"]
    assert items == [{"network": "mtn", "amount": 80.0, "description": "1GB-R80", "group": "Data Bundle"}]


def test_network_lookup_picks_the_network_and_never_blocks(cashier, db):
    _activate(db)
    with patch("app.clients.simcloud._request", return_value={"status": "success", "network": "Cell C"}):
        assert cashier.get("/airtime/network", params={"msisdn": "084 555 0000"}).json() == {"msisdn": "0845550000", "network": "cellc"}
    with patch("app.clients.simcloud.lookup_network", side_effect=SimcloudError("down", status=502)):
        assert cashier.get("/airtime/network", params={"msisdn": "0845550000"}).json()["network"] is None
    assert cashier.get("/airtime/network", params={"msisdn": "12"}).status_code == 400


def test_switch_off_forgets_the_wallet_but_keeps_sales(db):
    from app.main import app

    _activate(db)
    db.add("airtime_sales", id=1, store_id="s1", kind="airtime", network="mtn", msisdn="0835550000", amount=10.0, service_fee=0,
           reference="KP-a", request_id="1", status="delivered", created_at="2026-11-11T10:00:00+00:00")
    # One login at a time: each _client() replaces who the app thinks is calling.
    assert _client("cashier").delete("/airtime/account").status_code == 403
    owner = _client("admin")
    try:
        _switch_off_as_owner(owner, db)
    finally:
        app.dependency_overrides.clear()


def _switch_off_as_owner(owner, db):
    with patch("app.clients.simcloud.balance", return_value={"balance": 12.0}):
        res = owner.delete("/airtime/account")
    assert res.status_code == 200
    assert res.json() == {"switched_off": True, "cell": "0821234567", "balance_left": 12.0}
    assert db.rows("airtime_accounts") == [] and len(db.rows("airtime_sales")) == 1
    assert owner.get("/airtime/status").json()["activated"] is False
    assert owner.delete("/airtime/account").status_code == 409


CATALOG = [
    {"product_id": 14, "voucher_type": "Shoprite", "slug": "shoprite", "available_amounts": [100, 110, 200], "logo_url": "https://simcloud.co.za/l/shoprite.png"},
    {"product_id": 3, "voucher_type": "Golfers Club", "slug": "golfers-club", "available_amounts": [500]},
    {"product_id": 10, "voucher_type": "PEP", "slug": "pep", "available_amounts": [250], "logo_url": "javascript:alert(1)"},
]


def test_electricity_fee_matches_simclouds_example():
    assert simcloud.electricity_fee(150) == 4   # their example: R150 -> R4 fee, R146 electricity
    assert simcloud.electricity_fee(55) == 1
    assert simcloud.electricity_fee(1000) == 10  # capped


def test_meter_check(cashier, db):
    _activate(db)
    with patch("app.clients.simcloud.electricity_meter", return_value={"status": "success", "supported": True, "provider": "City Power"}):
        body = cashier.get("/airtime/electricity/meter", params={"meter_number": "0123 4567 890"}).json()
    assert body == {"meter_number": "01234567890", "status": "success", "supported": True, "provider": "City Power"}
    with patch("app.clients.simcloud.electricity_meter", side_effect=SimcloudError("down", status=503)):
        assert cashier.get("/airtime/electricity/meter", params={"meter_number": "01234567890"}).json()["status"] == "unknown"
    assert cashier.get("/airtime/electricity/meter", params={"meter_number": "12"}).status_code == 400


def test_selling_electricity_records_the_token_and_both_fees(cashier, db):
    _activate(db)
    db.rows("airtime_accounts")[0]["electricity_fee"] = 3
    answer = {"status": "success", "order_id": 501, "recharge_pin_information": [{"entry_order": 1, "pin": "1234 5678 9012 3456 7890"}]}
    with patch("app.clients.simcloud.order_electricity", return_value=answer) as order:
        res = cashier.post("/airtime/sell", json={"kind": "electricity", "meter_number": "01234567890", "amount": 150, "msisdn": "083 555 0000"})
    assert res.status_code == 200, res.text
    kw = order.call_args.kwargs
    assert kw["meter_number"] == "01234567890" and kw["amount"] == 150 and kw["recipient"] == "0835550000"
    sale = db.rows("airtime_sales")[0]
    assert kw["client_reference"] == sale["reference"]
    assert sale["kind"] == "electricity" and sale["status"] == "delivered"
    assert sale["service_fee"] == 3.0 and sale["provider_fee"] == 4.0
    assert sale["details"]["tokens"] == ["12345678901234567890"] and sale["details"]["meter_number"] == "01234567890"


def test_electricity_validation(cashier, db):
    _activate(db)
    with patch("app.clients.simcloud.order_electricity") as order:
        base = {"kind": "electricity", "meter_number": "01234567890", "msisdn": "0835550000"}
        assert cashier.post("/airtime/sell", json={**base, "amount": 50}).status_code == 400
        assert cashier.post("/airtime/sell", json={**base, "amount": 99.5}).status_code == 400
        assert cashier.post("/airtime/sell", json={**base, "amount": 100, "msisdn": ""}).status_code == 400
        assert cashier.post("/airtime/sell", json={**base, "amount": 100, "meter_number": "1"}).status_code == 400
    order.assert_not_called()


def test_pending_electricity_gets_its_token_on_refresh_without_buying_again(owner, db):
    _activate(db)
    db.add("airtime_sales", id=7, store_id="s1", kind="electricity", network="electricity", msisdn="0835550000", amount=100.0, service_fee=0,
           reference="KP-e1", request_id="", status="pending", details={"meter_number": "01234567890", "tokens": []}, created_at="2026-11-11T10:00:00+00:00")
    done = {"status": "success", "recharge_pin_information": {"pins": ["11112222333344445555"]}}
    with patch("app.clients.simcloud.electricity_status", return_value=done) as st, patch("app.clients.simcloud.order_electricity") as order:
        sale = owner.get("/airtime/sales/7").json()
    assert st.call_args.args[1] == "KP-e1"
    order.assert_not_called()
    assert sale["status"] == "delivered" and sale["details"]["tokens"] == ["11112222333344445555"]


def test_voucher_list_is_the_shop_friendly_subset(cashier, db):
    _activate(db)
    with patch("app.clients.simcloud.voucher_catalog", return_value=CATALOG):
        items = cashier.get("/airtime/vouchers").json()["items"]
    assert [i["name"] for i in items] == ["Shoprite", "PEP"]
    assert items[0]["amounts"] == [100.0, 110.0, 200.0] and items[0]["logo_url"].startswith("https://")
    assert items[1]["logo_url"] is None  # only https logos are passed on


def test_selling_a_voucher_and_fetching_its_code(cashier, db):
    _activate(db, fee=2)
    with patch("app.clients.simcloud.voucher_catalog", return_value=CATALOG), patch(
        "app.clients.simcloud.order_voucher", return_value={"order_id": 88, "process_status": "Pending", "fee": 0}
    ) as order:
        res = cashier.post("/airtime/sell", json={"kind": "voucher", "product_id": 14, "amount": 100})
        assert res.status_code == 200, res.text
        assert order.call_args.kwargs["product_id"] == 14 and order.call_args.kwargs["amount"] == 100
        # An amount the brand doesn't sell, and a brand we don't list, never reach SIMcloud.
        assert cashier.post("/airtime/sell", json={"kind": "voucher", "product_id": 14, "amount": 105}).status_code == 400
        assert cashier.post("/airtime/sell", json={"kind": "voucher", "product_id": 3, "amount": 500}).status_code == 400
        assert order.call_count == 1
    sale = db.rows("airtime_sales")[0]
    assert sale["kind"] == "voucher" and sale["network"] == "shoprite" and sale["status"] == "pending" and sale["service_fee"] == 2.0
    sale["id"] = 1
    ready = {"process_status": "Success", "vouchers": [{"voucher_type": "Shoprite", "voucher_code": "ABCD-1234", "value": 100, "expiry_date": "2029-11-11"}]}
    with patch("app.clients.simcloud.voucher_status", return_value=ready):
        got = cashier.get("/airtime/sales/1").json()
    assert got["status"] == "delivered" and got["details"]["vouchers"][0]["voucher_code"] == "ABCD-1234"


def test_electricity_fee_setting(owner, db):
    _activate(db)
    assert owner.put("/airtime/settings", json={"electricity_fee": 3}).json() == {"electricity_fee": 3}
    assert db.rows("airtime_accounts")[0]["electricity_fee"] == 3
    assert owner.put("/airtime/settings", json={}).status_code == 400


def test_missing_tables_say_so(owner, db):
    del db.tables["airtime_accounts"]
    res = owner.post("/airtime/sell", json={"msisdn": "0835550000", "network": "mtn", "amount": 10})
    assert res.status_code == 503 and "20261111120000_airtime.sql" in res.json()["detail"]
