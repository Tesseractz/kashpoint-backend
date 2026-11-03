"""Receipts: built from every line of a sale, branded with the shop's
details, and sent through the shop's own mailbox.

Before this, the emailed receipt showed only the first product of a
multi-item sale, under the name "KashPoint", and went out via a Brevo
account that was never activated ("API Key is not enabled").
"""
import smtplib
import socket
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import RequestContext, get_current_context
from app.main import app
from app.services import mailer, receipts
from app.services.store_profile import InvalidField, MissingColumns, clean_changes, read_store


# ---------------------------------------------------------------- in-memory Supabase
class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.op, self.payload, self.filters, self.in_filters = "select", None, [], []
        self._single, self._limit = False, None

    def select(self, *_, **__):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.in_filters.append((key, list(values)))
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
        out = [r for r in rows if all(r.get(k) == v for k, v in self.filters)]
        for k, vals in self.in_filters:
            out = [r for r in out if r.get(k) in vals]
        return out

    def execute(self):
        self.db.maybe_fail(self.table_name, self.op, self.payload)
        rows = self.db.tables.setdefault(self.table_name, [])
        if self.op == "insert":
            row = dict(self.payload)
            row.setdefault("id", len(rows) + 1)
            rows.append(row)
            return _Result([row])
        if self.op == "update":
            hit = self._hit(rows)
            for r in hit:
                r.update(self.payload)
            return _Result(hit)
        hit = sorted(self._hit(rows), key=lambda r: r.get("id") or 0)
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
    def __init__(self):
        self.tables = {"sales": [], "products": [], "profiles": [], "customers": [], "stores": []}
        self._failures = []

    def fail(self, table, op, exc, when=None):
        self._failures.append((table, op, exc, when))

    def maybe_fail(self, table, op, payload):
        for t, o, exc, when in self._failures:
            if t == table and o == op and (when is None or when(payload)):
                raise exc

    def table(self, name):
        return _Query(self, name)


SPAZA = {
    "id": "s1",
    "name": "Kj General Fashion",
    "address": "12 Market St, Mayfair, Johannesburg",
    "phone": "+27821234567",
    "email": "kj@example.com",
    "vat_number": "4123456789",
    "registration_number": "2020/123456/07",
    "receipt_footer": "No returns without this slip.",
    "logo_url": "https://cdn.example.com/kj.png",
    "website": None,
}


def _db(store=SPAZA):
    db = FakeSupabase()
    db.tables["stores"].append(dict(store))
    db.tables["products"] += [
        {"id": 10, "name": "Levi's Jeans", "price": 450.0, "sku": "LJ-32"},
        {"id": 11, "name": "T-shirt <white>", "price": 120.0, "sku": None},
    ]
    db.tables["profiles"].append({"id": "u-cashier", "name": "Thabo", "store_id": "s1"})
    db.tables["customers"].append({"id": "c1", "store_id": "s1", "name": "Naledi M", "phone": "+27831112222", "email": "naledi@example.com"})
    db.tables["sales"] += [
        {"id": 131, "store_id": "s1", "product_id": 10, "quantity_sold": 2, "total_price": 900.0, "subtotal": 900.0,
         "discount_amount": 0, "payment_method": "cash", "sold_by": "u-cashier", "customer_id": None,
         "timestamp": "2026-11-02T17:58:26+00:00"},
        {"id": 132, "store_id": "s1", "product_id": 11, "quantity_sold": 1, "total_price": 120.0, "subtotal": 120.0,
         "discount_amount": 0, "payment_method": "cash", "sold_by": "u-cashier", "customer_id": None,
         "timestamp": "2026-11-02T17:58:27+00:00"},
        {"id": 200, "store_id": "other", "product_id": 10, "quantity_sold": 1, "total_price": 450.0, "subtotal": 450.0,
         "timestamp": "2026-11-02T17:00:00+00:00"},
    ]
    return db


