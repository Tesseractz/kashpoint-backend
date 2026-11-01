"""
Pytest configuration and fixtures for testing KashPoint API.
"""
import os
import sys

# CRITICAL: Clear DEV_PLAN_OVERRIDE BEFORE any imports to prevent .env from affecting tests
os.environ.pop("DEV_PLAN_OVERRIDE", None)
os.environ["DEV_PLAN_OVERRIDE"] = ""  # Set to empty string to override any .env value

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from typing import Generator

import pytest
from fastapi.testclient import TestClient

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set test environment variables BEFORE importing app modules
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"
os.environ["SUPABASE_JWT_SECRET"] = "test-jwt-secret-key-for-testing-only"
os.environ["PAYSTACK_SECRET_KEY_TEST"] = "sk_test_fake_paystack"
os.environ["PAYSTACK_MODE"] = "test"
os.environ["PASSWORD_ENCRYPTION_KEY"] = "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkw"


# Sample test data
@pytest.fixture
def sample_products():
    """Sample product data for testing."""
    return [
        {
            "id": 1,
            "sku": "PRD-001",
            "name": "Test Product 1",
            "price": 100.0,
            "quantity": 50,
            "cost_price": 60.0,
            "store_id": "test-store-id"
        },
        {
            "id": 2,
            "sku": "PRD-002",
            "name": "Test Product 2",
            "price": 200.0,
            "quantity": 25,
            "cost_price": 120.0,
            "store_id": "test-store-id"
        },
        {
            "id": 3,
            "sku": "PRD-003",
            "name": "Low Stock Product",
            "price": 50.0,
            "quantity": 5,
            "cost_price": 30.0,
            "store_id": "test-store-id"
        },
    ]


@pytest.fixture
def sample_sales():
    """Sample sales data for testing."""
    now = datetime.now(timezone.utc)
    return [
        {
            "id": 1,
            "product_id": 1,
            "quantity_sold": 2,
            "total_price": 200.0,
            "timestamp": now.isoformat(),
            "store_id": "test-store-id",
            "sold_by": "test-user-id"
        },
        {
            "id": 2,
            "product_id": 2,
            "quantity_sold": 1,
            "total_price": 200.0,
            "timestamp": (now - timedelta(hours=3)).isoformat(),
            "store_id": "test-store-id",
            "sold_by": "test-user-id"
        },
        {
            "id": 3,
            "product_id": 1,
            "quantity_sold": 3,
            "total_price": 300.0,
            "timestamp": (now - timedelta(days=1)).isoformat(),
            "store_id": "test-store-id",
            "sold_by": "test-user-id"
        },
    ]


@pytest.fixture
def sample_subscription_free():
    """Sample free plan subscription."""
    return {
        "store_id": "test-store-id",
        "plan": "free",
        "status": "active",
        "trial_end": None,
        "billing_provider": "paystack",
        "paystack_customer_code": None,
        "paystack_subscription_code": None,
    }


@pytest.fixture
def sample_subscription_pro():
    """Sample pro plan subscription."""
    return {
        "store_id": "test-store-id",
        "plan": "pro",
        "status": "active",
        "trial_end": None,
        "billing_provider": "paystack",
        "paystack_customer_code": "CUS_test123",
        "paystack_subscription_code": "SUB_test123",
    }


@pytest.fixture
def sample_subscription_trial():
    """Sample trial subscription."""
    trial_end = datetime.now(timezone.utc) + timedelta(days=15)
    return {
        "store_id": "test-store-id",
        "plan": "pro",
        "status": "trialing",
        "trial_end": trial_end.isoformat(),
        "billing_provider": "paystack",
        "paystack_customer_code": "CUS_test123",
        "paystack_subscription_code": "SUB_test123",
    }


