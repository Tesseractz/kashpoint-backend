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

Render's free instances cannot open the mail ports at all (25, 465, 587 are
blocked outbound), so when the mail server cannot be reached the same
message goes over HTTPS to a small relay on the web host next to the
mailbox (frontend/public/mail/relay.php), which speaks SMTP from there. The
relay authenticates with the same mailbox login; nothing extra to configure.
"""
import os
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

import httpx
from pydantic import BaseModel

DEFAULT_RELAY_URL = "https://app.kashpoint.co.za/mail/relay.php"
# Seconds to wait for the mail server before deciding this host can't reach
# it. A blocked port drops packets rather than refusing, so this is what a
# Render instance actually waits.
DIRECT_CONNECT_TIMEOUT = 8
# Once a direct connection has timed out, this process goes straight to the
# relay: the block does not come and go.
_direct_smtp_blocked = False


def relay_url() -> str:
    value = os.getenv("MAIL_RELAY_URL")
    if value is None:
        return DEFAULT_RELAY_URL
    return value.strip()  # set it empty to disable the relay


def reset_transport_state() -> None:
    global _direct_smtp_blocked
    _direct_smtp_blocked = False


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
    global _direct_smtp_blocked
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    try:
        port = int(os.getenv("SMTP_PORT") or 465)
    except ValueError:
        port = 465
    from_email = (os.getenv("SMTP_FROM") or user).strip()
    display = (from_name or os.getenv("SMTP_FROM_NAME") or "KashPoint").strip()
    text = text_body or html_to_text(html_body)

    if not _direct_smtp_blocked:
        msg = EmailMessage()
        msg["From"] = formataddr((display, from_email))
        msg["To"] = to_email
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=from_email.rsplit("@", 1)[-1] or None)
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")

        try:
            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=DIRECT_CONNECT_TIMEOUT, context=ssl.create_default_context()) as smtp:
                    smtp.login(user, password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=DIRECT_CONNECT_TIMEOUT) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.login(user, password)
                    smtp.send_message(msg)
            print(f"[mail] sent via SMTP to {to_email}: {subject}")
            return SendResult(success=True, message="Email sent successfully", recipient=to_email)
        except smtplib.SMTPAuthenticationError:
            print(f"[mail] SMTP login refused for {user} at {host}:{port}")
            return SendResult(success=False, message="Email login failed: check SMTP_USER and SMTP_PASSWORD.", recipient=to_email)
        except smtplib.SMTPRecipientsRefused:
            return SendResult(success=False, message=f"The mail server refused the address {to_email}.", recipient=to_email)
        except smtplib.SMTPException as e:
            # The server answered, and said no: the relay would hear the same.
            print(f"[mail] SMTP send to {to_email} failed: {e!r}")
            return SendResult(success=False, message=f"Email could not be sent: {e}", recipient=to_email)
        except (socket.timeout, TimeoutError, ConnectionError, socket.gaierror, OSError) as e:
            # Could not reach the mail server at all. On Render's free plan
            # the mail ports are blocked, so this is the normal path there.
            if not relay_url():
                print(f"[mail] cannot reach {host}:{port} ({e!r}) and no relay is configured")
                return SendResult(success=False, message=f"Email could not be sent: {e}", recipient=to_email)
            _direct_smtp_blocked = True
            print(f"[mail] cannot reach {host}:{port} ({e!r}); using the HTTPS relay from now on")

    return _send_via_relay(
        to_email, subject, html_body, text=text, display=display, reply_to=reply_to,
        host=host, port=port, user=user, password=password,
    )


def _send_via_relay(to_email, subject, html_body, *, text, display, reply_to, host, port, user, password) -> SendResult:
    url = relay_url()
    if not url:
        return SendResult(success=False, message="Email could not be sent: the mail server is unreachable and no relay is configured.", recipient=to_email)
    payload = {
        "user": user,
        "password": password,
        "host": host,
        "port": port,
        "to": to_email,
        "subject": subject,
        "html": html_body,
        "text": text,
        "from_name": display,
        "reply_to": reply_to or "",
    }
    # An ordinary form post: the web host answers 406 to requests sent as JSON.
    try:
        response = httpx.post(url, data=payload, timeout=40.0)
    except httpx.TimeoutException:
        return SendResult(success=False, message="Email sending timed out. Please try again.", recipient=to_email)
    except Exception as e:
        print(f"[mail] relay {url} unreachable: {e!r}")
        return SendResult(success=False, message="Email could not be sent: the mail relay is unreachable.", recipient=to_email)
    try:
        body = response.json() if response.content else {}
    except Exception:
        body = {}
    if response.status_code == 200 and body.get("ok"):
        print(f"[mail] sent via relay to {to_email}: {subject}")
        return SendResult(success=True, message="Email sent successfully", recipient=to_email)
    error = body.get("error") if isinstance(body, dict) else None
    print(f"[mail] relay refused ({response.status_code}): {error or response.text[:120]}")
    return SendResult(success=False, message=error or f"Email could not be sent (relay HTTP {response.status_code}).", recipient=to_email)


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