# ---------------------------------------------------------------- formatting
class TestFormatting:
    def test_money_groups_thousands_south_african_style(self):
        assert receipts.money(10000) == "R 10 000.00"
        assert receipts.money(1234567.891) == "R 1 234 567.89"
        assert receipts.money(0.5) == "R 0.50"
        assert receipts.money(-5) == "-R 5.00"

    def test_vat_is_the_fifteen_percent_inside_the_price(self):
        assert receipts.vat_breakdown(115) == {"rate": 15, "amount": 15.0, "excluding": 100.0}
        assert receipts.vat_breakdown(1020)["amount"] == 133.04

    def test_dates_read_in_south_african_time(self):
        assert receipts.format_sast("2026-11-02T17:58:26+00:00") == "02 Nov 2026, 19:58"
        assert receipts.format_sast("2026-11-02T17:58:26Z") == "02 Nov 2026, 19:58"
        assert receipts.format_sast("not a date") == "not a date"


# ---------------------------------------------------------------- building
class TestBuildReceipt:
    def test_every_line_of_the_sale_is_on_it(self):
        r = receipts.build_receipt(_db(), "s1", [131, 132], payment_method="cash", payment_amount=1100, change=80)
        assert r["number"] == 131
        assert [i["name"] for i in r["items"]] == ["Levi's Jeans", "T-shirt <white>"]
        assert r["items"][0] == {"name": "Levi's Jeans", "sku": "LJ-32", "quantity": 2, "unit_price": 450.0, "total": 900.0}
        assert r["total"] == 1020.0
        assert r["payment"] == {"method": "cash", "tendered": 1100.0, "change": 80.0}
        assert r["cashier"] == "Thabo"
        assert r["date_label"] == "02 Nov 2026, 19:58"

    def test_a_vat_registered_shop_issues_a_tax_invoice(self):
        r = receipts.build_receipt(_db(), "s1", [131, 132])
        assert r["title"] == "TAX INVOICE"
        assert r["vat"] == {"rate": 15, "amount": 133.04, "excluding": 886.96}
        assert r["store"]["vat_number"] == "4123456789"

    def test_without_a_vat_number_it_is_a_plain_receipt(self):
        r = receipts.build_receipt(_db({**SPAZA, "vat_number": None}), "s1", [131])
        assert r["title"] == "RECEIPT" and r["vat"] is None

    def test_the_customer_chosen_at_checkout_is_named(self):
        r = receipts.build_receipt(_db(), "s1", [131], customer_id="c1")
        assert r["customer"] == {"name": "Naledi M", "phone": "+27831112222", "email": "naledi@example.com"}

    def test_another_shops_sale_is_not_found(self):
        with pytest.raises(LookupError):
            receipts.build_receipt(_db(), "s1", [200])

    def test_card_payment_has_no_change(self):
        r = receipts.build_receipt(_db(), "s1", [131], payment_method="card")
        assert r["payment"] == {"method": "card", "tendered": 900.0, "change": 0.0}

    def test_works_before_the_receipt_columns_exist(self):
        db = _db({"id": "s1", "name": "Old Store"})  # a stores row from before the migration
        r = receipts.build_receipt(db, "s1", [131])
        assert r["store"]["name"] == "Old Store"
        assert r["store"]["vat_number"] is None and r["title"] == "RECEIPT"


class TestRendering:
    def test_email_carries_the_business_profile(self):
        r = receipts.build_receipt(_db(), "s1", [131, 132], payment_amount=1100, change=80)
        html = receipts.receipt_html(r)
        for needle in (
            "Kj General Fashion", "12 Market St, Mayfair", "+27821234567", "VAT No: 4123456789",
            "Reg No: 2020/123456/07", "TAX INVOICE", "No. 131", "Served by Thabo", "Levi&#x27;s Jeans",
            "R 1 020.00", "Includes VAT @ 15%", "R 133.04", "Cash tendered", "R 1 100.00", "R 80.00",
            "No returns without this slip.", 'src="https://cdn.example.com/kj.png"',
        ):
            assert needle in html, needle
        assert receipts.receipt_subject(r) == "Tax invoice #131 from Kj General Fashion"

    def test_a_two_line_address_stays_on_two_lines(self):
        html = receipts.receipt_html(receipts.build_receipt(_db({**SPAZA, "address": "12 Market St\nJohannesburg"}), "s1", [131]))
        assert "12 Market St<br>Johannesburg" in html

    def test_product_names_cannot_inject_html(self):
        html = receipts.receipt_html(receipts.build_receipt(_db(), "s1", [132]))
        assert "<white>" not in html and "&lt;white&gt;" in html

    def test_plain_text_version_reads_on_its_own(self):
        text = receipts.receipt_text(receipts.build_receipt(_db(), "s1", [131, 132]))
        assert text.startswith("KJ GENERAL FASHION")
        assert "2 x Levi's Jeans @ R 450.00" in text
        assert "TOTAL: R 1 020.00" in text
        assert "Includes VAT @ 15%: R 133.04" in text
        assert "No returns without this slip." in text

    def test_old_flat_shape_still_renders(self):
        from app.services.notifications import generate_receipt_html

        html = generate_receipt_html({"id": 7, "total": 50.0, "items": [{"name": "Bread", "quantity": 1, "price": 50.0, "total": 50.0}],
                                      "payment_method": "cash", "payment_amount": 100.0, "change": 50.0}, "Corner Shop")
        assert "Corner Shop" in html and "Receipt #7" in html and "Bread" in html


