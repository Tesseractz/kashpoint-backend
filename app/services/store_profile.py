"""The business details printed on every receipt: name, logo, address,
contact details, VAT number and a footer line.

They live on the `stores` row. The columns arrive with migration
20261102130000_store_receipt_profile.sql; reading copes with a database that
hasn't had it yet (fields come back None), while writing reports it clearly.
"""
import re
from typing import Optional

from app.core.phone import normalize_phone

STORE_FIELDS = (
    "name",
    "logo_url",
    "address",
    "phone",
    "email",
    "vat_number",
    "registration_number",
    "receipt_footer",
    "website",
)

# Keeps a receipt tidy and the database out of trouble.
MAX_LENGTHS = {
    "name": 80,
    "logo_url": 500,
    "address": 200,
    "phone": 20,
    "email": 120,
    "vat_number": 20,
    "registration_number": 40,
    "receipt_footer": 160,
    "website": 120,
}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MissingColumns(Exception):
    """The stores table doesn't have the receipt columns yet."""


class InvalidField(Exception):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def empty_profile(store_id: str) -> dict:
    out = {k: None for k in STORE_FIELDS}
    out["id"] = store_id
    out["name"] = "Your Store"
    return out


def read_store(supa, store_id: str) -> dict:
    """The store's receipt profile; every field present, None when unset."""
    res = supa.table("stores").select("*").eq("id", store_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    row = rows[0] if rows else {}
    out = {k: (row.get(k) or None) for k in STORE_FIELDS}
    out["id"] = store_id
    out["name"] = out["name"] or "Your Store"
    return out


def clean_changes(changes: dict) -> dict:
    """Validate and normalise what the owner typed. Unknown keys are dropped,
    blanks become None (the field is cleared)."""
    out = {}
    for key, raw in changes.items():
        if key not in STORE_FIELDS:
            continue
        value = (str(raw).strip() if raw is not None else "") or None
        if value and len(value) > MAX_LENGTHS[key]:
            raise InvalidField(key, f"{key.replace('_', ' ').capitalize()} is too long (max {MAX_LENGTHS[key]} characters).")
        if key == "name" and not value:
            raise InvalidField(key, "Store name can't be empty.")
        if key == "phone" and value:
            normalised = normalize_phone(value)
            if not normalised:
                raise InvalidField(key, "Enter a valid phone number, e.g. 082 123 4567.")
            value = normalised
        if key == "email" and value and not _EMAIL.match(value):
            raise InvalidField(key, "Enter a valid email address.")
        if key in ("logo_url", "website") and value and not re.match(r"^https?://", value, re.I):
            if key == "website":
                value = "https://" + value
            else:
                raise InvalidField(key, "Logo must be an https:// image address.")
        if key == "vat_number" and value:
            digits = re.sub(r"\D", "", value)
            # South African VAT numbers are 10 digits starting with 4.
            if len(digits) != 10 or not digits.startswith("4"):
                raise InvalidField(key, "A South African VAT number has 10 digits and starts with 4.")
            value = digits
        out[key] = value
    return out


def update_store(supa, store_id: str, changes: dict) -> dict:
    """Apply validated changes. Raises MissingColumns when the table lacks a
    column being written, so the caller can say so instead of 500-ing."""
    payload = clean_changes(changes)
    if not payload:
        return read_store(supa, store_id)
    try:
        supa.table("stores").update(payload).eq("id", store_id).execute()
    except Exception as e:
        text = str(e)
        if "PGRST204" in text or ("column" in text.lower() and "schema cache" in text.lower()):
            raise MissingColumns(text) from e
        raise
    return read_store(supa, store_id)
