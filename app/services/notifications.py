"""
Email notifications: receipts, low-stock alerts and the daily summary.

Sending itself lives in app/services/mailer.py (the business's own SMTP
mailbox first, Brevo as a fallback); this module shapes the messages.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel

from app.services import mailer, receipts
from app.services.mailer import SendResult as NotificationResult  # noqa: F401  (re-export)
from app.services.store_profile import empty_profile


class NotificationType(str, Enum):
    LOW_STOCK = "low_stock"
    RECEIPT = "receipt"


class NotificationConfig(BaseModel):
    email_enabled: bool = False
    low_stock_threshold: int = 10
    notification_email: Optional[str] = None


class ReceiptRequest(BaseModel):
    # One sale at the till is several `sales` rows (one per product). Older
    # clients send only the first one as sale_id; newer ones send them all.
    sale_id: Optional[int] = None
    sale_ids: Optional[List[int]] = None
    customer_email: Optional[str] = None
    send_email: bool = False
    # Payment info for receipt
    payment_method: Optional[str] = "cash"  # "cash" or "card"
    payment_amount: Optional[float] = None  # Amount tendered
    change_amount: Optional[float] = None   # Change given
    # The customer attached at checkout (sales rows don't carry it).
    customer_id: Optional[str] = None

    def all_sale_ids(self) -> List[int]:
        ids = list(self.sale_ids or [])
        if self.sale_id is not None and self.sale_id not in ids:
            ids.insert(0, self.sale_id)
        return ids


def is_email_configured() -> bool:
    return mailer.is_email_configured()


def email_transport() -> Optional[str]:
    return mailer.transport()


def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    *,
    text_body: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> NotificationResult:
    return mailer.send_email(
        to_email, subject, html_body, text_body=text_body, from_name=from_name, reply_to=reply_to
    )


def generate_receipt_html(sale_data: dict, store_name: str = "KashPoint") -> str:
    """Receipt email from the older flat sale shape (kept for callers and
    tests that still build it): same renderer as the full receipt."""
    total = float(sale_data.get("total", 0) or 0)
    method = (sale_data.get("payment_method") or "cash").lower()
    tendered = float(sale_data.get("payment_amount") or total)
    change = float(sale_data.get("change") or 0)
    store = empty_profile("")
    store["name"] = store_name
    receipt = {
        "number": sale_data.get("id", "N/A"),
        "title": "RECEIPT",
        "store": store,
        "timestamp": sale_data.get("timestamp", ""),
        "date_label": receipts.format_sast(sale_data.get("timestamp", "")),
        "cashier": None,
        "customer": None,
        "items": [
            {
                "name": it.get("name", "Product"),
                "sku": None,
                "quantity": it.get("quantity", 1),
                "unit_price": float(it.get("price", 0) or 0),
                "total": float(it.get("total", 0) or 0),
            }
            for it in sale_data.get("items", [])
        ],
        "subtotal": total,
        "discount": 0.0,
        "total": total,
        "vat": None,
        "payment": {"method": method, "tendered": tendered, "change": change},
    }
    return receipts.receipt_html(receipt)


def generate_low_stock_email(products: List[dict], store_name: str = "KashPoint") -> tuple:
    """Generate low stock alert email. Returns (subject, html_body)."""
    items_html = ""
    for p in products:
        qty = p.get('quantity', 0)
        qty_color = "#dc2626" if qty <= 5 else "#f59e0b"
        items_html += f"""
        <tr>
            <td style="padding: 12px 8px; border-bottom: 1px solid #e2e8f0;">{p.get('name', 'Unknown')}</td>
            <td style="padding: 12px 8px; border-bottom: 1px solid #e2e8f0; color: #64748b;">{p.get('sku', 'N/A')}</td>
            <td style="padding: 12px 8px; border-bottom: 1px solid #e2e8f0; text-align: center;">
                <span style="background-color: {qty_color}; color: white; padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 13px;">{qty}</span>
            </td>
        </tr>
        """

    subject = f"⚠️ [{store_name}] Low Stock Alert - {len(products)} products need attention"

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Low Stock Alert</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f8fafc;">
        <div style="background-color: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden;">
            <!-- Alert Header -->
            <div style="background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); padding: 30px; text-align: center;">
                <div style="font-size: 40px; margin-bottom: 10px;">⚠️</div>
                <h1 style="color: white; margin: 0; font-size: 22px;">Low Stock Alert</h1>
                <p style="color: rgba(255,255,255,0.9); margin: 8px 0 0 0;">{len(products)} product(s) need restocking</p>
        </div>

            <!-- Products Table -->
            <div style="padding: 20px;">
                <table style="width: 100%; border-collapse: collapse;">
            <thead>
                <tr style="background-color: #f8fafc;">
                            <th style="padding: 12px 8px; text-align: left; font-size: 12px; text-transform: uppercase; color: #64748b; border-bottom: 2px solid #e2e8f0;">Product</th>
                            <th style="padding: 12px 8px; text-align: left; font-size: 12px; text-transform: uppercase; color: #64748b; border-bottom: 2px solid #e2e8f0;">Product Code</th>
                            <th style="padding: 12px 8px; text-align: center; font-size: 12px; text-transform: uppercase; color: #64748b; border-bottom: 2px solid #e2e8f0;">Stock</th>
                </tr>
            </thead>
            <tbody>
                {items_html}
            </tbody>
        </table>
            </div>

            <!-- Action -->
            <div style="padding: 20px; background-color: #fef2f2; border-top: 1px solid #fecaca;">
                <p style="color: #991b1b; margin: 0; text-align: center;">
            Please restock these items soon to avoid running out.
        </p>
            </div>

            <!-- Footer -->
            <div style="padding: 15px; text-align: center; border-top: 1px solid #e2e8f0;">
                <p style="color: #94a3b8; font-size: 12px; margin: 0;">Sent by KashPoint</p>
            </div>
        </div>
    </body>
    </html>
    """

    return subject, html_body


