"""
Tests for the Supabase client module (app/db/supabase.py).
Covers client initialization, the per-thread client, and the transient retry.
"""
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestGetSupabaseClient:
    """Tests for get_supabase_client function."""

    def test_returns_client(self):
        """Test that a Supabase client is returned."""
        import app.db.supabase as sb_module

        sb_module.reset_supabase_client()

        original_url = os.environ.get("SUPABASE_URL")
        original_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-key"

        with patch('app.db.supabase.create_client') as mock_create:
            mock_client = MagicMock()
            mock_create.return_value = mock_client

            try:
                result = sb_module.get_supabase_client()

                assert result == mock_client
                mock_create.assert_called_once_with("https://test.supabase.co", "test-service-key")
            finally:
                if original_url:
                    os.environ["SUPABASE_URL"] = original_url
                else:
                    os.environ.pop("SUPABASE_URL", None)
                if original_key:
                    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = original_key
                else:
                    os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

                sb_module.reset_supabase_client()

    def test_same_thread_reuses_its_client(self):
        """Within one thread, subsequent calls return the same instance."""
        import app.db.supabase as sb_module

        mock_client = MagicMock()
        sb_module._local.client = mock_client

        result1 = sb_module.get_supabase_client()
        result2 = sb_module.get_supabase_client()

        assert result1 is result2
        assert result1 is mock_client

        sb_module.reset_supabase_client()

    def test_each_thread_gets_its_own_client(self):
        """Sync endpoints run on a thread pool; sharing one HTTP/2 connection
        between threads is what raised "[Errno 11] Resource temporarily
        unavailable" under a first-load burst, so clients are per thread."""
        import app.db.supabase as sb_module

        with patch("app.db.supabase.create_client", side_effect=lambda *a, **k: MagicMock()), \
                patch("app.db.supabase.load_dotenv"), \
                patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co",
                                        "SUPABASE_SERVICE_ROLE_KEY": "test-service-key"}):
            sb_module.reset_supabase_client()
            try:
                here = sb_module.get_supabase_client()
                assert sb_module.get_supabase_client() is here

                seen = []
                worker = threading.Thread(target=lambda: seen.append(sb_module.get_supabase_client()))
                worker.start()
                worker.join()
                assert seen and seen[0] is not here
            finally:
                sb_module.reset_supabase_client()

    def test_raises_on_missing_url(self):
        """Test that an error is raised when SUPABASE_URL is missing."""
        import app.db.supabase as sb_module

        original_url = os.environ.get("SUPABASE_URL")
        original_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

        os.environ.pop("SUPABASE_URL", None)
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"

        try:
            # Mock both create_client and load_dotenv to prevent .env from overriding our test env
            with patch('app.db.supabase.create_client') as mock_create, \
                 patch('app.db.supabase.load_dotenv'):
                sb_module.reset_supabase_client()

                # The error should be raised BEFORE create_client is called
                with pytest.raises(RuntimeError) as exc_info:
                    sb_module.get_supabase_client()

                # Verify error message
                assert "SUPABASE_URL" in str(exc_info.value) or "Missing" in str(exc_info.value)

                # create_client should NOT have been called
                mock_create.assert_not_called()
        finally:
            if original_url:
                os.environ["SUPABASE_URL"] = original_url
            if original_key:
                os.environ["SUPABASE_SERVICE_ROLE_KEY"] = original_key
            else:
                os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
            sb_module.reset_supabase_client()

    def test_raises_on_missing_key(self):
        """Test that an error is raised when SUPABASE_SERVICE_ROLE_KEY is missing."""
        import app.db.supabase as sb_module

        original_url = os.environ.get("SUPABASE_URL")
        original_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        try:
            # Mock both create_client and load_dotenv to prevent .env from overriding our test env
            with patch('app.db.supabase.create_client') as mock_create, \
                 patch('app.db.supabase.load_dotenv'):
                sb_module.reset_supabase_client()

                # The error should be raised BEFORE create_client is called
                with pytest.raises(RuntimeError) as exc_info:
                    sb_module.get_supabase_client()

                # Verify error message
                assert "SUPABASE_SERVICE_ROLE_KEY" in str(exc_info.value) or "Missing" in str(exc_info.value)

                # create_client should NOT have been called
                mock_create.assert_not_called()
        finally:
            if original_url:
                os.environ["SUPABASE_URL"] = original_url
            else:
                os.environ.pop("SUPABASE_URL", None)
            if original_key:
                os.environ["SUPABASE_SERVICE_ROLE_KEY"] = original_key
            sb_module.reset_supabase_client()


class TestRetryTransient:
    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        import app.db.supabase as sb_module
        monkeypatch.setattr(sb_module, "RETRY_BASE_DELAY", 0)

    def test_retries_the_socket_error_seen_in_production(self):
        from app.db.supabase import retry_transient
        op = MagicMock(side_effect=[OSError(11, "Resource temporarily unavailable"), "ok"])
        assert retry_transient(op) == "ok"
        assert op.call_count == 2

    def test_gives_up_after_the_last_attempt(self):
        import app.db.supabase as sb_module
        op = MagicMock(side_effect=TimeoutError("timed out"))
        with pytest.raises(TimeoutError):
            sb_module.retry_transient(op)
        assert op.call_count == sb_module.RETRY_ATTEMPTS

    def test_real_errors_are_not_retried(self):
        from app.db.supabase import retry_transient
        op = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError):
            retry_transient(op)
        assert op.call_count == 1
