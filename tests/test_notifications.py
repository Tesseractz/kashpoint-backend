"""
Tests for notifications module (notifications.py).
Covers email sending, receipt generation, and alert emails.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    """These tests exercise the Brevo route; a developer's .env may configure
    SMTP, which would take precedence (and try to connect)."""
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_PORT", "SMTP_FROM", "SMTP_FROM_NAME"):
        monkeypatch.delenv(key, raising=False)


class TestIsEmailConfigured:
    """Tests for is_email_configured function."""
    
    def test_returns_false_when_not_configured(self):
        """Test that function returns False when BREVO_API_KEY is not set."""
        from app.services.notifications import is_email_configured
        
        original = os.environ.get("BREVO_API_KEY")
        os.environ.pop("BREVO_API_KEY", None)
        
        try:
            result = is_email_configured()
            assert result is False
        finally:
            if original:
                os.environ["BREVO_API_KEY"] = original
    
    def test_returns_true_when_configured(self):
        """Test that function returns True when BREVO_API_KEY is set."""
        from app.services.notifications import is_email_configured
        
        original = os.environ.get("BREVO_API_KEY")
        os.environ["BREVO_API_KEY"] = "test-api-key"
        
        try:
            result = is_email_configured()
            assert result is True
        finally:
            if original:
                os.environ["BREVO_API_KEY"] = original
            else:
                os.environ.pop("BREVO_API_KEY", None)


class TestSendEmail:
    """Tests for send_email function."""
    
    def test_returns_error_when_api_key_missing(self):
        """Test that error is returned when API key is not set."""
        from app.services.notifications import send_email
        
        original = os.environ.get("BREVO_API_KEY")
        os.environ.pop("BREVO_API_KEY", None)
        
        try:
            result = send_email("test@example.com", "Test Subject", "<p>Test</p>")
            
            assert result.success is False
            assert "not configured" in result.message
        finally:
            if original:
                os.environ["BREVO_API_KEY"] = original
    
    def test_returns_error_when_sender_email_missing(self):
        """Test that error is returned when sender email is not set."""
        from app.services.notifications import send_email
        
        original_key = os.environ.get("BREVO_API_KEY")
        original_sender = os.environ.get("BREVO_SENDER_EMAIL")
        
        os.environ["BREVO_API_KEY"] = "test-api-key"
        os.environ.pop("BREVO_SENDER_EMAIL", None)
        
        try:
            result = send_email("test@example.com", "Test Subject", "<p>Test</p>")
            
            assert result.success is False
            assert "BREVO_SENDER_EMAIL" in result.message
        finally:
            if original_key:
                os.environ["BREVO_API_KEY"] = original_key
            else:
                os.environ.pop("BREVO_API_KEY", None)
            if original_sender:
                os.environ["BREVO_SENDER_EMAIL"] = original_sender
    
    @patch('httpx.post')
    def test_sends_email_successfully(self, mock_post):
        """Test successful email sending."""
        from app.services.notifications import send_email
        
        original_key = os.environ.get("BREVO_API_KEY")
        original_sender = os.environ.get("BREVO_SENDER_EMAIL")
        
        os.environ["BREVO_API_KEY"] = "test-api-key"
        os.environ["BREVO_SENDER_EMAIL"] = "sender@example.com"
        
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_post.return_value = mock_response
        
        try:
            result = send_email("recipient@example.com", "Test Subject", "<p>Hello</p>")
            
            assert result.success is True
            assert result.message == "Email sent successfully"
            assert result.recipient == "recipient@example.com"
        finally:
            if original_key:
                os.environ["BREVO_API_KEY"] = original_key
            else:
                os.environ.pop("BREVO_API_KEY", None)
            if original_sender:
                os.environ["BREVO_SENDER_EMAIL"] = original_sender
            else:
                os.environ.pop("BREVO_SENDER_EMAIL", None)
    
    @patch('httpx.post')
    def test_handles_api_error(self, mock_post):
        """Test handling of API error response."""
        from app.services.notifications import send_email
        
        original_key = os.environ.get("BREVO_API_KEY")
        original_sender = os.environ.get("BREVO_SENDER_EMAIL")
        
        os.environ["BREVO_API_KEY"] = "test-api-key"
        os.environ["BREVO_SENDER_EMAIL"] = "sender@example.com"
        
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.content = b'{"message": "Invalid request"}'
        mock_response.json.return_value = {"message": "Invalid request", "code": "invalid_request"}
        mock_post.return_value = mock_response
        
        try:
            result = send_email("recipient@example.com", "Test", "<p>Test</p>")
            
            assert result.success is False
            assert "Invalid request" in result.message
        finally:
            if original_key:
                os.environ["BREVO_API_KEY"] = original_key
            else:
                os.environ.pop("BREVO_API_KEY", None)
            if original_sender:
                os.environ["BREVO_SENDER_EMAIL"] = original_sender
            else:
                os.environ.pop("BREVO_SENDER_EMAIL", None)
    
    @patch('httpx.post')
    def test_handles_timeout(self, mock_post):
        """Test handling of timeout error."""
        from app.services.notifications import send_email
        import httpx
        
        original_key = os.environ.get("BREVO_API_KEY")
        original_sender = os.environ.get("BREVO_SENDER_EMAIL")
        
        os.environ["BREVO_API_KEY"] = "test-api-key"
        os.environ["BREVO_SENDER_EMAIL"] = "sender@example.com"
        
        mock_post.side_effect = httpx.TimeoutException("Connection timed out")
        
        try:
            result = send_email("recipient@example.com", "Test", "<p>Test</p>")
            
            assert result.success is False
            assert "timed out" in result.message.lower()
        finally:
            if original_key:
                os.environ["BREVO_API_KEY"] = original_key
            else:
                os.environ.pop("BREVO_API_KEY", None)
            if original_sender:
                os.environ["BREVO_SENDER_EMAIL"] = original_sender
            else:
                os.environ.pop("BREVO_SENDER_EMAIL", None)
    
    @patch('httpx.post')
    def test_handles_unauthorized_sender_error(self, mock_post):
        """Test handling of unauthorized sender error."""
        from app.services.notifications import send_email
        
        original_key = os.environ.get("BREVO_API_KEY")
        original_sender = os.environ.get("BREVO_SENDER_EMAIL")
        
        os.environ["BREVO_API_KEY"] = "test-api-key"
        os.environ["BREVO_SENDER_EMAIL"] = "unverified@example.com"
        
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.content = b'{"message": "sender not verified", "code": "unauthorized_sender"}'
        mock_response.json.return_value = {"message": "sender not verified", "code": "unauthorized_sender"}
        mock_post.return_value = mock_response
        
        try:
            result = send_email("recipient@example.com", "Test", "<p>Test</p>")
            
            assert result.success is False
            assert "not verified" in result.message.lower()
        finally:
            if original_key:
                os.environ["BREVO_API_KEY"] = original_key
            else:
                os.environ.pop("BREVO_API_KEY", None)
            if original_sender:
                os.environ["BREVO_SENDER_EMAIL"] = original_sender
            else:
                os.environ.pop("BREVO_SENDER_EMAIL", None)


class TestGenerateReceiptHtml:
    """Tests for generate_receipt_html function."""
    
    def test_generates_html_with_items(self):
        """Test that receipt HTML is generated correctly."""
        from app.services.notifications import generate_receipt_html
        
        sale_data = {
            "id": 123,
            "timestamp": "2026-02-09T10:30:00Z",
            "total": 250.00,
            "items": [
                {"name": "Product A", "quantity": 2, "price": 100.00, "total": 200.00},
                {"name": "Product B", "quantity": 1, "price": 50.00, "total": 50.00},
            ]
        }
        
        html = generate_receipt_html(sale_data, "Test Store")
        
        assert "Test Store" in html
        assert "Receipt #123" in html
        assert "Product A" in html
        assert "Product B" in html
        assert "R 250.00" in html
    
    def test_includes_cash_payment_info(self):
        """Test that cash payment info is included."""
        from app.services.notifications import generate_receipt_html
        
        sale_data = {
            "id": 456,
            "timestamp": "2026-02-09T10:30:00Z",
            "total": 100.00,
            "items": [{"name": "Item", "quantity": 1, "price": 100.00, "total": 100.00}],
            "payment_method": "cash",
            "payment_amount": 150.00,
            "change": 50.00
        }
        
        html = generate_receipt_html(sale_data)
        
        assert "Cash" in html
        assert "R 150.00" in html
        assert "R 50.00" in html
    
    def test_includes_card_payment_info(self):
        """Test that card payment info is included."""
        from app.services.notifications import generate_receipt_html
        
        sale_data = {
            "id": 789,
            "timestamp": "2026-02-09T10:30:00Z",
            "total": 200.00,
            "items": [{"name": "Item", "quantity": 1, "price": 200.00, "total": 200.00}],
            "payment_method": "card"
        }
        
        html = generate_receipt_html(sale_data)
        
        assert "Card" in html


class TestGenerateLowStockEmail:
    """Tests for generate_low_stock_email function."""
    
    def test_generates_alert_email(self):
        """Test that low stock alert email is generated correctly."""
        from app.services.notifications import generate_low_stock_email
        
        products = [
            {"name": "Low Item 1", "sku": "LI1", "quantity": 3},
            {"name": "Low Item 2", "sku": "LI2", "quantity": 5},
        ]
        
        subject, html = generate_low_stock_email(products, "My Store")
        
        assert "Low Stock Alert" in subject
        assert "My Store" in subject
        assert "2 products" in subject
        assert "Low Item 1" in html
        assert "Low Item 2" in html
        assert "3" in html
        assert "5" in html
    
    def test_handles_missing_sku(self):
        """Test that missing SKU is handled."""
        from app.services.notifications import generate_low_stock_email
        
        products = [
            {"name": "No SKU Item", "quantity": 2},
        ]
        
        subject, html = generate_low_stock_email(products)
        
        assert "No SKU Item" in html
        assert "N/A" in html


class TestGenerateDailySummaryEmail:
    """Tests for generate_daily_summary_email function."""
    
    def test_generates_summary_email(self):
        """Test that daily summary email is generated correctly."""
        from app.services.notifications import generate_daily_summary_email
        
        summary = {
            "date_label": "2026-02-09",
            "totals": {
                "total_sales_count": 15,
                "total_revenue": 5000.00,
                "total_profit": 2000.00
            }
        }
        
        subject, html = generate_daily_summary_email(summary, "Test Store")
        
        assert "Daily Summary" in subject
        assert "Test Store" in subject
        assert "2026-02-09" in subject
        assert "15" in html
        assert "R 5000.00" in html
        assert "R 2000.00" in html


class TestNotificationModels:
    """Tests for notification Pydantic models."""
    
    def test_notification_config_defaults(self):
        """Test NotificationConfig default values."""
        from app.services.notifications import NotificationConfig
        
        config = NotificationConfig()
        
        assert config.email_enabled is False
        assert config.low_stock_threshold == 10
        assert config.notification_email is None
    
    def test_receipt_request_defaults(self):
        """Test ReceiptRequest default values."""
        from app.services.notifications import ReceiptRequest
        
        request = ReceiptRequest(sale_id=123)
        
        assert request.sale_id == 123
        assert request.customer_email is None
        assert request.send_email is False
        assert request.payment_method == "cash"
    
    def test_notification_result_success(self):
        """Test NotificationResult creation."""
        from app.services.notifications import NotificationResult
        
        result = NotificationResult(
            success=True,
            message="Sent successfully",
            recipient="test@example.com"
        )
        
        assert result.success is True
        assert result.message == "Sent successfully"
        assert result.recipient == "test@example.com"
