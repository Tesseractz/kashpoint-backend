"""Phone numbers captured at signup, stored in one canonical form (E.164).

Shop owners type numbers every way imaginable: "082 123 4567", "0821234567",
"+27 82 123 4567", "27821234567", "(082) 123-4567". They all mean the same
WhatsApp number, and keeping one form is what makes the list usable for
follow-up. The frontend applies the same rules (src/lib/phone.js); this copy
is the backstop for anything that reaches the API another way.
"""
import re
from typing import Optional

_E164 = re.compile(r"\+[1-9]\d{7,14}")


def normalize_phone(raw) -> Optional[str]:
    """Return the number as +<country><number>, or None if it isn't one.

    South African local numbers (10 digits starting 0) and 27-prefixed numbers
    without the plus become +27...; any other number must already carry its
    country code.
    """
    if raw is None:
        return None
    s = re.sub(r"[^\d+]", "", str(raw))
    if not s:
        return None
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("0") and len(s) == 10:
        s = "+27" + s[1:]
    elif s.startswith("27") and len(s) == 11:
        s = "+" + s
    if s.startswith("+27") and len(s) != 12:
        return None
    return s if _E164.fullmatch(s) else None