# ---------------------------------------------------------------- the endpoint
@pytest.fixture
def client():
    app.dependency_overrides[get_current_context] = lambda: RequestContext("u-cashier", "s1", "admin")
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSendReceiptEndpoint:
    def test_sends_all_lines_under_the_shops_name(self, client):
        db = _db()
        with patch("app.db.supabase.get_supabase_client", return_value=db), patch(
            "app.services.notifications.send_email",
            return_value=mailer.SendResult(success=True, message="Email sent successfully", recipient="x"),
        ) as send:
            res = client.post("/receipts/send", json={
                "sale_ids": [131, 132], "customer_email": "naledi@example.com", "send_email": True,
                "payment_method": "cash", "payment_amount": 1100, "change_amount": 80, "customer_id": "c1",
            })
        assert res.status_code == 200, res.text
        assert res.json()["success"] is True
        args, kwargs = send.call_args
        assert args[0] == "naledi@example.com"
        assert args[1] == "Tax invoice #131 from Kj General Fashion"
        assert "T-shirt" in args[2] and "Levi" in args[2]
        assert kwargs["from_name"] == "Kj General Fashion via KashPoint"
        assert kwargs["reply_to"] == "kj@example.com"
        assert "Naledi M" in kwargs["text_body"]

    def test_older_client_sending_one_sale_id_still_works(self, client):
        with patch("app.db.supabase.get_supabase_client", return_value=_db()), patch(
            "app.services.notifications.send_email",
            return_value=mailer.SendResult(success=True, message="ok", recipient="x"),
        ) as send:
            res = client.post("/receipts/send", json={"sale_id": 131, "customer_email": "a@b.co", "send_email": True})
        assert res.status_code == 200 and send.called

    def test_a_failed_send_is_reported_not_hidden(self, client):
        with patch("app.db.supabase.get_supabase_client", return_value=_db()), patch(
            "app.services.notifications.send_email",
            return_value=mailer.SendResult(success=False, message="Email login failed: check SMTP_USER and SMTP_PASSWORD.", recipient="x"),
        ):
            res = client.post("/receipts/send", json={"sale_ids": [131], "customer_email": "a@b.co", "send_email": True})
        body = res.json()
        assert body["success"] is False
        assert "login failed" in body["message"]

    def test_another_shops_sale_is_404(self, client):
        with patch("app.db.supabase.get_supabase_client", return_value=_db()):
            res = client.post("/receipts/send", json={"sale_ids": [200], "customer_email": "a@b.co", "send_email": True})
        assert res.status_code == 404

    def test_email_requires_an_address(self, client):
        with patch("app.db.supabase.get_supabase_client", return_value=_db()):
            res = client.post("/receipts/send", json={"sale_ids": [131], "send_email": True, "customer_email": " "})
        assert res.status_code == 400


