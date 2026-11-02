"""A new account's setup must survive its own first page load.

The app fires several requests the moment a fresh account signs in, and every
one of them runs the store/profile/subscription setup. The shared Supabase
client answers some of that burst with transient socket errors, and before
this the setup was neither retried nor resumable: a request cut short after
the profile step left a store with no subscription row at all. That read as
"expired", and the first click on Subscribe then planted a free/active row
(the table's column defaults) in place of the 30-day trial.

These tests use a small in-memory stand-in for Supabase so the sequences can
be stated honestly — interrupted runs, lost races, injected failures —
instead of counting mock calls.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import deps
from app.services import subscriptions


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self.op = "select"
        self.payload = None
        self.kwargs = {}
        self.filters = []
        self._single = False
        self._limit = None

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

    def _matches(self, rows):
        return [r for r in rows if all(r.get(k) == v for k, v in self.filters)]

    def execute(self):
        self.db.maybe_fail(self.table_name, self.op)
        rows = self.db.tables.setdefault(self.table_name, [])
        key = self.db.keys[self.table_name]
        if self.op == "insert":
            row = dict(self.payload)
            row.setdefault("id", f"{self.table_name}-{len(rows) + 1}")
            if any(r[key] == row[key] for r in rows):
                raise Exception('{"code": "23505", "message": "duplicate key value violates unique constraint"}')
            rows.append(row)
            return _Result([row])
        if self.op == "upsert":
            row = dict(self.payload)
            existing = next((r for r in rows if r[key] == row[key]), None)
            if existing is not None:
                if self.kwargs.get("ignore_duplicates"):
                    return _Result([])
                existing.update(row)
                return _Result([existing])
            rows.append(row)
            return _Result([row])
        if self.op == "update":
            hit = self._matches(rows)
            for r in hit:
                r.update(self.payload)
            return _Result(hit)
        hit = self._matches(rows)
        if self._limit:
            hit = hit[: self._limit]
        if self._single:
            if len(hit) != 1:
                err = Exception("JSON object requested, multiple (or no) rows returned")
                err.code = "PGRST116"
                raise err
            return _Result(hit[0])
        return _Result(hit)


class FakeSupabase:
    keys = {"stores": "id", "profiles": "id", "subscriptions": "store_id", "products": "id"}

    def __init__(self):
        self.tables = {"stores": [], "profiles": [], "subscriptions": [], "products": []}
        self._failures = []
        self.calls = []

    def fail(self, table, op, exc, times=1):
        """Make the next `times` (table, op) executions raise `exc`.
        `exc` may be a callable producing the exception, which lets a test
        plant rows "written by another request" before the error surfaces."""
        self._failures.append([table, op, exc, times])

    def maybe_fail(self, table, op):
        self.calls.append((table, op))
        for f in self._failures:
            if f[0] == table and f[1] == op and f[3] > 0:
                f[3] -= 1
                exc = f[2]
                raise exc() if callable(exc) else exc

    def table(self, name):
        return _Query(self, name)


def _transient():
    # str() of this is "[Errno 11] Resource temporarily unavailable" — what
    # the shared client raised in production under a first-load burst.
    return OSError(11, "Resource temporarily unavailable")


def _is_fresh_trial(sub):
    assert (sub["plan"], sub["status"]) == ("pro", "trialing")
    remaining = datetime.fromisoformat(sub["trial_end"]) - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(deps, "RETRY_BASE_DELAY", 0)


class TestFirstRequestSetup:
    def test_fresh_account_gets_store_profile_and_trial(self):
        db = FakeSupabase()
        row = deps._create_store_and_profile(db, "u1", {"store_name": "Shop", "email": "o@x.com"})
        store = db.tables["stores"][0]
        assert store["name"] == "Shop" and store["owner_id"] == "u1"
        assert row["role"] == "admin" and row["store_id"] == store["id"]
        assert len(db.tables["subscriptions"]) == 1
        _is_fresh_trial(db.tables["subscriptions"][0])

    def test_transient_error_on_the_subscription_step_is_retried(self):
        db = FakeSupabase()
        db.fail("subscriptions", "upsert", _transient, times=2)
        deps._create_store_and_profile(db, "u1", {})
        assert len(db.tables["subscriptions"]) == 1
        _is_fresh_trial(db.tables["subscriptions"][0])

    def test_interrupted_signup_is_completed_by_the_next_request(self):
        # Store and profile exist (an earlier request got that far); the
        # subscription never happened. This is the production state behind the
        # free-plan bug.
        db = FakeSupabase()
        db.tables["stores"].append({"id": "s1", "owner_id": "u1", "name": "Shop"})
        db.tables["profiles"].append({"id": "u1", "role": "admin", "store_id": "s1", "name": "o"})
        row = deps._create_store_and_profile(db, "u1", {})
        assert row["store_id"] == "s1"
        assert len(db.tables["stores"]) == 1
        assert len(db.tables["profiles"]) == 1
        _is_fresh_trial(db.tables["subscriptions"][0])

    def test_existing_subscription_is_never_reset(self):
        db = FakeSupabase()
        db.tables["stores"].append({"id": "s1", "owner_id": "u1", "name": "Shop"})
        paid = {"store_id": "s1", "plan": "pro", "status": "active", "paystack_subscription_code": "SUB_1"}
        db.tables["subscriptions"].append(dict(paid))
        deps._create_store_and_profile(db, "u1", {})
        assert db.tables["subscriptions"] == [paid]

    def test_losing_the_store_insert_race_reuses_the_winners_store(self):
        db = FakeSupabase()

        def other_request_won():
            db.tables["stores"].append({"id": "s-winner", "owner_id": "u1", "name": "Shop"})
            return Exception('{"code": "23505", "message": "duplicate key"}')

        db.fail("stores", "insert", other_request_won)
        row = deps._create_store_and_profile(db, "u1", {})
        assert row["store_id"] == "s-winner"
        assert [s["id"] for s in db.tables["stores"]] == ["s-winner"]
        assert db.tables["subscriptions"][0]["store_id"] == "s-winner"

    def test_profile_written_by_a_concurrent_request_is_reused(self):
        db = FakeSupabase()
        db.tables["profiles"].append({"id": "u1", "role": "admin", "store_id": "s-other", "name": "o"})
        db.tables["stores"].append({"id": "s-other", "owner_id": "u1", "name": "Shop"})
        row = deps._create_store_and_profile(db, "u1", {})
        assert row["store_id"] == "s-other"
        assert len(db.tables["profiles"]) == 1

    def test_persistent_subscription_failure_does_not_fail_the_request(self):
        # The profile exists, so the plan lookup heals the missing row later;
        # a 500 here would only turn one bad step into a broken first load.
        db = FakeSupabase()
        db.fail("subscriptions", "upsert", _transient, times=99)
        row = deps._create_store_and_profile(db, "u1", {})
        assert row["role"] == "admin"
        assert db.tables["subscriptions"] == []

    def test_real_error_on_store_insert_propagates(self):
        db = FakeSupabase()
        db.fail("stores", "insert", RuntimeError("permission denied"))
        with pytest.raises(RuntimeError):
            deps._create_store_and_profile(db, "u1", {})

    def test_profile_that_cannot_be_written_or_read_is_an_error(self):
        db = FakeSupabase()
        db.fail("profiles", "upsert", RuntimeError("permission denied"), times=99)
        with pytest.raises(HTTPException) as exc:
            deps._create_store_and_profile(db, "u1", {})
        assert exc.value.status_code == 500
        assert "Failed to create profile" in exc.value.detail


class TestPlanLookupHealsMissingRow:
    def test_missing_row_gets_the_trial_instead_of_reading_as_expired(self):
        db = FakeSupabase()
        with patch.object(subscriptions, "get_supabase_client", return_value=db):
            limits = subscriptions.get_store_plan("s1")
        assert limits.plan == "pro"
        assert limits.is_on_trial is True
        assert limits.max_products is None
        _is_fresh_trial(db.tables["subscriptions"][0])

    def test_plan_info_reports_the_healed_trial(self):
        db = FakeSupabase()
        with patch.object(subscriptions, "get_supabase_client", return_value=db):
            info = subscriptions.get_plan_info("s1")
        assert info["is_on_trial"] is True
        assert info["trial_end"] == db.tables["subscriptions"][0]["trial_end"]

    def test_row_that_cannot_be_created_still_reads_as_expired(self):
        db = FakeSupabase()
        db.fail("subscriptions", "upsert", RuntimeError("down"), times=99)
        with patch.object(subscriptions, "get_supabase_client", return_value=db):
            limits = subscriptions.get_store_plan("s1")
        assert limits.plan == "expired"
        assert limits.is_active is False
        assert limits.degraded is False


class TestCheckoutNeverPlantsARow:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from app.main import app

        app.dependency_overrides[deps.get_current_context] = lambda: deps.RequestContext(
            user_id="u1", store_id="s1", role="admin"
        )
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()

    @patch("app.db.supabase.get_supabase_client")
    @patch("app.clients.paystack.initialize_transaction")
    def test_checkout_without_a_subscription_row_creates_none(self, mock_init_tx, mock_supa, client):
        db = FakeSupabase()
        mock_supa.return_value = db
        mock_init_tx.return_value = {"url": "https://checkout.paystack.com/abc", "reference": "ref_1"}
        resp = client.post("/billing/checkout", json={"plan": "pro", "email": "a@b.com"})
        assert resp.status_code == 200
        assert db.tables["subscriptions"] == []

    @patch("app.db.supabase.get_supabase_client")
    @patch("app.clients.paystack.initialize_transaction")
    def test_checkout_marks_the_provider_on_an_existing_row(self, mock_init_tx, mock_supa, client):
        db = FakeSupabase()
        db.tables["subscriptions"].append({"store_id": "s1", "plan": "pro", "status": "trialing", "billing_provider": None})
        mock_supa.return_value = db
        mock_init_tx.return_value = {"url": "https://checkout.paystack.com/abc", "reference": "ref_1"}
        resp = client.post("/billing/checkout", json={"plan": "pro", "email": "a@b.com"})
        assert resp.status_code == 200
        sub = db.tables["subscriptions"][0]
        assert sub["billing_provider"] == "paystack"
        assert (sub["plan"], sub["status"]) == ("pro", "trialing")
