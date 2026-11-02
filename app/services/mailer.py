"""The one place that sends email.

Transport 1 — SMTP through the business's own mailbox (SMTP_HOST, SMTP_USER,
SMTP_PASSWORD; port 465 is implicit TLS, anything else STARTTLS). For
KashPoint that is noreply@kashpoint.co.za on the cPanel host, the same route
Supabase sends signup mail through, so SPF, DKIM and DMARC all pass and no
third party sits in the middle.

Transport 2 — Brevo's HTTP API (BREVO_API_KEY + a verified BREVO_SENDER_EMAIL),
kept for installs that have no mailbox. Brevo refuses an unactivated account
with "API Key is not enabled", which is what receipts died of before.

Every message carries a Message-ID and a plain-text part: mail without them
is what spam filters are built to catch.
"""
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

import httpx
from pydantic import BaseModel


class SendResult(BaseModel):
    success: bool
    message: str
    recipient: Optional[str] = None


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def brevo_configured() -> bool:
    return bool(os.getenv("BREVO_API_KEY"))


def transport() -> Optional[str]:
    """Which route a message would take right now: 'smtp', 'brevo' or None."""
    if smtp_configured():
        return "smtp"
    if brevo_configured():
        return "brevo"
    return None


def is_email_configured() -> bool:
    return transport() is not None


def html_to_text(html: str) -> str:
    """A readable plain-text fallback for mail clients that want one."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h[1-6]|li)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "  ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    *,
    text_body: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> SendResult:
    to_email = (to_email or "").strip()
    if not to_email:
        return SendResult(success=False, message="No recipient address", recipient=to_email)
    if smtp_configured():
        return _send_smtp(to_email, subject, html_body, text_body=text_body, from_name=from_name, reply_to=reply_to)
    if brevo_configured():
        return _send_brevo(to_email, subject, html_body, text_body=text_body, from_name=from_name, reply_to=reply_to)
    return SendResult(
        success=False,
        message="Email not configured. Set SMTP_HOST, SMTP_USER and SMTP_PASSWORD (or BREVO_API_KEY).",
        recipient=to_email,
    )


def _send_smtp(to_email, subject, html_body, *, text_body, from_name, reply_to) -> SendResult:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    try:
        port = int(os.getenv("SMTP_PORT") or 465)
    except ValueError:
        port = 465
    from_email = (os.getenv("SMTP_FROM") or user).strip()
    display = (from_name or os.getenv("SMTP_FROM_NAME") or "KashPoint").strip()

    msg = EmailMessage()
    msg["From"] = formataddr((display, from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_email.rsplit("@", 1)[-1] or None)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text_body or html_to_text(html_body))
    msg.add_alternative(html_body, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=25, context=ssl.create_default_context()) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=25) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, password)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        print(f"[mail] SMTP login refused for {user} at {host}:{port}")
        return SendResult(success=False, message="Email login failed: check SMTP_USER and SMTP_PASSWORD.", recipient=to_email)
    except smtplib.SMTPRecipientsRefused:
        return SendResult(success=False, message=f"The mail server refused the address {to_email}.", recipient=to_email)
    except (smtplib.SMTPException, OSError) as e:
        print(f"[mail] SMTP send to {to_email} failed: {e!r}")
        return SendResult(success=False, message=f"Email could not be sent: {e}", recipient=to_email)
    print(f"[mail] sent via SMTP to {to_email}: {subject}")
    return SendResult(success=True, message="Email sent successfully", recipient=to_email)


def _send_brevo(to_email, subject, html_body, *, text_body, from_name, reply_to) -> SendResult:
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("BREVO_SENDER_EMAIL")
    sender_name = from_name or os.getenv("BREVO_SENDER_NAME", "KashPoint")
    if not sender_email:
        return SendResult(
            success=False,
            message="BREVO_SENDER_EMAIL not set. Add a verified sender email from your Brevo account.",
            recipient=to_email,
        )
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body or html_to_text(html_body),
    }
    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
            timeout=15.0,
        )
    except httpx.TimeoutException:
        return SendResult(success=False, message="Email sending timed out. Please try again.", recipient=to_email)
    except Exception as e:
        return SendResult(success=False, message=f"Failed to send email: {e}", recipient=to_email)

    print(f"[Brevo] Status: {response.status_code}, To: {to_email}")
    if response.status_code in (200, 201, 202):
        return SendResult(success=True, message="Email sent successfully", recipient=to_email)

    error_data = {}
    try:
        error_data = response.json() if response.content else {}
    except Exception:
        pass
    error_msg = error_data.get("message", f"HTTP {response.status_code}")
    error_code = error_data.get("code", "")
    if "sender" in error_msg.lower() or error_code == "unauthorized_sender":
        error_msg = f"Sender email not verified in Brevo. Verify '{sender_email}' at Settings > Senders & IP"
    elif "not enabled" in error_msg.lower():
        error_msg = "The Brevo account is not activated yet (API key not enabled). Activate it in Brevo, or set SMTP_* to send through your own mailbox."
    elif "unauthorized" in error_msg.lower():
        error_msg = "Invalid API key. Check your BREVO_API_KEY"
    print(f"[Brevo] Error: {error_msg} | Full response: {error_data}")
    return SendResult(success=False, message=error_msg, recipient=to_email)
