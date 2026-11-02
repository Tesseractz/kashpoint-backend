"""A sales receipt: what goes on it, and how it reads as an email.

One sale at the till is several `sales` rows (one per product), so a receipt
is built from all of them. The slip the cashier sees and prints in the app
(frontend src/components/ReceiptSlip.jsx) renders the same fields, so the
customer gets the same document whichever way it reaches them.

A shop that has entered its VAT number gets a tax invoice: SARS wants the
words "Tax Invoice", the supplier's name and VAT number, the date, a
description and either the VAT amount or a note that prices include VAT.
"""
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Iterable, Optional

from app.services.store_profile import read_store

VAT_RATE = Decimal("0.15")  # South Africa
SA_TZ = timezone(timedelta(hours=2))  # SAST has no daylight saving


def money(value) -> str:
    """R 10 000.00 — a space every three digits, always two decimals."""
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, frac = f"{abs(amount):.2f}".split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{sign}R {' '.join(groups)}.{frac}"


def vat_breakdown(total) -> dict:
    amount = Decimal(str(total or 0))
    vat = (amount * VAT_RATE / (1 + VAT_RATE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"rate": int(VAT_RATE * 100), "amount": float(vat), "excluding": float(amount - vat)}


def format_sast(value) -> str:
    """'02 Nov 2026, 19:58' in South African time."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return str(value or "")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SA_TZ).strftime("%d %b %Y, %H:%M")


def _rows(res) -> list:
    return getattr(res, "data", None) or []


def build_receipt(
    supa,
    store_id: str,
    sale_ids: Iterable[int],
    *,
    payment_method: Optional[str] = None,
    payment_amount: Optional[float] = None,
    change: Optional[float] = None,
    customer_id: Optional[str] = None,
) -> dict:
    """Everything the receipt shows, from the database. Raises LookupError when
    none of the sales belong to this store."""
    ids = [int(i) for i in sale_ids]
    sales = _rows(
        supa.table("sales").select("*").eq("store_id", store_id).in_("id", ids).order("id").execute()
    )
    if not sales:
        raise LookupError("Sale not found")

    product_ids = sorted({s["product_id"] for s in sales if s.get("product_id") is not None})
    products = {}
    if product_ids:
        for p in _rows(supa.table("products").select("id,name,price,sku").in_("id", product_ids).execute()):
            products[p["id"]] = p

    items = []
    subtotal = Decimal("0")
    discount = Decimal("0")
    total = Decimal("0")
    for s in sales:
        p = products.get(s.get("product_id"), {})
        qty = int(s.get("quantity_sold") or 1)
        line_total = Decimal(str(s.get("total_price") or 0))
        line_sub = Decimal(str(s.get("subtotal") or line_total))
        line_disc = Decimal(str(s.get("discount_amount") or 0))
        unit = (line_sub / qty) if qty else Decimal(str(p.get("price") or 0))
        items.append(
            {
                "name": p.get("name") or f"Product #{s.get('product_id')}",
                "sku": p.get("sku"),
                "quantity": qty,
                "unit_price": float(unit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                "total": float(line_total),
            }
        )
        subtotal += line_sub
        discount += line_disc
        total += line_total

    first = sales[0]
    store = read_store(supa, store_id)

    cashier = None
    if first.get("sold_by"):
        try:
            prof = _rows(supa.table("profiles").select("name").eq("id", first["sold_by"]).limit(1).execute())
            cashier = (prof[0].get("name") if prof else None) or None
        except Exception:
            cashier = None
    # An owner's profile name is their email address; that is not a name to
    # print on a customer's receipt.
    if cashier and "@" in cashier:
        cashier = None

    customer = None
    cid = customer_id or first.get("customer_id")
    if cid:
        try:
            rows = _rows(
                supa.table("customers").select("name,phone,email").eq("id", cid).eq("store_id", store_id).limit(1).execute()
            )
            if rows:
                customer = {"name": rows[0].get("name"), "phone": rows[0].get("phone"), "email": rows[0].get("email")}
        except Exception:
            customer = None

    method = (payment_method or first.get("payment_method") or "cash").lower()
    tendered = float(payment_amount) if payment_amount is not None else float(total)
    change_due = float(change) if change is not None else max(0.0, tendered - float(total)) if method == "cash" else 0.0

    vat = vat_breakdown(total) if store.get("vat_number") else None
    return {
        "number": first["id"],
        "title": "TAX INVOICE" if vat else "RECEIPT",
        "store": store,
        "timestamp": first.get("timestamp"),
        "date_label": format_sast(first.get("timestamp")),
        "cashier": cashier,
        "customer": customer,
        "items": items,
        "subtotal": float(subtotal),
        "discount": float(discount),
        "total": float(total),
        "vat": vat,
        "payment": {"method": method, "tendered": tendered, "change": change_due},
    }


def receipt_subject(receipt: dict) -> str:
    kind = "Tax invoice" if receipt.get("vat") else "Receipt"
    return f"{kind} #{receipt['number']} from {receipt['store']['name']}"


def receipt_text(receipt: dict) -> str:
    """Plain-text version (the email's text part)."""
    store = receipt["store"]
    lines = [store["name"].upper()]
    for key in ("address", "phone", "email", "website"):
        if store.get(key):
            lines.append(store[key])
    if store.get("vat_number"):
        lines.append(f"VAT No: {store['vat_number']}")
    if store.get("registration_number"):
        lines.append(f"Reg No: {store['registration_number']}")
    lines += ["", f"{receipt['title']} #{receipt['number']}", receipt["date_label"]]
    if receipt.get("cashier"):
        lines.append(f"Served by: {receipt['cashier']}")
    if receipt.get("customer") and receipt["customer"].get("name"):
        lines.append(f"Customer: {receipt['customer']['name']}")
    lines.append("")
    for it in receipt["items"]:
        lines.append(f"{it['quantity']} x {it['name']} @ {money(it['unit_price'])}")
        lines.append(f"{money(it['total']):>40}")
    lines.append("")
    if receipt["discount"]:
        lines.append(f"Subtotal: {money(receipt['subtotal'])}")
        lines.append(f"Discount: -{money(receipt['discount'])}")
    lines.append(f"TOTAL: {money(receipt['total'])}")
    if receipt.get("vat"):
        v = receipt["vat"]
        lines.append(f"Includes VAT @ {v['rate']}%: {money(v['amount'])}")
    pay = receipt["payment"]
    if pay["method"] == "card":
        lines.append("Paid by card")
    else:
        lines.append(f"Paid cash: {money(pay['tendered'])}")
        lines.append(f"Change: {money(pay['change'])}")
    lines.append("")
    lines.append(store.get("receipt_footer") or "Thank you for your support!")
    lines.append("Powered by KashPoint - kashpoint.co.za")
    return "\n".join(lines)


def receipt_html(receipt: dict) -> str:
    """The emailed receipt: inline styles only (Gmail strips <style>)."""
    store = receipt["store"]
    e = escape

    head_lines = []
    for key in ("address", "phone", "email", "website"):
        if store.get(key):
            # An address is typed over several lines; keep them.
            head_lines.append(e(str(store[key])).replace("\n", "<br>"))
    if store.get("vat_number"):
        head_lines.append(f"VAT No: {e(str(store['vat_number']))}")
    if store.get("registration_number"):
        head_lines.append(f"Reg No: {e(str(store['registration_number']))}")
    details = "<br>".join(head_lines)

    logo = ""
    if store.get("logo_url"):
        logo = (
            f'<img src="{e(str(store["logo_url"]))}" alt="{e(store["name"])}" '
            'style="max-height:72px;max-width:220px;display:block;margin:0 auto 10px;">'
        )

    meta = [receipt["date_label"]]
    if receipt.get("cashier"):
        meta.append(f"Served by {e(receipt['cashier'])}")
    if receipt.get("customer") and receipt["customer"].get("name"):
        meta.append(f"Customer: {e(receipt['customer']['name'])}")

    rows = ""
    for it in receipt["items"]:
        rows += (
            '<tr>'
            f'<td style="padding:9px 0;border-bottom:1px solid #e2e8f0;font-size:14px;color:#0f172a;">{e(it["name"])}'
            f'<div style="font-size:12px;color:#64748b;">{it["quantity"]} × {money(it["unit_price"])}</div></td>'
            f'<td style="padding:9px 0;border-bottom:1px solid #e2e8f0;text-align:right;font-size:14px;color:#0f172a;white-space:nowrap;">{money(it["total"])}</td>'
            '</tr>'
        )

    totals = ""
    if receipt["discount"]:
        totals += _total_row("Subtotal", money(receipt["subtotal"]))
        totals += _total_row("Discount", "-" + money(receipt["discount"]))
    totals += (
        '<tr><td style="padding:10px 0 4px;font-size:16px;font-weight:700;color:#0f172a;">Total</td>'
        f'<td style="padding:10px 0 4px;text-align:right;font-size:20px;font-weight:700;color:#0f172a;white-space:nowrap;">{money(receipt["total"])}</td></tr>'
    )
    if receipt.get("vat"):
        v = receipt["vat"]
        totals += _total_row(f"Includes VAT @ {v['rate']}%", money(v["amount"]), muted=True)
        totals += _total_row("Excluding VAT", money(v["excluding"]), muted=True)
    pay = receipt["payment"]
    if pay["method"] == "card":
        totals += _total_row("Paid by", "Card", muted=True)
    else:
        totals += _total_row("Cash tendered", money(pay["tendered"]), muted=True)
        totals += _total_row("Change", money(pay["change"]), muted=True)

    footer = e(store.get("receipt_footer") or "Thank you for your support!")
    reply_line = ""
    if store.get("email") or store.get("phone"):
        contact = " · ".join(e(str(store[k])) for k in ("phone", "email") if store.get(k))
        reply_line = f'<p style="margin:6px 0 0;font-size:12px;color:#64748b;">Questions about this receipt? {contact}</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(receipt_subject(receipt))}</title></head>
<body style="margin:0;padding:24px 12px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:480px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,0.08);">
  <tr><td style="padding:28px 28px 18px;text-align:center;border-bottom:1px dashed #cbd5e1;">
    {logo}
    <div style="font-size:22px;font-weight:700;color:#0f172a;">{e(store["name"])}</div>
    <div style="margin-top:6px;font-size:13px;line-height:1.5;color:#475569;">{details}</div>
  </td></tr>
  <tr><td style="padding:16px 28px 0;text-align:center;">
    <div style="font-size:12px;letter-spacing:2px;font-weight:700;color:#0f172a;">{e(receipt["title"])}</div>
    <div style="font-size:13px;color:#475569;margin-top:4px;">No. {receipt["number"]} · {" · ".join(meta)}</div>
  </td></tr>
  <tr><td style="padding:12px 28px 0;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:6px;">{totals}</table>
  </td></tr>
  <tr><td style="padding:20px 28px 26px;text-align:center;border-top:1px dashed #cbd5e1;margin-top:16px;">
    <p style="margin:0;font-size:14px;font-weight:600;color:#047857;">{footer}</p>
    {reply_line}
    <p style="margin:14px 0 0;font-size:11px;color:#94a3b8;">Powered by <a href="https://kashpoint.co.za" style="color:#94a3b8;">KashPoint</a> · Point of sale for South African shops</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def _total_row(label: str, value: str, muted: bool = False) -> str:
    color = "#64748b" if muted else "#0f172a"
    weight = "400" if muted else "600"
    return (
        f'<tr><td style="padding:3px 0;font-size:13px;color:{color};font-weight:{weight};">{escape(label)}</td>'
        f'<td style="padding:3px 0;text-align:right;font-size:13px;color:{color};font-weight:{weight};white-space:nowrap;">{escape(value)}</td></tr>'
    )