def generate_daily_summary_email(summary: dict, store_name: str = "KashPoint") -> tuple:
    """Generate daily finance summary email. Returns (subject, html_body)."""
    date_label = summary.get("date_label", "Today")
    totals = summary.get("totals", {})
    sales_count = totals.get("total_sales_count", 0)
    revenue = float(totals.get("total_revenue", 0))
    profit = float(totals.get("total_profit", 0))

    subject = f"📊 [{store_name}] Daily Summary - {date_label}"

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daily Summary</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f8fafc;">
        <div style="background-color: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden;">
            <div style="background: linear-gradient(135deg, #0ea5e9 0%, #2563eb 100%); padding: 30px; text-align: center;">
                <div style="font-size: 40px; margin-bottom: 10px;">📊</div>
                <h1 style="color: white; margin: 0; font-size: 22px;">Daily Finance Summary</h1>
                <p style="color: rgba(255,255,255,0.9); margin: 8px 0 0 0;">{date_label}</p>
            </div>
            <div style="padding: 20px;">
                <div style="display: flex; flex-direction: column; gap: 12px;">
                    <div style="display: flex; justify-content: space-between; background: #f8fafc; padding: 12px 16px; border-radius: 10px;">
                        <span style="color: #64748b;">Sales Count</span>
                        <strong style="color: #0f172a;">{sales_count}</strong>
                    </div>
                    <div style="display: flex; justify-content: space-between; background: #f8fafc; padding: 12px 16px; border-radius: 10px;">
                        <span style="color: #64748b;">Total Revenue</span>
                        <strong style="color: #0f172a;">R {revenue:.2f}</strong>
                    </div>
                    <div style="display: flex; justify-content: space-between; background: #f8fafc; padding: 12px 16px; border-radius: 10px;">
                        <span style="color: #64748b;">Total Profit</span>
                        <strong style="color: #0f172a;">R {profit:.2f}</strong>
                    </div>
                </div>
            </div>
            <div style="padding: 15px; text-align: center; border-top: 1px solid #e2e8f0;">
                <p style="color: #94a3b8; font-size: 12px; margin: 0;">Sent by KashPoint</p>
            </div>
        </div>
    </body>
    </html>
    """

    return subject, html_body