# ---------------------------------------------------------------- store profile
class TestStoreProfile:
    def test_cleaning_normalises_phone_and_vat(self):
        out = clean_changes({"phone": "082 123 4567", "vat_number": "4 123 456 789", "website": "kj.co.za", "name": " Kj ", "junk": "x"})
        assert out == {"phone": "+27821234567", "vat_number": "4123456789", "website": "https://kj.co.za", "name": "Kj"}

    @pytest.mark.parametrize("field,value", [("phone", "123"), ("vat_number", "123"), ("email", "nope"), ("name", "  "), ("logo_url", "ftp://x")])
    def test_bad_values_are_refused_with_the_field_named(self, field, value):
        with pytest.raises(InvalidField) as e:
            clean_changes({field: value})
        assert e.value.field == field

    def test_blank_clears_a_field(self):
        assert clean_changes({"receipt_footer": ""}) == {"receipt_footer": None}

    def test_read_fills_missing_columns_with_none(self):
        db = FakeSupabase()
        db.tables["stores"].append({"id": "s1", "name": "Old"})
        assert read_store(db, "s1")["vat_number"] is None

    def test_put_saves_and_returns_the_profile(self, client):
        db = _db()
        with patch("app.db.supabase.get_supabase_client", return_value=db):
            res = client.put("/store", json={"address": "1 New Rd", "phone": "0831112222", "receipt_footer": ""})
        assert res.status_code == 200, res.text
        assert res.json()["address"] == "1 New Rd" and res.json()["phone"] == "+27831112222"
        assert db.tables["stores"][0]["receipt_footer"] is None

    def test_put_explains_a_bad_phone(self, client):
        with patch("app.db.supabase.get_supabase_client", return_value=_db()):
            res = client.put("/store", json={"phone": "12"})
        assert res.status_code == 400 and "phone" in res.json()["detail"].lower()

    def test_put_before_the_migration_says_so(self, client):
        db = _db()
        db.fail("stores", "update", Exception("{'code': 'PGRST204', 'message': \"Could not find the 'vat_number' column of 'stores' in the schema cache\"}"))
        with patch("app.db.supabase.get_supabase_client", return_value=db):
            res = client.put("/store", json={"vat_number": "4123456789"})
        assert res.status_code == 409 and "database" in res.json()["detail"].lower()

    def test_cashiers_cannot_change_it(self):
        app.dependency_overrides[get_current_context] = lambda: RequestContext("u2", "s1", "cashier")
        try:
            res = TestClient(app).put("/store", json={"name": "Hijack"})
        finally:
            app.dependency_overrides.clear()
        assert res.status_code == 403

    def test_profile_includes_the_store(self, client):
        with patch("app.db.supabase.get_supabase_client", return_value=_db()):
            res = client.get("/profile")
        assert res.status_code == 200
        assert res.json()["store"]["vat_number"] == "4123456789"
        assert res.json()["store_name"] == "Kj General Fashion"