@pytest.fixture
def sample_subscription_expired_trial():
    """Sample expired trial subscription."""
    trial_end = datetime.now(timezone.utc) - timedelta(days=5)
    return {
        "store_id": "test-store-id",
        "plan": "pro",
        "status": "trialing",
        "trial_end": trial_end.isoformat(),
        "billing_provider": "paystack",
        "paystack_customer_code": "CUS_test123",
        "paystack_subscription_code": "SUB_test123",
    }


@pytest.fixture
def sample_subscription_business():
    """Sample business plan subscription (mapped to pro)."""
    return {
        "store_id": "test-store-id",
        "plan": "business",
        "status": "active",
        "trial_end": None,
        "billing_provider": "paystack",
        "paystack_customer_code": "CUS_test456",
        "paystack_subscription_code": "SUB_test456",
    }


@pytest.fixture
def mock_supabase():
    """Create a mock Supabase client."""
    mock_client = MagicMock()
    
    # Create a helper to build chainable query mocks
    def create_query_mock(data=None, error=None, count=0):
        query = MagicMock()
        query.select = MagicMock(return_value=query)
        query.insert = MagicMock(return_value=query)
        query.update = MagicMock(return_value=query)
        query.delete = MagicMock(return_value=query)
        query.upsert = MagicMock(return_value=query)
        query.eq = MagicMock(return_value=query)
        query.neq = MagicMock(return_value=query)
        query.gte = MagicMock(return_value=query)
        query.lte = MagicMock(return_value=query)
        query.lt = MagicMock(return_value=query)
        query.gt = MagicMock(return_value=query)
        query.order = MagicMock(return_value=query)
        query.limit = MagicMock(return_value=query)
        query.range = MagicMock(return_value=query)
        query.single = MagicMock(return_value=query)
        query.or_ = MagicMock(return_value=query)
        query.ilike = MagicMock(return_value=query)
        
        # Execute returns the data
        execute_result = MagicMock()
        execute_result.data = data
        execute_result.count = count
        query.execute = MagicMock(return_value=execute_result)
        
        return query
    
    mock_client._create_query_mock = create_query_mock
    mock_client.table = MagicMock(return_value=create_query_mock([]))
    mock_client.rpc = MagicMock(return_value=create_query_mock([]))
    
    return mock_client


@pytest.fixture
def mock_request_context():
    """Create a mock request context."""
    from app.api.deps import RequestContext
    return RequestContext(
        user_id="test-user-id",
        store_id="test-store-id",
        role="admin"
    )


@pytest.fixture
def mock_request_context_cashier():
    """Create a mock request context for cashier."""
    from app.api.deps import RequestContext
    return RequestContext(
        user_id="test-cashier-id",
        store_id="test-store-id",
        role="cashier"
    )


class MockTableQuery:
    """Helper class for creating mock table queries with proper chaining."""
    
    def __init__(self, data=None, count=0):
        self.data = data or []
        self._count = count
        
    def select(self, *args, **kwargs):
        return self
    
    def insert(self, *args, **kwargs):
        return self
    
    def update(self, *args, **kwargs):
        return self
    
    def delete(self, *args, **kwargs):
        return self
    
    def upsert(self, *args, **kwargs):
        return self
    
    def eq(self, *args, **kwargs):
        return self
    
    def neq(self, *args, **kwargs):
        return self
    
    def gte(self, *args, **kwargs):
        return self
    
    def lte(self, *args, **kwargs):
        return self
    
    def lt(self, *args, **kwargs):
        return self
    
    def gt(self, *args, **kwargs):
        return self
    
    def order(self, *args, **kwargs):
        return self
    
    def limit(self, *args, **kwargs):
        return self
    
    def range(self, *args, **kwargs):
        return self
    
    def single(self):
        return self
    
    def or_(self, *args, **kwargs):
        return self
    
    def ilike(self, *args, **kwargs):
        return self
    
    def execute(self):
        result = MagicMock()
        result.data = self.data
        result.count = self._count
        return result


@pytest.fixture
def mock_table_query():
    """Factory for creating mock table queries."""
    return MockTableQuery
