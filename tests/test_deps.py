"""
Tests for dependency injection module (deps.py).
Covers request context creation. The store/profile/subscription setup itself
is covered in test_signup_resilience.py against an in-memory Supabase stand-in.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set environment variables
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"
os.environ["SUPABASE_JWT_SECRET"] = "test-jwt-secret-key-for-testing"


class TestRequestContext:
    """Tests for RequestContext class."""

    def test_creates_context_with_correct_attributes(self):
        """Test that RequestContext stores attributes correctly."""
        from app.api.deps import RequestContext

        ctx = RequestContext(
            user_id="user-123",
            store_id="store-456",
            role="admin"
        )

        assert ctx.user_id == "user-123"
        assert ctx.store_id == "store-456"
        assert ctx.role == "admin"


class TestGetCurrentContext:
    """Tests for get_current_context function."""

    def test_raises_on_missing_token(self):
        """Test that missing authorization header raises 401."""
        from app.api.deps import get_current_context

        with pytest.raises(HTTPException) as exc:
            get_current_context(None)

        assert exc.value.status_code == 401
        assert "Missing token" in exc.value.detail

    def test_raises_on_invalid_token_format(self):
        """Test that invalid token format raises 401."""
        from app.api.deps import get_current_context

        with pytest.raises(HTTPException) as exc:
            get_current_context("InvalidToken")

        assert exc.value.status_code == 401

    @patch('app.api.deps.get_supabase_client')
    @patch('app.api.deps.verify_supabase_jwt')
    def test_returns_context_for_existing_profile(self, mock_verify, mock_supabase):
        """Test that context is returned for existing profile."""
        from app.api.deps import get_current_context

        mock_verify.return_value = {"sub": "user-123"}

        mock_query = MagicMock()
        mock_query.execute.return_value.data = [{
            "id": "user-123",
            "store_id": "store-456",
            "role": "admin"
        }]
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query

        ctx = get_current_context("Bearer valid-token")

        assert ctx.user_id == "user-123"
        assert ctx.store_id == "store-456"
        assert ctx.role == "admin"

    @patch('app.api.deps.get_supabase_client')
    @patch('app.api.deps.verify_supabase_jwt')
    def test_raises_on_missing_store_id(self, mock_verify, mock_supabase):
        """Test that missing store_id raises 500."""
        from app.api.deps import get_current_context

        mock_verify.return_value = {"sub": "user-123"}

        mock_query = MagicMock()
        mock_query.execute.return_value.data = [{
            "id": "user-123",
            "store_id": None,
            "role": "admin"
        }]
        mock_query.select.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_supabase.return_value.table.return_value = mock_query

        with pytest.raises(HTTPException) as exc:
            get_current_context("Bearer valid-token")

        assert exc.value.status_code == 500
        assert "missing store_id" in exc.value.detail

    @patch('app.api.deps.get_supabase_client')
    @patch('app.api.deps.verify_supabase_jwt')
    def test_raises_on_invalid_token(self, mock_verify, mock_supabase):
        """Test that invalid JWT raises 401."""
        from app.api.deps import get_current_context

        mock_verify.side_effect = Exception("Invalid token")

        with pytest.raises(HTTPException) as exc:
            get_current_context("Bearer invalid-token")

        assert exc.value.status_code == 401

    @patch('app.api.deps.get_supabase_client')
    @patch('app.api.deps.verify_supabase_jwt')
    def test_handles_duplicate_key_error_on_profile_creation(self, mock_verify, mock_supabase):
        """Test that duplicate key error is handled gracefully."""
        from app.api.deps import get_current_context, _create_store_and_profile

        mock_verify.return_value = {
            "sub": "user-123",
            "user_metadata": {"store_name": "Test Store"},
            "email": "test@example.com"
        }

        # First query returns no profile
        mock_empty_query = MagicMock()
        mock_empty_query.execute.return_value.data = []
        mock_empty_query.select.return_value = mock_empty_query
        mock_empty_query.eq.return_value = mock_empty_query

        # Second query returns existing profile (after race condition)
        mock_existing_query = MagicMock()
        mock_existing_query.execute.return_value.data = [{
            "id": "user-123",
            "store_id": "store-456",
            "role": "admin"
        }]
        mock_existing_query.select.return_value = mock_existing_query
        mock_existing_query.eq.return_value = mock_existing_query

        call_count = [0]

        def table_router(table_name):
            if table_name == "profiles":
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock_empty_query
                return mock_existing_query
            return MagicMock()

        mock_supabase.return_value.table = table_router

        with patch('app.api.deps._create_store_and_profile') as mock_create:
            # Simulate duplicate key error
            mock_create.side_effect = Exception("23505 duplicate key violation")

            ctx = get_current_context("Bearer valid-token")

        assert ctx.user_id == "user-123"
        assert ctx.store_id == "store-456"
