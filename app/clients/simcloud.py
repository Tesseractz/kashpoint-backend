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
