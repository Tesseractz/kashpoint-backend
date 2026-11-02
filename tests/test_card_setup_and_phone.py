"""Phone numbers at signup, and saving a card during the free trial.

The card flow moves real money, so every path is stated against an in-memory
database: a store on its trial saves a card, Paystack is asked for a
subscription whose first debit is the trial's last day, the R1 card check is
refunded, and nothing — a racing webhook, a non-reusable card, a failed write —
can produce two subscriptions or a charge the app doesn't know about.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import RequestContext, get_current_context
from app.api.routers import billing
from app.core.phone import normalize_phone
from app.main import app
from app.services.subscriptions import PlanLimits


# ---------------------------------------------------------------- in-memory Supabase
class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.op, self.payload, self.kwargs, self.filters = "select", None, {}, []
        self._single, self._limit = False, None

    def select(self, *_, **__):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload, **kwargs):
        self.op, self.payload, self.kwargs = "upsert", payload, kwargs
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, *_, **__):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def _hit(self, rows):
        return [r for r in rows if all(r.get(k) == v for k, v in self.filters)]

    def execute(self):
        self.db.maybe_fail(self.table_name, self.op, self.payload)
        rows = self.db.tables.setdefault(self.table_name, [])
        key = self.db.keys.get(self.table_name, "id")
        if self.op == "insert":
            row = dict(self.payload)
            row.setdefault("id", f"{self.table_name}-{len(rows) + 1}")
            if any(r.get(key) == row.get(key) for r in rows):
                raise Exception('{"code": "23505", "message": "duplicate key value violates unique constraint"}')
            rows.append(row)
            return _Result([row])
        if self.op == "upsert":
            row = dict(self.payload)
            existing = next((r for r in rows if r.get(key) == row.get(key)), None)
            if existing is not None:
                if self.kwargs.get("ignore_duplicates"):
                    return _Result([])
                existing.update(row)
                return _Result([existing])
            rows.append(row)
            return _Result([row])
        if self.op == "update":
            hit = self._hit(rows)
            for r in hit:
                r.update(self.payload)
            return _Result(hit)
        if self.op == "delete":
            hit = self._hit(rows)
            self.db.tables[self.table_name] = [r for r in rows if r not in hit]
            return _Result(hit)
        hit = self._hit(rows)[: self._limit] if self._limit else self._hit(rows)
        if self._single:
            if len(hit) != 1:
                err = Exception("JSON object requested, multiple (or no) rows returned")
                err.code = "PGRST116"
                raise err
            return _Result(hit[0])
        return _Result(hit)


class FakeSupabase:
    keys = {"stores": "id", "profiles": "id", "subscriptions": "store_id", "webhook_events": "id"}

    def __init__(self):
        self.tables = {"stores": [], "profiles": [], "subscriptions": [], "webhook_events": [], "products": []}
        self._failures = []

    def fail(self, table, op, exc, times=1, when=None):
        self._failures.append([table, op, exc, times, when])

    def maybe_fail(self, table, op, payload):
        for f in self._failures:
            if f[0] == table and f[1] == op and f[3] > 0 and (f[4] is None or f[4](payload)):
                f[3] -= 1
                raise f[2]

    def table(self, name):
        return _Query(self, name)

    def sub(self, store_id="s1"):
        return next(r for r in self.tables["subscriptions"] if r["store_id"] == store_id)


def _iso(delta):
    return (datetime.now(timezone.utc) + delta).isoformat()


def _trial_db(days_left=12, **extra):
    db = FakeSupabase()
    row = {"store_id": "s1", "plan": "pro", "status": "trialing", "trial_end": _iso(timedelta(days=days_left))}
    row.update(extra)
    db.tables["subscriptions"].append(row)
    return db


def _card_check(reusable=True, reference="ref-check"):
    return {
        "status": "success",
        "reference": reference,
        "metadata": {"store_id": "s1", "purpose": "card_setup"},
        "authorization": {"authorization_code": "AUTH_abc", "reusable": reusable, "channel": "card"},
        "customer": {"customer_code": "CUS_1", "email": "owner@example.com"},
    }


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    import app.db.supabase as sb

    monkeypatch.setattr(sb, "RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(billing.time, "sleep", lambda *_: None)


@pytest.fixture
def paystack():
    """Every Paystack call the card flow makes, mocked and recorded."""
    with patch("app.clients.paystack.create_subscription") as create, patch(
        "app.clients.paystack.refund_transaction"
    ) as refund, patch("app.clients.paystack.disable_subscription") as disable, patch(
        "app.clients.paystack.initialize_card_check"
    ) as init:
        create.side_effect = lambda **kw: {
            "subscription_code": "SUB_new",
            "email_token": "tok_new",
            "next_payment_date": kw["start_date"],
        }
        refund.return_value = True
        init.return_value = {"url": "https://checkout.paystack.com/card", "reference": "ref-check"}
        yield {"create": create, "refund": refund, "disable": disable, "init": init}


# ---------------------------------------------------------------- phone numbers
class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw",
        ["082 123 4567", "0821234567", "+27 82 123 4567", "27821234567", "(082) 123-4567", "0027821234567"],
    )
    def test_every_way_of_writing_a_sa_number_means_the_same(self, raw):
        assert normalize_phone(raw) == "+27821234567"

    def test_foreign_numbers_keep_their_country_code(self):
        assert normalize_phone("+44 7700 900123") == "+447700900123"

    @pytest.mark.parametrize("raw", [None, "", "12345", "082 123 456", "+27 82 123 45678", "hello"])
    def test_rejects_what_is_not_a_phone_number(self, raw):
        assert normalize_phone(raw) is None


class TestSignupStoresThePhone:
    def test_new_store_gets_the_normalised_number_and_the_marketing_choice(self):
        db = FakeSupabase()
        with patch("app.api.deps.ensure_store_subscription"):
            deps._create_store_and_profile(
                db, "u1", {"store_name": "Spaza", "email": "a@b.c", "phone": "082 123 4567", "marketing_opt_in": True}
            )
        store = db.tables["stores"][0]
        assert store["phone"] == "+27821234567"
        assert db.tables["profiles"][0]["marketing_emails_enabled"] is True

    def test_signup_still_works_before_the_phone_column_exists(self):
        db = FakeSupabase()
        db.fail(
            "stores",
            "insert",
            Exception("{'code': 'PGRST204', 'message': \"Could not find the 'phone' column of 'stores' in the schema cache\"}"),
            when=lambda payload: "phone" in payload,
        )
        with patch("app.api.deps.ensure_store_subscription"):
            row = deps._create_store_and_profile(db, "u1", {"store_name": "Spaza", "phone": "0821234567"})
        assert row["store_id"] == db.tables["stores"][0]["id"]
        assert "phone" not in db.tables["stores"][0]

    def test_an_unusable_number_is_simply_not_stored(self):
        db = FakeSupabase()
        with patch("app.api.deps.ensure_store_subscription"):
            deps._create_store_and_profile(db, "u1", {"store_name": "Spaza", "phone": "123"})
        assert "phone" not in db.tables["stores"][0]


# ---------------------------------------------------------------- saving the card
class TestCompleteCardSetup:
    def test_saves_the_card_with_the_first_charge_on_the_trial_end(self, paystack):
        db = _trial_db(days_left=12)
        trial_end = db.sub()["trial_end"]

        result = billing._complete_card_setup(db, "s1", _card_check(), wait=True)

        kw = paystack["create"].call_args.kwargs
        assert kw["customer"] == "CUS_1" and kw["authorization"] == "AUTH_abc"
        assert kw["start_date"] == trial_end
        sub = db.sub()
        assert (sub["status"], sub["plan"]) == ("trialing", "pro"), "the trial keeps running"
        assert sub["paystack_customer_code"] == "CUS_1"
        assert sub["paystack_subscription_code"] == "SUB_new"
        assert sub["current_period_end"] == trial_end
        paystack["refund"].assert_called_once_with(reference="ref-check")
        assert result["card_saved"] and result["first_charge_date"] == trial_end

    def test_a_card_that_cannot_be_reused_is_refunded_and_never_subscribed(self, paystack):
        db = _trial_db()
        with pytest.raises(HTTPException) as e:
            billing._complete_card_setup(db, "s1", _card_check(reusable=False), wait=True)
        assert e.value.status_code == 400
        paystack["create"].assert_not_called()
        paystack["refund"].assert_called_once_with(reference="ref-check")
        assert db.tables["webhook_events"] == [], "the claim is released so the owner can retry"

    def test_redirect_and_webhook_together_create_one_subscription(self, paystack):
        db = _trial_db()
        billing._complete_card_setup(db, "s1", _card_check(), wait=False)  # the webhook
        billing._complete_card_setup(db, "s1", _card_check(), wait=True)  # the browser's return
        assert paystack["create"].call_count == 1

    def test_the_loser_of_the_race_does_nothing_while_the_winner_works(self, paystack):
        db = _trial_db()
        db.tables["webhook_events"].append({"id": "card_setup:ref-check"})  # the other request's claim
        result = billing._complete_card_setup(db, "s1", _card_check(), wait=False)
        assert result == {"card_saved": False, "pending": True}
        paystack["create"].assert_not_called()

    def test_a_subscription_the_app_cannot_record_is_cancelled_at_paystack(self, paystack):
        db = _trial_db()
        db.fail(
            "subscriptions",
            "update",
            Exception("database is down"),
            times=10,
            when=lambda payload: "paystack_subscription_code" in payload,
        )
        with pytest.raises(HTTPException) as e:
            billing._complete_card_setup(db, "s1", _card_check(), wait=True)
        assert e.value.status_code == 502
        paystack["disable"].assert_called_once_with(subscription_code="SUB_new", email_token="tok_new")
        paystack["refund"].assert_called_with(reference="ref-check")
        assert db.tables["webhook_events"] == []

    def test_after_the_trial_was_cancelled_the_check_is_refunded(self, paystack):
        db = _trial_db(status="canceled", plan="expired")
        with pytest.raises(HTTPException) as e:
            billing._complete_card_setup(db, "s1", _card_check(), wait=True)
        assert e.value.status_code == 400
        paystack["create"].assert_not_called()
        paystack["refund"].assert_called_once()

    def test_a_trial_on_its_last_minutes_is_billed_soon_not_in_the_past(self, paystack):
        db = _trial_db(days_left=0)
        db.sub()["trial_end"] = _iso(timedelta(minutes=5))
        billing._complete_card_setup(db, "s1", _card_check(), wait=True)
        start = datetime.fromisoformat(paystack["create"].call_args.kwargs["start_date"])
        assert start > datetime.now(timezone.utc) + timedelta(minutes=25)


# ---------------------------------------------------------------- endpoints
@pytest.fixture
def client():
    app.dependency_overrides[get_current_context] = lambda: RequestContext("u1", "s1", "admin")
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCardSetupEndpoint:
    def test_starts_a_card_check_marked_as_card_setup(self, client, paystack):
        db = _trial_db()
        with patch("app.db.supabase.get_supabase_client", return_value=db):
            res = client.post("/billing/card-setup", json={"email": "owner@example.com"})
        assert res.status_code == 200, res.text
        assert res.json()["reference"] == "ref-check"
        kw = paystack["init"].call_args.kwargs
        assert kw["metadata"] == {"store_id": "s1", "purpose": "card_setup"}
        assert kw["amount_cents"] == 100
        assert kw["callback_url"].endswith("/billing?success=1&card=1")

    def test_only_during_the_trial(self, client, paystack):
        db = _trial_db(days_left=-1)
        with patch("app.db.supabase.get_supabase_client", return_value=db):
            res = client.post("/billing/card-setup", json={"email": "owner@example.com"})
        assert res.status_code == 400
        paystack["init"].assert_not_called()

    def test_not_twice(self, client, paystack):
        db = _trial_db(paystack_subscription_code="SUB_old")
        with patch("app.db.supabase.get_supabase_client", return_value=db):
            res = client.post("/billing/card-setup", json={"email": "owner@example.com"})
        assert res.status_code == 409

    def test_cashiers_cannot(self, paystack):
        app.dependency_overrides[get_current_context] = lambda: RequestContext("u2", "s1", "cashier")
        try:
            res = TestClient(app).post("/billing/card-setup", json={"email": "c@example.com"})
        finally:
            app.dependency_overrides.clear()
        assert res.status_code == 403

    def test_sync_after_the_card_check_saves_the_card_instead_of_activating(self, client, paystack):
        db = _trial_db()
        with patch("app.db.supabase.get_supabase_client", return_value=db), patch(
            "app.clients.paystack.verify_transaction", return_value=_card_check()
        ):
            res = client.post("/billing/paystack/sync", json={"reference": "ref-check"})
        assert res.status_code == 200, res.text
        assert res.json()["card_saved"] is True
        assert db.sub()["status"] == "trialing"
        assert db.sub()["paystack_subscription_code"] == "SUB_new"


class TestRemovingTheCard:
    def test_removing_the_card_stops_the_charge_and_keeps_the_trial(self, client, paystack):
        db = _trial_db(paystack_subscription_code="SUB_new", paystack_email_token="tok_new", current_period_end=_iso(timedelta(days=12)))
        with patch("app.db.supabase.get_supabase_client", return_value=db):
            res = client.post("/billing/cancel")
        assert res.status_code == 200, res.text
        assert res.json()["card_removed"] is True
        paystack["disable"].assert_called_once_with(subscription_code="SUB_new", email_token="tok_new")
        sub = db.sub()
        assert sub["status"] == "trialing" and sub["plan"] == "pro"
        assert sub["paystack_subscription_code"] is None


class TestWebhooksDuringTheTrial:
    @pytest.fixture
    def post(self):
        def _post(event, data, db):
            with patch("app.db.supabase.get_supabase_client", return_value=db), patch(
                "app.clients.paystack.verify_paystack_signature_with_reason", return_value=(True, "ok")
            ), patch("app.clients.paystack.fetch_subscription_by_code", return_value={}):
                return TestClient(app).post(
                    "/paystack/webhook", json={"event": event, "data": data}, headers={"x-paystack-signature": "x" * 128}
                )

        return _post

    def test_the_card_check_charge_never_activates_a_paid_plan(self, post, paystack):
        db = _trial_db()
        data = dict(_card_check(), id=111)
        res = post("charge.success", data, db)
        assert res.status_code == 200
        sub = db.sub()
        assert sub["status"] == "trialing", "R1 card check is not a Pro payment"
        assert sub["paystack_subscription_code"] == "SUB_new"

    def test_subscription_create_keeps_the_store_on_its_trial(self, post, paystack):
        db = _trial_db(paystack_customer_code="CUS_1")
        data = {"id": 222, "subscription_code": "SUB_new", "email_token": "tok_new",
                "next_payment_date": db.sub()["trial_end"], "customer": {"customer_code": "CUS_1"}}
        post("subscription.create", data, db)
        assert db.sub()["status"] == "trialing"

    def test_card_removed_event_does_not_end_the_trial(self, post, paystack):
        db = _trial_db(paystack_customer_code="CUS_1", paystack_subscription_code="SUB_new")
        post("subscription.disable", {"id": 333, "customer": {"customer_code": "CUS_1"}}, db)
        sub = db.sub()
        assert (sub["status"], sub["plan"]) == ("trialing", "pro")
        assert sub["paystack_subscription_code"] is None

    def test_the_first_real_debit_at_the_end_of_the_trial_activates_pro(self, post, paystack):
        db = _trial_db(days_left=0, paystack_customer_code="CUS_1", paystack_subscription_code="SUB_new")
        db.sub()["trial_end"] = _iso(timedelta(minutes=-10))
        nxt = _iso(timedelta(days=30))
        with patch("app.clients.paystack.fetch_subscription_by_code", return_value={"next_payment_date": nxt}):
            post("charge.success", {"id": 444, "customer": {"customer_code": "CUS_1"}, "subscription": {"subscription_code": "SUB_new"}}, db)
        sub = db.sub()
        assert (sub["status"], sub["plan"]) == ("active", "pro")


# ---------------------------------------------------------------- access while the charge is on its way
class TestGraceWhileTheChargeIsDue:
    def test_trial_over_with_a_card_keeps_pro_while_paystack_charges(self):
        limits = PlanLimits("pro", status="trialing", trial_end=_iso(timedelta(hours=-5)), has_card=True)
        assert limits.is_active and limits.max_products is None

    def test_trial_over_without_a_card_locks(self):
        limits = PlanLimits("pro", status="trialing", trial_end=_iso(timedelta(hours=-5)))
        assert not limits.is_active and limits.max_products == 10

    def test_grace_does_not_last_forever(self):
        limits = PlanLimits("pro", status="trialing", trial_end=_iso(timedelta(days=-4)), has_card=True)
        assert not limits.is_active

    def test_a_renewal_due_this_morning_does_not_lock_a_paying_store(self):
        limits = PlanLimits("pro", status="active", current_period_end=_iso(timedelta(hours=-3)), has_card=True)
        assert limits.is_active

    def test_a_failed_renewal_still_locks_at_once(self):
        limits = PlanLimits("pro", status="past_due", current_period_end=_iso(timedelta(hours=-3)), has_card=True)
        assert not limits.is_active
