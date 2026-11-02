"""
Extended API tests covering additional endpoints.
Covers user management, profile, notifications, receipts, and webhook endpoints.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import json

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set environment variables
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"
os.environ["SUPABASE_JWT_SECRET"] = "test-jwt-secret"
os.environ["PASSWORD_ENCRYPTION_KEY"] = "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkw"
os.environ.pop("DEV_PLAN_OVERRIDE", None)


def create_mock_context(user_id="test-user-id", store_id="test-store-id", role="admin"):
    """Create a mock RequestContext."""
    from app.api.deps import RequestContext
    return RequestContext(user_id=user_id, store_id=store_id, role=role)


@pytest.fixture
def admin_client():
    """Create test client with admin role."""
    from app.main import app
    from app.api.deps import get_current_context
    
    app.dependency_overrides[get_current_context] = lambda: create_mock_context(role="admin")
    
    client = TestClient(app)
    yield client
    
    app.dependency_overrides.clear()


@pytest.fixture
def cashier_client():
    """Create test client with cashier role."""
    from app.main import app
    from app.api.deps import get_current_context
    
    app.dependency_overrides[get_current_context] = lambda: create_mock_context(role="cashier")
    
    client = TestClient(app)
    yield client
    
    app.dependency_overrides.clear()


class TestProfileAPI:
    """Tests for profile endpoint."""
    
    @patch('app.db.supabase.get_supabase_client')
    def test_get_profile_success(self, mock_supabase, admin_client):
        """Test getting profile successfully."""
        profile_data = {
            "id": "test-user-id",
            "name": "Test Admin",
            "role": "admin",
            "store_id": "test-store-id"
        }
        
        mock_query = MagicMock()
        mock_query.execute.return_value.data = [profile_data]
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        response = admin_client.get("/profile")
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-user-id"
        assert data["name"] == "Test Admin"
        assert data["role"] == "admin"
    
    @patch('app.db.supabase.get_supabase_client')
    def test_get_profile_fallback(self, mock_supabase, admin_client):
        """Test profile fallback when no profile in DB."""
        mock_query = MagicMock()
        mock_query.execute.return_value.data = []
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        response = admin_client.get("/profile")
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-user-id"
        assert data["role"] == "admin"


class TestUserInviteAPI:
    """Tests for user invite endpoint."""
    
    @patch('app.services.subscriptions.get_store_plan')
    @patch('app.db.supabase.get_supabase_client')
    def test_invite_user_limit_reached(self, mock_supabase, mock_plan, admin_client):
        """Test that user limit is enforced."""
        from app.services.subscriptions import PlanLimits
        
        mock_plan.return_value = PlanLimits("free", status="active")
        
        # Free plan has 1 user limit
        mock_query = MagicMock()
        mock_query.execute.return_value.data = [{"id": "existing-user"}]
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        response = admin_client.post(
            "/users/invite",
            json={"role": "cashier"}
        )
        
        assert response.status_code == 402
        assert "limit reached" in response.json()["detail"].lower()
    
    def test_invite_user_forbidden_for_cashier(self, cashier_client):
        """Test that cashiers cannot invite users."""
        response = cashier_client.post(
            "/users/invite",
            json={"role": "cashier"}
        )
        
        assert response.status_code == 403
    
    @patch('app.services.subscriptions.get_store_plan')
    @patch('app.db.supabase.get_supabase_client')
    def test_invite_user_invalid_role(self, mock_supabase, mock_plan, admin_client):
        """Test that invalid role is rejected."""
        from app.services.subscriptions import PlanLimits
        
        mock_plan.return_value = PlanLimits("pro", status="active")
        
        mock_query = MagicMock()
        mock_query.execute.return_value.data = []
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        response = admin_client.post(
            "/users/invite",
            json={"role": "superuser"}
        )
        
        assert response.status_code == 400
        assert "admin" in response.json()["detail"].lower() or "cashier" in response.json()["detail"].lower()


class TestUserRoleUpdateAPI:
    """Tests for user role update endpoint."""
    
    @patch('app.db.supabase.get_supabase_client')
    def test_update_role_success(self, mock_supabase, admin_client):
        """Test updating user role successfully."""
        mock_supabase_instance = MagicMock()
        
        # Profile query
        profile_query = MagicMock()
        profile_query.execute.return_value.data = {
            "id": "user-2",
            "name": "Cashier",
            "role": "cashier",
            "store_id": "test-store-id"
        }
        profile_query.execute.return_value.count = 2
        profile_query.select.return_value = profile_query
        profile_query.eq.return_value = profile_query
        profile_query.single.return_value = profile_query
        
        # Update query
        update_query = MagicMock()
        update_query.execute.return_value.data = None
        update_query.update.return_value = update_query
        update_query.eq.return_value = update_query
        
        # Updated profile query
        updated_query = MagicMock()
        updated_query.execute.return_value.data = {
            "id": "user-2",
            "name": "Cashier",
            "role": "admin",
            "store_id": "test-store-id"
        }
        updated_query.select.return_value = updated_query
        updated_query.eq.return_value = updated_query
        updated_query.single.return_value = updated_query
        
        call_count = [0]
        
        def table_router(table_name):
            if table_name == "profiles":
                call_count[0] += 1
                if call_count[0] == 1:
                    return profile_query
                elif call_count[0] == 2:
                    return update_query
                else:
                    return updated_query
            return MagicMock()
        
        mock_supabase_instance.table = table_router
        mock_supabase.return_value = mock_supabase_instance
        
        with patch('httpx.Client') as mock_http:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"email": "cashier@store.local"}
            mock_http.return_value.__enter__.return_value.get.return_value = mock_response
            
            response = admin_client.put(
                "/users/user-2/role",
                json={"role": "admin"}
            )
        
        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "admin"
    
    def test_update_role_forbidden_for_cashier(self, cashier_client):
        """Test that cashiers cannot update roles."""
        response = cashier_client.put(
            "/users/user-2/role",
            json={"role": "admin"}
        )
        
        assert response.status_code == 403
    
    @patch('app.db.supabase.get_supabase_client')
    def test_update_role_invalid_role(self, mock_supabase, admin_client):
        """Test that invalid role is rejected."""
        response = admin_client.put(
            "/users/user-2/role",
            json={"role": "superadmin"}
        )
        
        assert response.status_code == 400


class TestUserDeleteAPI:
    """Tests for user delete endpoint."""
    
    @patch('app.db.supabase.get_supabase_client')
    def test_delete_user_success(self, mock_supabase, admin_client):
        """Test deleting user successfully."""
        mock_supabase_instance = MagicMock()
        
        # Profile query
        profile_query = MagicMock()
        profile_query.execute.return_value.data = {
            "id": "user-to-delete",
            "role": "cashier",
            "store_id": "test-store-id"
        }
        profile_query.execute.return_value.count = 2
        profile_query.select.return_value = profile_query
        profile_query.eq.return_value = profile_query
        profile_query.single.return_value = profile_query
        
        # Delete query
        delete_query = MagicMock()
        delete_query.execute.return_value.data = None
        delete_query.delete.return_value = delete_query
        delete_query.eq.return_value = delete_query
        
        def table_router(table_name):
            if table_name == "profiles":
                return profile_query
            return MagicMock()
        
        mock_supabase_instance.table = table_router
        mock_supabase.return_value = mock_supabase_instance
        
        # Override profile query for delete operation
        def side_effect_table(name):
            if name == "profiles":
                q = MagicMock()
                q.execute.return_value.data = {
                    "id": "user-to-delete",
                    "role": "cashier",
                    "store_id": "test-store-id"
                }
                q.execute.return_value.count = 2
                q.select.return_value = q
                q.eq.return_value = q
                q.single.return_value = q
                q.delete.return_value = q
                return q
            return MagicMock()
        
        mock_supabase.return_value.table.side_effect = side_effect_table
        
        response = admin_client.delete("/users/user-to-delete")
        
        assert response.status_code == 204
    
    def test_delete_self_forbidden(self, admin_client):
        """Test that users cannot delete themselves."""
        response = admin_client.delete("/users/test-user-id")
        
        assert response.status_code == 400
        assert "yourself" in response.json()["detail"].lower()
    
    def test_delete_user_forbidden_for_cashier(self, cashier_client):
        """Test that cashiers cannot delete users."""
        response = cashier_client.delete("/users/other-user")
        
        assert response.status_code == 403


class TestUserCredentialsAPI:
    """Tests for user credentials endpoint."""
    
    @patch('app.db.supabase.get_supabase_client')
    def test_get_credentials_success(self, mock_supabase, admin_client):
        """Test getting user credentials successfully."""
        from app.main import _encrypt_password
        
        encrypted_pw = _encrypt_password("test-password-123")
        
        mock_query = MagicMock()
        mock_query.execute.return_value.data = {
            "id": "user-2",
            "name": "Cashier",
            "store_id": "test-store-id",
            "temp_password_encrypted": encrypted_pw
        }
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.single.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        with patch('httpx.Client') as mock_http:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"email": "cashier@store.local"}
            mock_http.return_value.__enter__.return_value.get.return_value = mock_response
            
            response = admin_client.get("/users/user-2/credentials")
        
        assert response.status_code == 200
        data = response.json()
        assert data["password"] == "test-password-123"
    
    @patch('app.db.supabase.get_supabase_client')
    def test_get_credentials_no_password_stored(self, mock_supabase, admin_client):
        """Test error when password is not stored."""
        mock_query = MagicMock()
        mock_query.execute.return_value.data = {
            "id": "user-2",
            "name": "Cashier",
            "store_id": "test-store-id",
            "temp_password_encrypted": None
        }
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.single.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        # Mock httpx for auth API call
        with patch('httpx.Client') as mock_http:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"email": "cashier@store.local"}
            mock_http.return_value.__enter__.return_value.get.return_value = mock_response
            
            response = admin_client.get("/users/user-2/credentials")
        
        assert response.status_code == 404
        assert "not stored" in response.json()["detail"].lower()
    
    def test_get_credentials_forbidden_for_cashier(self, cashier_client):
        """Test that cashiers cannot view credentials."""
        response = cashier_client.get("/users/user-2/credentials")
        
        assert response.status_code == 403


class TestNotificationsStatusAPI:
    """Tests for notification status endpoint."""
    
    @patch('app.services.notifications.is_email_configured')
    def test_get_notification_status(self, mock_configured, admin_client):
        """Test getting notification status."""
        mock_configured.return_value = True
        
        response = admin_client.get("/notifications/status")
        
        assert response.status_code == 200
        data = response.json()
        assert data["email_configured"] is True


class TestNotificationSettingsAPI:
    """Tests for notification settings endpoints."""
    
    @patch('app.db.supabase.get_supabase_client')
    def test_get_notification_settings(self, mock_supabase, admin_client):
        """Test getting notification settings."""
        mock_query = MagicMock()
        mock_query.execute.return_value.data = {
            "notification_email": "alerts@test.com",
            "low_stock_threshold": 15,
            "daily_summary_enabled": True
        }
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.single.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        response = admin_client.get("/notifications/settings")
        
        assert response.status_code == 200
        data = response.json()
        assert data["notification_email"] == "alerts@test.com"
    
    @patch('app.db.supabase.get_supabase_client')
    def test_update_notification_settings(self, mock_supabase, admin_client):
        """Test updating notification settings."""
        mock_query = MagicMock()
        mock_query.execute.return_value.data = [{
            "notification_email": "new@test.com",
            "low_stock_threshold": 20,
            "daily_summary_enabled": False
        }]
        mock_query.upsert.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        response = admin_client.put(
            "/notifications/settings",
            json={
                "notification_email": "new@test.com",
                "low_stock_threshold": 20,
                "daily_summary_enabled": False
            }
        )
        
        assert response.status_code == 200


class TestLowStockNotificationAPI:
    """Tests for low stock notification endpoint."""
    
    @patch('app.services.subscriptions.get_store_plan')
    def test_low_stock_notification_blocked_for_free(self, mock_plan, admin_client):
        """Test that free plan cannot send low stock notifications."""
        from app.services.subscriptions import PlanLimits
        
        mock_plan.return_value = PlanLimits("free", status="active")
        
        response = admin_client.post(
            "/notifications/low-stock",
            json={"threshold": 10}
        )
        
        assert response.status_code == 402
    
    @patch('app.services.subscriptions.get_store_plan')
    @patch('app.db.supabase.get_supabase_client')
    def test_low_stock_notification_no_products(self, mock_supabase, mock_plan, admin_client):
        """Test notification with no low stock products."""
        from app.services.subscriptions import PlanLimits
        
        mock_plan.return_value = PlanLimits("pro", status="active")
        
        mock_query = MagicMock()
        mock_query.execute.return_value.data = []
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.lte.return_value = mock_query
        mock_query.order.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query
        
        response = admin_client.post(
            "/notifications/low-stock",
            json={"threshold": 10}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "No products" in data["message"]


class TestDailySummaryNotificationAPI:
    """Tests for daily summary notification endpoint."""
    
    @patch('app.db.supabase.get_supabase_client')
    def test_daily_summary_without_email(self, mock_supabase, admin_client):
        """Test daily summary when email not configured."""
        mock_supabase_instance = MagicMock()
        
        # Sales query
        sales_query = MagicMock()
        sales_query.execute.return_value.data = []
        sales_query.select.return_value = sales_query
        sales_query.eq.return_value = sales_query
        sales_query.gte.return_value = sales_query
        sales_query.lte.return_value = sales_query
        sales_query.order.return_value = sales_query
        
        # Products query
        products_query = MagicMock()
        products_query.execute.return_value.data = []
        products_query.select.return_value = products_query
        products_query.eq.return_value = products_query
        
        # Settings query
        settings_query = MagicMock()
        settings_query.execute.return_value.data = None
        settings_query.select.return_value = settings_query
        settings_query.eq.return_value = settings_query
        settings_query.single.return_value = settings_query
        
        def table_router(table_name):
            if table_name == "sales":
                return sales_query
            elif table_name == "products":
                return products_query
            elif table_name == "notification_settings":
                return settings_query
            return MagicMock()
        
        mock_supabase_instance.table = table_router
        mock_supabase.return_value = mock_supabase_instance
        
        response = admin_client.post(
            "/notifications/daily-summary",
            json={"send_email": True}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "not configured" in data["message"].lower()


# The receipt endpoint (/receipts/send) is covered in tests/test_receipts.py
# against an in-memory database: every line of a multi-item sale, the shop's
# own details, 404 for another shop's sale, and a failed send being reported.


class TestBillingCheckoutAPI:
    """Tests for billing checkout endpoint."""
    
    def test_checkout_forbidden_for_cashier(self, cashier_client):
        """Test that cashiers cannot create checkout sessions."""
        response = cashier_client.post(
            "/billing/checkout",
            json={"plan": "pro"}
        )
        
        assert response.status_code == 403


class TestBillingCancelAPI:
    """Tests for billing cancel endpoint."""

    def test_cancel_forbidden_for_cashier(self, cashier_client):
        """Test that cashiers cannot cancel subscriptions."""
        response = cashier_client.post("/billing/cancel")

        assert response.status_code == 403
