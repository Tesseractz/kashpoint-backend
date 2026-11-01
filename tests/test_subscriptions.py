"""
Unit tests for subscription/billing logic.
Tests PlanLimits class and subscription enforcement.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Clear DEV_PLAN_OVERRIDE for tests
os.environ.pop("DEV_PLAN_OVERRIDE", None)


class TestPlanLimits:
    """Tests for PlanLimits class."""
    
    def test_free_plan_limits(self):
        """Test free plan has correct limits."""
        from app.services.subscriptions import PlanLimits
        
        limits = PlanLimits("free", status="active")
        
        assert limits.plan == "free"
        assert limits.is_active == False
        assert limits.is_on_trial == False
        assert limits.max_products == 10
        assert limits.max_users == 1
        assert limits.allow_csv_export == False
        assert limits.allow_low_stock_alerts == False
        assert limits.allow_audit_logs == False
        assert limits.allow_advanced_reports == False
    
    def test_pro_plan_limits(self):
        """Test pro plan has correct limits."""
        from app.services.subscriptions import PlanLimits
        
        limits = PlanLimits("pro", status="active")
        
        assert limits.plan == "pro"
        assert limits.is_active == True
        assert limits.is_on_trial == False
        assert limits.max_products is None  # Unlimited
        assert limits.max_users == 999  # Full access (single-plan billing)
        assert limits.allow_csv_export == True
        assert limits.allow_low_stock_alerts == True
        assert limits.allow_audit_logs == True
        assert limits.allow_advanced_reports == True
    
    def test_business_plan_limits(self):
        """Legacy business plan is normalized to pro for limits."""
        from app.services.subscriptions import PlanLimits

        limits = PlanLimits("business", status="active")

        assert limits.plan == "pro"
        assert limits.is_active == True
        assert limits.is_on_trial == False
        assert limits.max_products is None  # Unlimited
        assert limits.max_users == 999  # Unlimited
        assert limits.allow_csv_export == True
        assert limits.allow_low_stock_alerts == True
        assert limits.allow_audit_logs == True
        assert limits.allow_advanced_reports == True
    
    def test_expired_plan_limits(self):
        """Test expired plan has restricted limits."""
        from app.services.subscriptions import PlanLimits
        
        limits = PlanLimits("expired", status="expired")
        
        assert limits.is_active == False
        assert limits.is_on_trial == False
        assert limits.max_products == 10
        assert limits.max_users == 1
        assert limits.allow_csv_export == False
        assert limits.allow_low_stock_alerts == False
        assert limits.allow_audit_logs == False
        assert limits.allow_advanced_reports == False
    
    def test_active_trial_has_full_access(self):
        """Test that active trial gives full access to all features."""
        from app.services.subscriptions import PlanLimits
        
        # Trial ends 15 days from now
        trial_end = datetime.now(timezone.utc) + timedelta(days=15)
        limits = PlanLimits("pro", status="trialing", trial_end=trial_end.isoformat())
        
        assert limits.is_active == True
        assert limits.is_on_trial == True
        assert limits.max_products is None  # Unlimited during trial
        assert limits.max_users == 999  # Full access during trial
        assert limits.allow_csv_export == True
        assert limits.allow_low_stock_alerts == True
        assert limits.allow_audit_logs == True  # Full access during trial
        assert limits.allow_advanced_reports == True
    
    def test_expired_trial_has_restricted_access(self):
        """Test that expired trial has restricted access."""
        from app.services.subscriptions import PlanLimits
        
        # Trial ended 5 days ago
        trial_end = datetime.now(timezone.utc) - timedelta(days=5)
        limits = PlanLimits("pro", status="trialing", trial_end=trial_end.isoformat())
        
        assert limits.is_active == False
        assert limits.is_on_trial == False
        assert limits.max_products == 10  # Free plan limits
        assert limits.max_users == 1
        assert limits.allow_csv_export == False
        assert limits.allow_low_stock_alerts == False
        assert limits.allow_audit_logs == False
        assert limits.allow_advanced_reports == False
    
    def test_trial_with_no_end_date(self):
        """Pro + trialing without trial_end is treated as active paid (not open-ended trial perks)."""
        from app.services.subscriptions import PlanLimits

        limits = PlanLimits("pro", status="trialing", trial_end=None)

        assert limits.status == "active"
        assert limits.is_on_trial == False
        assert limits.is_active == True
        assert limits.allow_advanced_reports == True
    
    def test_trial_with_z_suffix_timezone(self):
        """Test trial end parsing with Z suffix."""
        from app.services.subscriptions import PlanLimits
        
        # Future trial end with Z suffix
        trial_end = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        limits = PlanLimits("pro", status="trialing", trial_end=trial_end)
        
        assert limits.is_on_trial == True
        assert limits.is_active == True
    
    def test_past_due_status(self):
        """Test past_due status is not active."""
        from app.services.subscriptions import PlanLimits
        
        limits = PlanLimits("pro", status="past_due")
        
        assert limits.is_active == False
        assert limits.is_on_trial == False
    
    def test_canceled_status(self):
        """Test canceled status is not active."""
        from app.services.subscriptions import PlanLimits
        
        limits = PlanLimits("pro", status="canceled")
        
        assert limits.is_active == False
        assert limits.is_on_trial == False


class TestEnforceLimits:
    """Tests for enforce_limits_on_create_product function."""
    
    @patch('app.services.subscriptions.get_supabase_client')
    @patch('app.services.subscriptions.get_store_plan')
    def test_enforce_limits_allows_under_limit(self, mock_get_plan, mock_get_supabase):
        """Test that products can be created when under limit."""
        from app.services.subscriptions import enforce_limits_on_create_product, PlanLimits
        
        # Mock free plan with 10 product limit
        mock_get_plan.return_value = PlanLimits("free", status="active")
        
        # Mock 5 existing products
        mock_supabase = MagicMock()
        mock_query = MagicMock()
        mock_query.execute.return_value.data = [{"id": i} for i in range(5)]
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.table.return_value = mock_query
        mock_get_supabase.return_value = mock_supabase
        
        # Should not raise
        enforce_limits_on_create_product("test-store-id")
    
    @patch('app.services.subscriptions.get_supabase_client')
    @patch('app.services.subscriptions.get_store_plan')
    def test_enforce_limits_blocks_at_limit(self, mock_get_plan, mock_get_supabase):
        """Test that products cannot be created when at limit."""
        from app.services.subscriptions import enforce_limits_on_create_product, PlanLimits
        from fastapi import HTTPException
        
        # Mock free plan with 10 product limit
        mock_get_plan.return_value = PlanLimits("free", status="active")
        
        # Mock 10 existing products (at limit)
        mock_supabase = MagicMock()
        mock_query = MagicMock()
        mock_query.execute.return_value.data = [{"id": i} for i in range(10)]
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.table.return_value = mock_query
        mock_get_supabase.return_value = mock_supabase
        
        # Should raise HTTP 402
        with pytest.raises(HTTPException) as exc_info:
            enforce_limits_on_create_product("test-store-id")
        
        assert exc_info.value.status_code == 402
        assert "Product limit reached" in exc_info.value.detail
    
    @patch('app.services.subscriptions.get_supabase_client')
    @patch('app.services.subscriptions.get_store_plan')
    def test_enforce_limits_allows_unlimited_for_pro(self, mock_get_plan, mock_get_supabase):
        """Test that pro plan has unlimited products."""
        from app.services.subscriptions import enforce_limits_on_create_product, PlanLimits
        
        # Mock pro plan with unlimited products
        mock_get_plan.return_value = PlanLimits("pro", status="active")
        
        # Should not raise even with many products
        enforce_limits_on_create_product("test-store-id")


class TestGetStorePlan:
    """Tests for get_store_plan function."""
    
    def test_get_store_plan_returns_subscription_data(self):
        """Test get_store_plan returns correct plan from database."""
        # Ensure DEV_PLAN_OVERRIDE is not set for this test
        os.environ.pop("DEV_PLAN_OVERRIDE", None)
        os.environ["DEV_PLAN_OVERRIDE"] = ""
        
        # Need to reimport to pick up env var change
        import importlib
        import app.services.subscriptions
        importlib.reload(app.services.subscriptions)
        
        # Now patch after reload
        with patch('app.services.subscriptions.get_supabase_client') as mock_get_supabase:
            from app.services.subscriptions import get_store_plan
            
            mock_supabase = MagicMock()
            mock_query = MagicMock()
            mock_query.execute.return_value.data = {
                "plan": "pro",
                "status": "active",
                "trial_end": None
            }
            mock_query.select.return_value = mock_query
            mock_query.eq.return_value = mock_query
            mock_query.single.return_value = mock_query
            mock_supabase.table.return_value = mock_query
            mock_get_supabase.return_value = mock_supabase
            
            limits = get_store_plan("test-store-id")
            
            assert limits.plan == "pro"
            assert limits.status == "active"
            assert limits.is_active == True

    def test_get_store_plan_fails_open_on_error(self):
        """A database error must fail open, not read as an expired plan.

        The old contract returned "expired" here, which told paying customers
        their subscription had lapsed every time Supabase hiccuped. See
        tests/test_plan_fail_open.py for the full matrix.
        """
        # Ensure DEV_PLAN_OVERRIDE is not set for this test
        os.environ.pop("DEV_PLAN_OVERRIDE", None)
        os.environ["DEV_PLAN_OVERRIDE"] = ""
        
        # Need to reimport to pick up env var change
        import importlib
        import app.services.subscriptions
        importlib.reload(app.services.subscriptions)
        
        # Now patch after reload
        with patch('app.services.subscriptions.get_supabase_client') as mock_get_supabase:
            from app.services.subscriptions import get_store_plan
            
            mock_supabase = MagicMock()
            mock_supabase.table.side_effect = Exception("Database error")
            mock_get_supabase.return_value = mock_supabase
            
            limits = get_store_plan("test-store-id")
            
            assert limits.is_active is True
            assert limits.degraded is True
    
    def test_dev_plan_override(self):
        """Test DEV_PLAN_OVERRIDE environment variable."""
        # Set the override
        os.environ["DEV_PLAN_OVERRIDE"] = "pro"
        
        # Need to reimport to pick up env var change
        import importlib
        import app.services.subscriptions
        importlib.reload(app.services.subscriptions)
        
        from app.services.subscriptions import get_store_plan
        
        limits = get_store_plan("test-store-id")
        
        assert limits.plan == "pro"
        assert limits.is_active == True
        
        # Clean up
        os.environ.pop("DEV_PLAN_OVERRIDE", None)
        os.environ["DEV_PLAN_OVERRIDE"] = ""
        importlib.reload(app.services.subscriptions)


class TestGetPlanInfo:
    """Tests for get_plan_info function."""
    
    @patch('app.services.subscriptions.get_supabase_client')
    @patch('app.services.subscriptions.get_store_plan')
    def test_get_plan_info_returns_complete_info(self, mock_get_plan, mock_get_supabase):
        """Test get_plan_info returns all expected fields."""
        from app.services.subscriptions import get_plan_info, PlanLimits
        
        mock_get_plan.return_value = PlanLimits("pro", status="active")
        
        mock_supabase = MagicMock()
        mock_products_query = MagicMock()
        mock_products_query.execute.return_value.data = [{"id": 1}, {"id": 2}]
        mock_products_query.select.return_value = mock_products_query
        mock_products_query.eq.return_value = mock_products_query
        
        mock_sub_query = MagicMock()
        mock_sub_query.execute.return_value.data = {
            "trial_end": None,
            "current_period_end": "2026-03-09T00:00:00Z",
            "trial_consumed_at": None,
            "paystack_subscription_code": "SUB_test123",
        }
        mock_sub_query.select.return_value = mock_sub_query
        mock_sub_query.eq.return_value = mock_sub_query
        mock_sub_query.single.return_value = mock_sub_query
        
        def table_router(table_name):
            if table_name == "products":
                return mock_products_query
            elif table_name == "subscriptions":
                return mock_sub_query
            return MagicMock()
        
        mock_supabase.table = table_router
        mock_get_supabase.return_value = mock_supabase
        
        info = get_plan_info("test-store-id")
        
        assert info["plan"] == "pro"
        assert info["is_active"] == True
        assert info["is_on_trial"] == False
        assert "limits" in info
        assert "usage" in info
        assert info["usage"]["products"] == 2
        assert info["limits"]["csv_export"] == True
        assert info["trial_consumed"] is False