# ---------------------------------------------------------------- the mailbox
class TestMailer:
    @pytest.fixture(autouse=True)
    def _smtp_env(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "mail.example.co.za")
        monkeypatch.setenv("SMTP_PORT", "465")
        monkeypatch.setenv("SMTP_USER", "noreply@example.co.za")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        monkeypatch.setenv("BREVO_API_KEY", "also-set")
        monkeypatch.delenv("MAIL_RELAY_URL", raising=False)
        mailer.reset_transport_state()
        yield
        mailer.reset_transport_state()

    def test_the_mailbox_wins_over_brevo(self):
        assert mailer.transport() == "smtp"

    def test_message_is_complete_and_from_the_shop(self):
        smtp = MagicMock()
        with patch("smtplib.SMTP_SSL") as ssl_cls:
            ssl_cls.return_value.__enter__.return_value = smtp
            result = mailer.send_email("naledi@example.com", "Receipt #1", "<p>Hi <b>there</b></p>",
                                       from_name="Kj via KashPoint", reply_to="kj@example.com")
        assert result.success, result.message
        smtp.login.assert_called_once_with("noreply@example.co.za", "pw")
        msg = smtp.send_message.call_args.args[0]
        assert msg["From"] == "Kj via KashPoint <noreply@example.co.za>"
        assert msg["To"] == "naledi@example.com"
        assert msg["Reply-To"] == "kj@example.com"
        assert msg["Message-ID"].endswith("@example.co.za>")
        assert msg["Date"]
        parts = [p.get_content_type() for p in msg.walk()]
        assert "text/plain" in parts and "text/html" in parts
        assert "Hi there" in msg.get_body(("plain",)).get_content()

    def test_a_wrong_password_reads_as_such(self):
        with patch("smtplib.SMTP_SSL") as ssl_cls:
            ssl_cls.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
            result = mailer.send_email("a@b.co", "s", "<p>x</p>")
        assert not result.success and "login failed" in result.message

    def test_starttls_on_other_ports(self, monkeypatch):
        monkeypatch.setenv("SMTP_PORT", "587")
        smtp = MagicMock()
        with patch("smtplib.SMTP") as cls:
            cls.return_value.__enter__.return_value = smtp
            assert mailer.send_email("a@b.co", "s", "<p>x</p>").success
        smtp.starttls.assert_called_once()

    def test_brevo_not_enabled_gets_a_useful_message(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST")
        monkeypatch.setenv("BREVO_SENDER_EMAIL", "me@example.com")
        resp = MagicMock(status_code=401, content=b"x")
        resp.json.return_value = {"message": "API Key is not enabled", "code": "unauthorized"}
        with patch("httpx.post", return_value=resp):
            result = mailer.send_email("a@b.co", "s", "<p>x</p>")
        assert not result.success and "not activated" in result.message

    # -- Render's free plan: the mail ports are blocked, the HTTPS relay is not.
    def test_a_blocked_mail_port_falls_back_to_the_relay_with_the_same_login(self):
        relay = MagicMock(status_code=200, content=b"x")
        relay.json.return_value = {"ok": True}
        with patch("smtplib.SMTP_SSL", side_effect=socket.timeout("timed out")), patch("httpx.post", return_value=relay) as post:
            result = mailer.send_email("naledi@example.com", "Receipt #1", "<p>Hi</p>", from_name="Kj via KashPoint", reply_to="kj@example.com")
        assert result.success, result.message
        url = post.call_args.args[0]
        # An ordinary form post: the web host answers 406 to JSON requests.
        assert "json" not in post.call_args.kwargs
        body = post.call_args.kwargs["data"]
        assert url == mailer.DEFAULT_RELAY_URL
        assert body["user"] == "noreply@example.co.za" and body["password"] == "pw"
        assert body["host"] == "mail.example.co.za" and body["port"] == 465
        assert body["to"] == "naledi@example.com" and body["from_name"] == "Kj via KashPoint" and body["reply_to"] == "kj@example.com"
        assert body["html"] == "<p>Hi</p>" and body["text"] == "Hi"

    def test_after_one_timeout_the_process_skips_straight_to_the_relay(self):
        relay = MagicMock(status_code=200, content=b"x")
        relay.json.return_value = {"ok": True}
        with patch("smtplib.SMTP_SSL", side_effect=socket.timeout("timed out")) as direct, patch("httpx.post", return_value=relay):
            mailer.send_email("a@b.co", "s", "<p>x</p>")
            mailer.send_email("a@b.co", "s", "<p>x</p>")
        assert direct.call_count == 1

    def test_a_refused_login_never_goes_to_the_relay(self):
        with patch("smtplib.SMTP_SSL") as ssl_cls, patch("httpx.post") as post:
            ssl_cls.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
            result = mailer.send_email("a@b.co", "s", "<p>x</p>")
        assert not result.success and not post.called

    def test_the_relays_own_errors_reach_the_cashier(self):
        relay = MagicMock(status_code=401, content=b"x")
        relay.json.return_value = {"ok": False, "error": "Mailbox login failed: check SMTP_USER and SMTP_PASSWORD."}
        with patch("smtplib.SMTP_SSL", side_effect=socket.timeout("timed out")), patch("httpx.post", return_value=relay):
            result = mailer.send_email("a@b.co", "s", "<p>x</p>")
        assert not result.success and "login failed" in result.message

    def test_relay_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("MAIL_RELAY_URL", "")
        with patch("smtplib.SMTP_SSL", side_effect=socket.timeout("timed out")), patch("httpx.post") as post:
            result = mailer.send_email("a@b.co", "s", "<p>x</p>")
        assert not result.success and not post.called

    def test_html_to_text(self):
        assert mailer.html_to_text("<h1>Hi</h1><p>Total: <b>R 5</b></p>Bye<br>End") == "Hi\nTotal: R 5\nBye\nEnd"
