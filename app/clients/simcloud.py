"""SIMcloud (simcloud.co.za): airtime, data and vouchers.

Every shop that sells airtime has its own SIMcloud account, created from here
as a child of KashPoint's account, with its own wallet and its own API token.
A sale is paid from the wallet whose token is sent, so KashPoint's parent
account never funds a shop's stock.

SIMcloud has no sandbox: every order placed here is real. Balance, product
lists and order look-ups are the only calls that cost nothing.
"""
import os
import re
from typing import Optional

import httpx

BASE_URL = "https://simcloud.co.za/api"
TIMEOUT = 30

AIRTIME_NETWORKS = ("mtn", "vodacom", "cellc", "telkom")
AIRTIME_MIN = 2
AIRTIME_MAX = 999


class SimcloudError(Exception):
    """SIMcloud refused or could not complete a request."""

    def __init__(self, message: str, status: int = 0, body: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


def parent_token() -> str:
    token = os.getenv("SIMCLOUD_API_TOKEN", "").strip()
    if not token:
        raise SimcloudError("Airtime is not set up on this server yet (SIMCLOUD_API_TOKEN is missing).")
    return token


def normalize_cell(value) -> Optional[str]:
    """A South African mobile number as SIMcloud wants it: 10 digits from 0."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("27") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    return None


def _request(method: str, path: str, token: str, *, params: Optional[dict] = None, json: Optional[dict] = None) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.request(
                method,
                f"{BASE_URL}/{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                json=json,
            )
    except httpx.HTTPError as e:
        raise SimcloudError(f"Could not reach SIMcloud: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        message = body.get("message") or body.get("error") or f"SIMcloud answered HTTP {resp.status_code}."
        raise SimcloudError(str(message), status=resp.status_code, body=body)
    return body if isinstance(body, dict) else {"data": body}


def balance(token: str) -> dict:
    """The wallet's last known balance: {balance, updated_at, ...}."""
    return _request("GET", "balance.php", token)


def register_child(*, first_name: str, last_name: str, cell: str, email: str, password: str, business_name: Optional[str] = None) -> dict:
    """Create a shop's own SIMcloud account under KashPoint's.

    Returns {user_id, username, api_token, existing}. Safe to repeat: the same
    cell under the same parent returns the existing account.
    """
    payload = {"first_name": first_name, "last_name": last_name, "cell": cell, "email": email, "password": password}
    if business_name:
        payload["business_name"] = business_name
    return _request("POST", "register.php", parent_token(), json=payload)


def order_airtime(token: str, *, msisdn: str, network: str, amount: int, reference: str) -> dict:
    """Send airtime straight to a phone. Returns {request_id, queue_status}.

    SIMcloud refuses the same number and amount twice within 5 minutes (409),
    which is also what protects a double-tapped Sell button.
    """
    return _request(
        "POST", "airtime.php", token,
        json={"msisdn": msisdn, "network": network, "amount": amount, "reference": reference},
    )


def airtime_status(token: str, request_id) -> dict:
    """queued | pending | delivered | failed."""
    return _request("GET", "airtime.php", token, params={"request_id": request_id})


def lookup_network(token: str, msisdn: str) -> Optional[str]:
    """Which network a number is on now (ported numbers included), as one of
    AIRTIME_NETWORKS, or None when SIMcloud doesn't know."""
    res = _request("GET", "network.php", token, params={"msisdn": msisdn})
    name = re.sub(r"[^a-z]", "", str(res.get("network") or "").lower())
    return name if name in AIRTIME_NETWORKS else None


def data_products(token: str, network: Optional[str] = None) -> list:
    params = {"products": 1}
    if network:
        params["network"] = network
    return _request("GET", "data.php", token, params=params).get("products") or []


def order_data(token: str, *, msisdn: str, network: str, amount, reference: str) -> dict:
    return _request(
        "POST", "data.php", token,
        json={"msisdn": msisdn, "network": network, "amount": amount, "reference": reference},
    )


def data_status(token: str, request_id) -> dict:
    return _request("GET", "data.php", token, params={"request_id": request_id})


# --- Prepaid electricity ----------------------------------------------------

ELECTRICITY_MIN = 55
ELECTRICITY_MAX = 1000


def electricity_fee(amount: int) -> int:
    """SIMcloud's own fee, taken out of the amount: 3/103 of it, capped at
    R10, rounded down to a whole rand. R150 buys R146 of electricity."""
    return min(10, int(amount * 3 / 103))


def electricity_meter(token: str, meter_number: str) -> dict:
    """{status: success|unsupported|unknown, supported, provider}. Free."""
    return _request("GET", "electricity.php", token, params={"meter_number": meter_number})


def order_electricity(token: str, *, meter_number: str, amount: int, client_reference: str, recipient: str) -> dict:
    """Buy a token. `client_reference` makes it safe to repeat: the same
    reference returns the first result and never buys twice. May answer
    'pending' (HTTP 202): then poll, never re-buy with a new reference."""
    return _request(
        "POST", "electricity.php", token,
        json={"meter_number": meter_number, "amount": amount, "client_reference": client_reference, "recipient": recipient, "send_sms": True},
    )


def electricity_status(token: str, client_reference: str) -> dict:
    return _request("GET", "electricity.php", token, params={"client_reference": client_reference})


# --- Gift vouchers ----------------------------------------------------------

def voucher_catalog(token: str) -> list:
    return _request("GET", "vas.php", token).get("products") or []


def order_voucher(token: str, *, product_id: int, amount, reference: str) -> dict:
    return _request("POST", "vas.php", token, json={"product_id": product_id, "amount": amount, "reference": reference})


def voucher_status(token: str, order_id) -> dict:
    return _request("GET", "vas.php", token, params={"order_id": order_id})
