"""Regression tests for two billing faults that let paid access leak.

1. Nothing in the system ever expired a subscription on a timer, and
   PlanLimits.is_active ignored current_period_end — so a store whose
   cancellation or renewal failure never reached us kept full Pro access
   indefinitely.

2. The webhook de-duplication key was `paystack:<data.id>`. Paystack's
   `data.id` identifies the object the event is about, not the event, so
   subscription.create / .enable / .disable for one subscription all share
   it. Every event after the first looked like a replay, and cancellations
   were dropped.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.subscriptions import PlanLimits

SECRET = "test-paystack-secret"


def iso_in(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat().replace("+00:00", "Z")


class TestPaidPeriodExpiry:
    def test_active_plan_with_future_period_end_is_active(self):
        limits = PlanLimits("pro", status="active", current_period_end=iso_in(days=10))
        assert limits.is_active is True
        assert limits.max_products is None

    def test_active_plan_with_lapsed_period_end_is_not_active(self):
        limits = PlanLimits("pro", status="active", current_period_end=iso_in(days=-1))
        assert limits.is_active is False
        # and the store falls back to free-tier limits
        assert limits.max_products == 10
        assert limits.max_users == 1
        assert limits.allow_advanced_reports is False

    def test_active_plan_without_period_end_stays_active(self):
        """Manually granted or one-off-paid stores must not be locked out."""
        limits = PlanLimits("pro", status="active", current_period_end=None)
        assert limits.is_active is True

    def test_unparseable_period_end_does_not_lock_the_store_out(self):
        limits = PlanLimits("pro", status="active", current_period_end="not-a-date")
        assert limits.is_active is True

    def test_naive_timestamp_is_treated_as_utc(self):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None).isoformat()
        assert PlanLimits("pro", status="active", current_period_end=past).is_active is False


class TestPastDueGrace:
    def test_past_due_keeps_access_until_the_paid_period_ends(self):
        limits = PlanLimits("pro", status="past_due", current_period_end=iso_in(days=3))
        assert limits.is_active is True

    def test_past_due_loses_access_once_the_period_ends(self):
        limits = PlanLimits("pro", status="past_due", current_period_end=iso_in(days=-1))
        assert limits.is_active is False

    def test_past_due_without_a_known_period_has_no_access(self):
        limits = PlanLimits("pro", status="past_due", current_period_end=None)
        assert limits.is_active is False


class TestTrialUnaffected:
    def test_live_trial_still_active(self):
        limits = PlanLimits("pro", status="trialing", trial_end=iso_in(days=5))
        assert limits.is_active is True

    def test_expired_trial_still_inactive(self):
        limits = PlanLimits("pro", status="trialing", trial_end=iso_in(days=-5))
        assert limits.is_active is False


def _post_webhook(client, payload: dict):
    raw = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest()
    return client.post(
        "/paystack/webhook",
        content=raw,
        headers={"x-paystack-signature": sig, "content-type": "application/json"},
    )


class TestWebhookReplayKey:
    """The cancellation of a subscription must not be mistaken for a replay
    of that same subscription's creation."""

    @patch.dict("os.environ", {"PAYSTACK_SECRET_KEY": SECRET, "PAYSTACK_SECRET_KEY_LIVE": SECRET, "PAYSTACK_MODE": ""})
    @patch("app.db.supabase.get_supabase_client")
    def test_disable_after_create_is_processed_not_deduped(self, mock_client):
        supa = MagicMock()
        mock_client.return_value = supa
        seen_ids = set()

        def insert_side_effect(row):
            result = MagicMock()
            if row["id"] in seen_ids:
                raise Exception('duplicate key value violates unique constraint "webhook_events_pkey"')
            seen_ids.add(row["id"])
            result.execute.return_value = MagicMock(data=[row])
            return result

        supa.table.return_value.insert.side_effect = insert_side_effect

        client = TestClient(app)
        # Both events carry the SAME data.id, as Paystack really sends them.
        sub = {"id": 555, "subscription_code": "SUB_x", "email_token": "tok",
               "metadata": {"store_id": "store-1", "plan": "pro"}}

        created = _post_webhook(client, {"event": "subscription.create", "data": sub})
        assert created.status_code == 200
        assert created.json().get("duplicate") is not True

        disabled = _post_webhook(client, {"event": "subscription.disable", "data": sub})
        assert disabled.status_code == 200
        assert disabled.json().get("duplicate") is not True, (
            "subscription.disable was discarded as a duplicate of subscription.create"
        )

        # Two distinct keys recorded, both namespaced by event type.
        assert seen_ids == {"paystack:subscription.create:555", "paystack:subscription.disable:555"}

    @patch.dict("os.environ", {"PAYSTACK_SECRET_KEY": SECRET, "PAYSTACK_SECRET_KEY_LIVE": SECRET, "PAYSTACK_MODE": ""})
    @patch("app.db.supabase.get_supabase_client")
    def test_a_genuine_retry_of_the_same_event_is_still_deduped(self, mock_client):
        supa = MagicMock()
        mock_client.return_value = supa
        seen_ids = set()

        def insert_side_effect(row):
            result = MagicMock()
            if row["id"] in seen_ids:
                raise Exception("duplicate key value violates unique constraint")
            seen_ids.add(row["id"])
            result.execute.return_value = MagicMock(data=[row])
            return result

        supa.table.return_value.insert.side_effect = insert_side_effect

        client = TestClient(app)
        event = {"event": "charge.success",
                 "data": {"id": 999, "metadata": {"store_id": "store-1", "plan": "pro"}}}

        assert _post_webhook(client, event).status_code == 200
        replay = _post_webhook(client, event)
        assert replay.status_code == 200
        assert replay.json().get("duplicate") is True
