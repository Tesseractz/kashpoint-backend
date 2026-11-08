"""The admin portal: who gets in, what each role sees, and what the staff
actions do to a store's plan.

Runs against a small in-memory Supabase stand-in so the sequences can be
checked for real (a trial extended, a store suspended, a partner scoped)
rather than by counting mock calls. Supabase Auth is replaced by a fixed list
of accounts.
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.services import subscriptions
from app.services.subscriptions import PlanLimits

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def days(n):
    return iso(NOW + timedelta(days=n))


# ---------------------------------------------------------------------------
# A Supabase that keeps rows in lists.
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self.op = "select"
        self.payload = None
        self.kwargs = {}
        self.filters = []
        self._order = None
        self._limit = None
        self._single = False

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

    def eq(self, k, v):
        self.filters.append(("eq", k, v))
        return self

    def neq(self, k, v):
        self.filters.append(("neq", k, v))
        return self

    def in_(self, k, values):
        self.filters.append(("in", k, list(values)))
        return self

    def gte(self, k, v):
        self.filters.append(("gte", k, v))
        return self

    def lte(self, k, v):
        self.filters.append(("lte", k, v))
        return self

    def order(self, col, desc=False, **_):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def _match(self, row):
        for op, k, v in self.filters:
            have = row.get(k)
            if op == "eq" and have != v:
                return False
            if op == "neq" and have == v:
                return False
            if op == "in" and have not in v:
                return False
            if op == "gte" and not (have is not None and str(have) >= str(v)):
                return False
            if op == "lte" and not (have is not None and str(have) <= str(v)):
                return False
        return True

    def execute(self):
        if self.table_name not in self.db.tables:
            raise Exception('{"code":"PGRST205","message":"Could not find the table in the schema cache"}')
        rows = self.db.tables[self.table_name]
        key = self.db.keys[self.table_name]
        if self.op == "insert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault(key, f"{self.table_name}-{len(rows) + 1}")
                if any(r.get(key) == row[key] for r in rows):
                    raise Exception('{"code": "23505", "message": "duplicate key value violates unique constraint"}')
                rows.append(row)
                out.append(row)
            return _Result(out)
        if self.op == "upsert":
            row = dict(self.payload)
            conflict = self.kwargs.get("on_conflict") or key
            existing = next((r for r in rows if r.get(conflict) == row.get(conflict)), None)
            if existing is not None:
                if self.kwargs.get("ignore_duplicates"):
                    return _Result([])
                existing.update(row)
                return _Result([existing])
            row.setdefault(key, f"{self.table_name}-{len(rows) + 1}")
            rows.append(row)
            return _Result([row])
        if self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self.payload)
            return _Result(hit)
        if self.op == "delete":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                rows.remove(r)
            return _Result(hit)
        hit = [r for r in rows if self._match(r)]
        if self._order:
            col, desc = self._order
            hit.sort(key=lambda r: (r.get(col) is None, str(r.get(col) or "")), reverse=desc)
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
    keys = {
        "stores": "id",
        "profiles": "id",
        "subscriptions": "store_id",
        "platform_admins": "user_id",
        "partners": "id",
        "admin_actions": "id",
        "sales": "id",
        "products": "id",
        "customers": "id",
        "audit_logs": "id",
    }

    def __init__(self):
        self.tables = {k: [] for k in self.keys}

    def table(self, name):
        return _Query(self, name)

    def add(self, table, **row):
        self.tables[table].append(row)
        return row

    def rows(self, table):
        return self.tables[table]


AUTH_USERS = [
    {"id": "u1", "email": "one@example.com", "created_at": days(-5), "last_sign_in_at": days(-1)},
    {"id": "u2", "email": "two@example.com", "created_at": days(-60), "last_sign_in_at": days(-2)},
    {"id": "u2c", "email": "cashier@store.local", "created_at": days(-50), "last_sign_in_at": None},
    {"id": "u3", "email": "three@example.com", "created_at": days(-50), "last_sign_in_at": days(-20)},
    {"id": "staff", "email": "staff@kashpoint.co.za", "created_at": days(-90), "last_sign_in_at": days(0)},
    {"id": "pt_user", "email": "shop@techzone.co.za", "created_at": days(-30), "last_sign_in_at": days(0)},
    {"id": "newbie", "email": "newbie@example.com", "created_at": days(0), "last_sign_in_at": None},
]


@pytest.fixture
def db():
    d = FakeSupabase()
    d.add("partners", id="pt1", name="TechZone", code="TECHZONE", created_at=days(-40))
    d.add("stores", id="s_trial", name="Trial Shop", owner_id="u1", phone="+27820000001", created_at=days(-5), partner_id="pt1")
    d.add("stores", id="s_pay", name="Paying Shop", owner_id="u2", phone=None, created_at=days(-60), partner_id=None)
    d.add("stores", id="s_ended", name="Ended Shop", owner_id="u3", phone=None, created_at=days(-50), partner_id="pt1")
    d.add("profiles", id="u1", name="Owner One", role="admin", store_id="s_trial")
    d.add("profiles", id="u2", name="Owner Two", role="admin", store_id="s_pay")
    d.add("profiles", id="u2c", name="cashier", role="cashier", store_id="s_pay")
    d.add("profiles", id="u3", name="Owner Three", role="admin", store_id="s_ended")
    d.add("subscriptions", store_id="s_trial", plan="pro", status="trialing", trial_end=days(12), paystack_subscription_code=None)
    d.add(
        "subscriptions", store_id="s_pay", plan="pro", status="active", trial_end=days(-30),
        current_period_end=days(20), paystack_subscription_code="SUB_1", paystack_email_token="secret",
    )
    d.add("subscriptions", store_id="s_ended", plan="pro", status="trialing", trial_end=days(-3), paystack_subscription_code=None)
    d.add("products", id=1, store_id="s_pay")
    d.add("products", id=2, store_id="s_pay")
    d.add("sales", id=1, store_id="s_pay", total_price=120.0, timestamp=days(-1))
    d.add("sales", id=2, store_id="s_pay", total_price=80.0, timestamp=days(-2))
    d.add("sales", id=3, store_id="s_pay", total_price=999.0, timestamp=days(-45))
    d.add("platform_admins", user_id="staff", email="staff@kashpoint.co.za", role="owner", partner_id=None, created_at=days(-90))
    d.add("platform_admins", user_id="pt_user", email="shop@techzone.co.za", role="partner", partner_id="pt1", created_at=days(-30))
    return d


@pytest.fixture(autouse=True)
def fake_backend(db):
    with patch("app.db.supabase.get_supabase_client", return_value=db), patch(
        "app.services.subscriptions.get_supabase_client", return_value=db
    ), patch("app.services.platform_admin.list_auth_users", return_value=AUTH_USERS):
        yield


def _client_as(role, user_id, email, partner_id=None):
    from app.main import app
    from app.services.platform_admin import PlatformAdmin, get_platform_admin

    app.dependency_overrides[get_platform_admin] = lambda: PlatformAdmin(
        user_id=user_id, email=email, role=role, partner_id=partner_id
    )
    return TestClient(app)


@pytest.fixture
def owner():
    from app.main import app

    yield _client_as("owner", "staff", "staff@kashpoint.co.za")
    app.dependency_overrides.clear()


@pytest.fixture
def partner():
    from app.main import app

    yield _client_as("partner", "pt_user", "shop@techzone.co.za", partner_id="pt1")
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Getting in
# ---------------------------------------------------------------------------

class TestAccess:
    def test_ordinary_account_is_refused(self, db):
        from app.main import app

        with patch("app.services.platform_admin.verify_supabase_jwt", return_value={"sub": "u1", "email": "one@example.com"}):
            res = TestClient(app).get("/admin/me", headers={"Authorization": "Bearer x"})
        assert res.status_code == 403

    def test_no_token_is_401(self):
        from app.main import app

        assert TestClient(app).get("/admin/me").status_code == 401

    def test_row_in_platform_admins_opens_the_portal(self, db):
        from app.main import app

        with patch("app.services.platform_admin.verify_supabase_jwt", return_value={"sub": "pt_user", "email": "shop@techzone.co.za"}):
            res = TestClient(app).get("/admin/me", headers={"Authorization": "Bearer x"})
        assert res.status_code == 200
        body = res.json()
        assert body["role"] == "partner"
        assert body["partner"]["code"] == "TECHZONE"
        assert body["tables_ready"] is True

    def test_bootstrap_email_becomes_owner_and_is_recorded(self, db):
        from app.main import app

        with patch.dict(os.environ, {"PLATFORM_ADMIN_EMAILS": "Founder@KashPoint.co.za, other@x.y"}), patch(
            "app.services.platform_admin.verify_supabase_jwt",
            return_value={"sub": "founder", "email": "founder@kashpoint.co.za"},
        ):
            res = TestClient(app).get("/admin/me", headers={"Authorization": "Bearer x"})
        assert res.status_code == 200
        assert res.json()["role"] == "owner"
        rows = [r for r in db.rows("platform_admins") if r["user_id"] == "founder"]
        assert rows and rows[0]["role"] == "owner" and rows[0]["email"] == "founder@kashpoint.co.za"

    def test_profile_reports_platform_role(self, db):
        from app.main import app
        from app.api.deps import RequestContext, get_current_context

        app.dependency_overrides[get_current_context] = lambda: RequestContext("pt_user", "s_trial", "admin", email="shop@techzone.co.za")
        try:
            res = TestClient(app).get("/profile")
        finally:
            app.dependency_overrides.clear()
        assert res.status_code == 200
        assert res.json()["platform_role"] == "partner"
        assert res.json()["partner_id"] == "pt1"

    def test_profile_without_row_has_no_platform_role(self, db):
        from app.main import app
        from app.api.deps import RequestContext, get_current_context

        db.add("profiles", id="u9", name="x", role="admin", store_id="s_pay")
        app.dependency_overrides[get_current_context] = lambda: RequestContext("u9", "s_pay", "admin", email="nine@example.com")
        try:
            res = TestClient(app).get("/profile")
        finally:
            app.dependency_overrides.clear()
        assert res.json()["platform_role"] is None


# ---------------------------------------------------------------------------
# What each role sees
# ---------------------------------------------------------------------------

class TestStoresList:
    def test_owner_sees_every_store_with_its_state(self, owner):
        res = owner.get("/admin/stores")
        assert res.status_code == 200
        body = res.json()
        by_id = {i["id"]: i for i in body["items"]}
        assert set(by_id) == {"s_trial", "s_pay", "s_ended"}
        assert by_id["s_trial"]["subscription"]["state"] == "trial"
        assert by_id["s_trial"]["subscription"]["days_left"] == 12
        assert by_id["s_trial"]["owner_email"] == "one@example.com"
        assert by_id["s_trial"]["partner"]["code"] == "TECHZONE"
        assert by_id["s_pay"]["subscription"]["state"] == "active"
        assert by_id["s_pay"]["subscription"]["paying"] is True
        assert by_id["s_pay"]["users_count"] == 2
        assert by_id["s_pay"]["products_count"] == 2
        # Only the two sales inside the 30-day window count.
        assert by_id["s_pay"]["sales_30d_count"] == 2
        assert by_id["s_pay"]["sales_30d_total"] == 200.0
        assert by_id["s_ended"]["subscription"]["state"] == "trial_ended"
        assert [p["code"] for p in body["partners"]] == ["TECHZONE"]

    def test_search_and_state_filters(self, owner):
        assert [i["id"] for i in owner.get("/admin/stores", params={"q": "two@"}).json()["items"]] == ["s_pay"]
        assert [i["id"] for i in owner.get("/admin/stores", params={"q": "0820000001"}).json()["items"]] == ["s_trial"]
        states = {i["id"] for i in owner.get("/admin/stores", params={"state": "trial,trial_ending"}).json()["items"]}
        assert states == {"s_trial"}
        assert [i["id"] for i in owner.get("/admin/stores", params={"partner_id": "none"}).json()["items"]] == ["s_pay"]

    def test_partner_sees_only_their_stores_and_no_notes(self, partner, db):
        db.rows("stores")[0]["admin_notes"] = "called twice"
        res = partner.get("/admin/stores")
        assert res.status_code == 200
        body = res.json()
        assert {i["id"] for i in body["items"]} == {"s_trial", "s_ended"}
        assert all(i["admin_notes"] is None for i in body["items"])
        assert body["partners"] == []

    def test_partner_cannot_open_another_partners_store(self, partner, db):
        assert partner.get("/admin/stores/s_pay").status_code == 404
        next(r for r in db.rows("subscriptions") if r["store_id"] == "s_trial")["paystack_customer_code"] = "CUS_x"
        res = partner.get("/admin/stores/s_trial")
        assert res.status_code == 200
        # Where the store stands, yes; its billing identifiers and staff notes, no.
        assert "paystack_customer_code" not in res.json()["subscription"]
        assert res.json()["actions"] == []
        assert "admin_notes" not in res.json()["store"]

    def test_store_detail(self, owner):
        res = owner.get("/admin/stores/s_pay")
        assert res.status_code == 200
        body = res.json()
        assert body["summary"]["name"] == "Paying Shop"
        assert [u["email"] for u in body["users"]] == ["two@example.com", "cashier@store.local"]
        assert body["users"][0]["is_owner"] is True
        assert "paystack_email_token" not in body["subscription"]
        assert body["usage"]["sales_total_count"] == 3
        assert body["usage"]["sales_total_amount"] == 1199.0
        assert body["usage"]["sales_30d_count"] == 2

    def test_overview(self, owner):
        res = owner.get("/admin/overview")
        assert res.status_code == 200
        body = res.json()
        assert body["stores_total"] == 3
        assert body["stores_new_7d"] == 1
        assert body["paying"] == 1
        assert body["mrr_zar"] == 150
        assert body["states"] == {"trial": 1, "active": 1, "trial_ended": 1}
        assert len(body["signups_by_week"]) == 8
        assert sum(w["count"] for w in body["signups_by_week"]) >= 1
        assert body["sales_30d_total"] == 200.0
        # The trial shop has never sold and is 5 days old: too new to be at risk yet.
        assert [i["id"] for i in body["at_risk"]] == []


# ---------------------------------------------------------------------------
# Staff actions
# ---------------------------------------------------------------------------

class TestActions:
    def test_partner_cannot_change_anything(self, partner):
        assert partner.post("/admin/stores/s_trial/trial", json={"days": 7}).status_code == 403
        assert partner.post("/admin/stores/s_trial/comp", json={"months": 1}).status_code == 403
        assert partner.post("/admin/stores/s_trial/suspend", json={}).status_code == 403
        assert partner.patch("/admin/stores/s_trial", json={"admin_notes": "x"}).status_code == 403
        assert partner.get("/admin/partners").status_code == 403
        assert partner.get("/admin/actions").status_code == 403

    def test_extend_trial_adds_to_the_remaining_days_and_logs(self, owner, db):
        res = owner.post("/admin/stores/s_trial/trial", json={"days": 10})
        assert res.status_code == 200, res.text
        new_end = datetime.fromisoformat(res.json()["trial_end"])
        assert timedelta(days=21, hours=23) < new_end - NOW <= timedelta(days=22)
        assert res.json()["summary"]["state"] == "trial"
        assert res.json()["note"] is None
        log = db.rows("admin_actions")
        assert log and log[-1]["action"] == "trial.extend" and log[-1]["store_id"] == "s_trial"
        assert log[-1]["actor_email"] == "staff@kashpoint.co.za"

    def test_extend_trial_restarts_an_ended_trial_from_today(self, owner, db):
        res = owner.post("/admin/stores/s_ended/trial", json={"days": 14})
        assert res.status_code == 200
        new_end = datetime.fromisoformat(res.json()["trial_end"])
        # Counted from the moment of the request, not from the old end date.
        assert timedelta(days=13, hours=23) < new_end - NOW <= timedelta(days=14, minutes=5)
        sub = next(r for r in db.rows("subscriptions") if r["store_id"] == "s_ended")
        assert sub["status"] == "trialing" and sub["plan"] == "pro"
        assert subscriptions.get_store_plan("s_ended").is_on_trial

    def test_extend_trial_refuses_a_paying_store(self, owner):
        res = owner.post("/admin/stores/s_pay/trial", json={"days": 7})
        assert res.status_code == 400
        assert "paying" in res.json()["detail"]

    def test_extend_trial_creates_the_missing_row(self, owner, db):
        db.add("stores", id="s_bare", name="Bare", owner_id="newbie", created_at=days(-1), partner_id=None)
        db.add("profiles", id="newbie", name="n", role="admin", store_id="s_bare")
        res = owner.post("/admin/stores/s_bare/trial", json={"days": 5})
        assert res.status_code == 200
        assert any(r["store_id"] == "s_bare" for r in db.rows("subscriptions"))

    def test_comp_grants_pro_until_the_date_and_the_till_sees_it(self, owner, db):
        res = owner.post("/admin/stores/s_ended/comp", json={"until": (NOW + timedelta(days=40)).date().isoformat(), "reason": "hardware partner demo"})
        assert res.status_code == 200, res.text
        assert res.json()["summary"]["state"] == "comped"
        limits = subscriptions.get_store_plan("s_ended")
        assert limits.is_active and limits.plan == "pro" and limits.max_products is None
        info = subscriptions.get_plan_info("s_ended")
        assert info["is_active"] is True and info["comp_until"]

        res = owner.delete("/admin/stores/s_ended/comp")
        assert res.status_code == 200
        assert res.json()["summary"]["state"] == "trial_ended"
        assert not subscriptions.get_store_plan("s_ended").is_active

    def test_comp_by_months_and_bad_dates(self, owner):
        res = owner.post("/admin/stores/s_trial/comp", json={"months": 2})
        assert res.status_code == 200
        until = datetime.fromisoformat(res.json()["comp_until"])
        assert timedelta(days=58) < until - NOW < timedelta(days=63)
        assert owner.post("/admin/stores/s_trial/comp", json={"until": "yesterday"}).status_code == 400
        assert owner.post("/admin/stores/s_trial/comp", json={"until": (NOW - timedelta(days=1)).date().isoformat()}).status_code == 400
        assert owner.post("/admin/stores/s_trial/comp", json={}).status_code == 400

    def test_suspend_locks_even_a_paying_store_and_unsuspend_restores(self, owner, db):
        assert subscriptions.get_store_plan("s_pay").is_active
        res = owner.post("/admin/stores/s_pay/suspend", json={"reason": "chargeback"})
        assert res.status_code == 200
        assert res.json()["summary"]["state"] == "suspended"
        limits = subscriptions.get_store_plan("s_pay")
        assert not limits.is_active and limits.status == "suspended" and limits.max_products == 10
        assert subscriptions.get_plan_info("s_pay")["suspended"] is True

        res = owner.delete("/admin/stores/s_pay/suspend")
        assert res.status_code == 200
        assert res.json()["summary"]["state"] == "active"
        assert subscriptions.get_store_plan("s_pay").is_active
        actions = [a["action"] for a in db.rows("admin_actions")]
        assert actions[-2:] == ["store.suspend", "store.unsuspend"]

    def test_patch_store_partner_and_notes(self, owner, db):
        res = owner.patch("/admin/stores/s_pay", json={"partner_id": "pt1", "admin_notes": "  set up by TechZone  "})
        assert res.status_code == 200, res.text
        store = next(s for s in db.rows("stores") if s["id"] == "s_pay")
        assert store["partner_id"] == "pt1" and store["admin_notes"] == "set up by TechZone"
        assert owner.patch("/admin/stores/s_pay", json={"partner_id": "nope"}).status_code == 404
        res = owner.patch("/admin/stores/s_pay", json={"partner_id": None, "admin_notes": ""})
        assert res.status_code == 200
        assert store["partner_id"] is None and store["admin_notes"] is None
        assert owner.patch("/admin/stores/s_pay", json={}).status_code == 400

    def test_password_reset_emails_the_owner(self, owner, db):
        with patch("app.services.platform_admin.get_auth_user", return_value={"id": "u2", "email": "two@example.com"}), patch(
            "app.services.platform_admin.send_password_reset"
        ) as send:
            res = owner.post("/admin/stores/s_pay/password-reset")
        assert res.status_code == 200
        assert res.json()["email"] == "two@example.com"
        send.assert_called_once()
        assert send.call_args.args[0] == "two@example.com"
        assert db.rows("admin_actions")[-1]["action"] == "owner.password_reset"


# ---------------------------------------------------------------------------
# Partners and staff accounts
# ---------------------------------------------------------------------------

class TestPartners:
    def test_list_create_update(self, owner, db):
        res = owner.get("/admin/partners")
        assert res.status_code == 200
        assert res.json()["items"][0]["stores_count"] == 2
        assert res.json()["items"][0]["admins"] == [{"user_id": "pt_user", "email": "shop@techzone.co.za"}]

        res = owner.post("/admin/partners", json={"name": "PC Palace", "code": "pc palace", "contact_phone": "011 000 0000"})
        assert res.status_code == 201, res.text
        assert res.json()["code"] == "PC-PALACE"
        assert owner.post("/admin/partners", json={"name": "Dup", "code": "techzone"}).status_code == 409
        assert owner.post("/admin/partners", json={"name": "Bad", "code": "!!"}).status_code == 400

        pid = res.json()["id"]
        res = owner.patch(f"/admin/partners/{pid}", json={"name": "PC Palace Fordsburg", "code": "PCP"})
        assert res.status_code == 200
        assert res.json()["name"] == "PC Palace Fordsburg" and res.json()["code"] == "PCP"
        assert owner.patch(f"/admin/partners/{pid}", json={"code": "TECHZONE"}).status_code == 409

    def test_add_and_remove_partner_login(self, owner, db):
        res = owner.post("/admin/partners/pt1/admins", json={"email": "Newbie@example.com"})
        assert res.status_code == 201, res.text
        row = next(r for r in db.rows("platform_admins") if r["user_id"] == "newbie")
        assert row["role"] == "partner" and row["partner_id"] == "pt1" and row["email"] == "newbie@example.com"

        assert owner.post("/admin/partners/pt1/admins", json={"email": "nobody@example.com"}).status_code == 404
        assert owner.post("/admin/partners/pt1/admins", json={"email": "staff@kashpoint.co.za"}).status_code == 400

        assert owner.delete("/admin/partners/pt1/admins/newbie").status_code == 204
        assert not any(r["user_id"] == "newbie" for r in db.rows("platform_admins"))

    def test_staff_accounts(self, owner, db):
        res = owner.get("/admin/admins")
        assert res.status_code == 200
        assert [a["email"] for a in res.json()["items"]] == ["staff@kashpoint.co.za", "shop@techzone.co.za"]
        assert res.json()["items"][0]["is_you"] is True

        assert owner.post("/admin/admins", json={"email": "newbie@example.com"}).status_code == 201
        assert any(r["user_id"] == "newbie" and r["role"] == "owner" for r in db.rows("platform_admins"))
        assert owner.delete("/admin/admins/staff").status_code == 400
        assert owner.delete("/admin/admins/newbie").status_code == 204

    def test_action_log_names_stores(self, owner):
        owner.post("/admin/stores/s_trial/trial", json={"days": 1})
        res = owner.get("/admin/actions")
        assert res.status_code == 200
        item = res.json()["items"][0]
        assert item["action"] == "trial.extend" and item["store_name"] == "Trial Shop"

    def test_missing_tables_are_reported_not_500_garbage(self, owner, db):
        del db.tables["partners"]
        res = owner.get("/admin/partners")
        assert res.status_code == 500
        assert "20261108120000_admin_portal.sql" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Signup with a partner code
# ---------------------------------------------------------------------------

class TestSignupPartnerCode:
    def test_store_is_linked_to_the_partner(self, db):
        from app.api import deps

        deps._create_store_and_profile(db, "newbie", {"store_name": "Corner Cafe", "email": "newbie@example.com", "partner_code": " techzone "})
        store = next(s for s in db.rows("stores") if s["owner_id"] == "newbie")
        assert store["partner_id"] == "pt1"
        assert any(r["store_id"] == store["id"] for r in db.rows("subscriptions"))

    def test_unknown_code_does_not_block_signup(self, db):
        from app.api import deps

        deps._create_store_and_profile(db, "newbie", {"store_name": "Corner Cafe", "email": "newbie@example.com", "partner_code": "NOPE"})
        store = next(s for s in db.rows("stores") if s["owner_id"] == "newbie")
        assert "partner_id" not in store or store["partner_id"] is None

    def test_missing_partner_column_is_dropped(self, db):
        """A database without the admin-portal migration still signs up."""
        from app.api import deps

        real_table = db.table

        class _Strict(_Query):
            def execute(self):
                if self.op == "insert" and "partner_id" in (self.payload or {}):
                    raise Exception("{'code': 'PGRST204', 'message': \"Could not find the 'partner_id' column of 'stores' in the schema cache\"}")
                return super().execute()

        db.table = lambda name: _Strict(db, name) if name == "stores" else real_table(name)
        deps._create_store_and_profile(db, "newbie", {"store_name": "Corner Cafe", "email": "newbie@example.com", "partner_code": "TECHZONE"})
        store = next(s for s in db.rows("stores") if s["owner_id"] == "newbie")
        assert "partner_id" not in store


# ---------------------------------------------------------------------------
# The plan logic behind it all
# ---------------------------------------------------------------------------

class TestPlanOverrides:
    def test_suspension_outranks_everything(self):
        limits = PlanLimits("pro", status="active", current_period_end=days(30), has_card=True, suspended_at=days(-1))
        assert not limits.is_active and not limits.is_on_trial
        assert limits.status == "suspended"
        assert limits.max_products == 10 and limits.max_users == 1

    def test_comp_grants_pro_whatever_billing_says(self):
        limits = PlanLimits("expired", status="expired", comp_until=days(10))
        assert limits.is_active and limits.plan == "pro"
        assert limits.max_products is None and limits.allow_csv_export

    def test_expired_comp_changes_nothing(self):
        limits = PlanLimits("expired", status="expired", comp_until=days(-1))
        assert not limits.is_active and limits.plan == "expired"

    def test_comp_does_not_lift_a_suspension(self):
        limits = PlanLimits("pro", status="active", comp_until=days(10), suspended_at=days(-1))
        assert not limits.is_active


class TestAuditLogWrites:
    def test_writes_the_columns_the_table_has(self, db):
        from app.services.audit_log import log_audit_event

        log_audit_event("s_trial", "u1", "create", "product", 42, "Added Coke")
        rows = db.rows("audit_logs")
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "product" and rows[0]["entity_id"] == "42"
        assert "resource_type" not in rows[0] and "timestamp" not in rows[0]
        assert rows[0]["created_at"]
