"""Invoices (facturas emitidas / recibidas) -> journal entries with VAT, recargo and IRPF split out.

Deterministic: every amount is integer cents; tax amounts are recomputed from base x rate (half up) and an
invoice whose figures do not add up (base + IVA + recargo - IRPF != total) is never booked: it goes to `review`.
Nothing is guessed: an invoice without a counterparty subaccount is booked to the generic 430/400 account and
flagged, so the accountant can assign it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .journal import Entry, JournalError, Line, fmt, parse_amount, parse_date

DEFAULT_ACCOUNTS = {
    "customers": "430", "suppliers": "400", "sales": "700", "purchases": "600",
    "vat_output": "477", "vat_input": "472", "irpf_withheld": "4751", "irpf_suffered": "473",
}
KINDS = {"issued": "issued", "emitida": "issued", "venta": "issued", "sale": "issued",
         "received": "received", "recibida": "received", "compra": "received", "purchase": "received"}
MAX_INVOICES = 5000
TOLERANCE = 1  # cents: tax rounding per line in the source invoice


def _rate(raw: Any, where: str) -> Decimal:
    try:
        r = Decimal(str(raw).replace(",", ".").replace("%", "").strip() or "0")
    except InvalidOperation as exc:
        raise JournalError(f"{where}: tipo no numérico {raw!r}") from exc
    if r < 0 or r > 100:
        raise JournalError(f"{where}: tipo fuera de rango {raw!r}")
    return r


def _pct(base: int, rate: Decimal) -> int:
    return int((Decimal(base) * rate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _cents(raw: Any, where: str) -> int:
    return parse_amount(str(raw), f"{where}: ")


def _rate_key(rate: Decimal) -> str:
    return format(rate.normalize(), "f")


@dataclass
class Booked:
    entry: Entry
    vat: list[dict[str, Any]]
    generic_party: bool
    open_item: dict[str, Any]


def _line(lines: list[Line], account: str, cents: int, debit_side: bool, concept: str, doc: str) -> None:
    """Add a line; a negative amount (factura rectificativa) goes to the opposite side."""
    if not cents:
        return
    if cents < 0:
        cents, debit_side = -cents, not debit_side
    lines.append(Line(account=account, debit=cents if debit_side else 0, credit=0 if debit_side else cents,
                      concept=concept[:60], document=doc[:20]))


def book(item: dict[str, Any], idx: int, accounts: dict[str, str], parties: dict[str, str],
         vat_accounts: dict[str, str]) -> Booked:
    where = f"factura {idx + 1}"
    kind = KINDS.get(str(item.get("kind", "")).strip().lower())
    if not kind:
        raise JournalError(f"{where}: kind debe ser 'issued' (emitida) o 'received' (recibida)")
    number = str(item.get("number", "") or "").strip()
    if not number:
        raise JournalError(f"{where}: falta number (número de factura)")
    where = f"factura {number}"
    when = parse_date(str(item.get("date", "") or ""), f"{where}: ")
    nif = str(item.get("nif", "") or "").strip().upper().replace(" ", "").replace("-", "")
    name = str(item.get("name", "") or "").strip()
    issued = kind == "issued"
    taxes = item.get("taxes") or ([{"base": item["base"], "rate": item.get("vat_rate", "21")}]
                                  if "base" in item else [])
    if not isinstance(taxes, list) or not taxes:
        raise JournalError(f"{where}: indica taxes [{{base, rate}}] o base + vat_rate")

    party = str(item.get("account", "") or "").strip() or (parties.get(nif, "") if nif else "")
    generic = not party
    party = party or accounts["customers" if issued else "suppliers"]
    counterpart = str(item.get("counterpart_account", "") or "").strip() or accounts["sales" if issued else "purchases"]
    vat_root = accounts["vat_output" if issued else "vat_input"]
    concept = (str(item.get("concept", "") or "").strip()
               or f"{'Fra.' if issued else 'Fra. prov.'} {number} {name}".strip())

    bases = vat_total = re_total = 0
    vat_rows: list[dict[str, Any]] = []
    tax_lines: list[tuple[str, int]] = []
    for j, t in enumerate(taxes):
        if not isinstance(t, dict) or "base" not in t:
            raise JournalError(f"{where}: taxes[{j}] necesita base")
        base = _cents(t["base"], f"{where} base")
        rate = _rate(t.get("rate", "0"), f"{where} IVA")
        vat = _pct(base, rate)
        if t.get("vat") not in (None, ""):
            given = _cents(t["vat"], f"{where} cuota")
            if abs(given - vat) > TOLERANCE:
                raise JournalError(f"{where}: cuota IVA {fmt(given)} no cuadra con {fmt(base)} x {rate} % = {fmt(vat)}")
            vat = given
        re_rate = _rate(t.get("recargo_rate", "0") or "0", f"{where} recargo")
        recargo = _pct(base, re_rate)
        key = _rate_key(rate)
        acc = vat_accounts.get(key) or vat_root
        if vat:
            tax_lines.append((acc, vat))
        if recargo:
            tax_lines.append((vat_accounts.get(f"re{_rate_key(re_rate)}") or vat_root, recargo))
        bases += base
        vat_total += vat
        re_total += recargo
        vat_rows.append({"rate": key, "base": base, "vat": vat, "recargo_rate": _rate_key(re_rate),
                         "recargo": recargo})

    irpf = 0
    if item.get("irpf_amount") not in (None, ""):
        irpf = _cents(item["irpf_amount"], f"{where} IRPF")
    elif item.get("irpf_rate") not in (None, ""):
        irpf = _pct(bases, _rate(item["irpf_rate"], f"{where} IRPF"))
    total = bases + vat_total + re_total - irpf
    if item.get("total") not in (None, ""):
        given = _cents(item["total"], f"{where} total")
        if abs(given - total) > TOLERANCE * max(1, len(vat_rows)):
            raise JournalError(f"{where}: el total {fmt(given)} no cuadra con base {fmt(bases)} + IVA "
                               f"{fmt(vat_total)} + recargo {fmt(re_total)} - IRPF {fmt(irpf)} = {fmt(total)}")
        diff = given - total
        if diff:  # one-cent rounding of the source: keep its total, adjust the largest tax line
            if not tax_lines:
                raise JournalError(f"{where}: el total no cuadra por {fmt(diff)}")
            k = max(range(len(tax_lines)), key=lambda i: tax_lines[i][1])
            tax_lines[k] = (tax_lines[k][0], tax_lines[k][1] + diff)
            total = given
    if not total and not bases:
        raise JournalError(f"{where}: importes a cero")

    lines: list[Line] = []
    if issued:
        _line(lines, party, total, True, concept, number)
        _line(lines, accounts["irpf_suffered"], irpf, True, concept, number)
        _line(lines, counterpart, bases, False, concept, number)
        for acc, cents in tax_lines:
            _line(lines, acc, cents, False, concept, number)
    else:
        _line(lines, counterpart, bases, True, concept, number)
        for acc, cents in tax_lines:
            _line(lines, acc, cents, True, concept, number)
        _line(lines, accounts["irpf_withheld"], irpf, False, concept, number)
        _line(lines, party, total, False, concept, number)
    for r in vat_rows:
        r.update(kind=kind, number=number, nif=nif, name=name, date=when.isoformat())
    open_item = {"account": party, "amount": fmt(total if issued else -total), "reference": number,
                 "name": name, "date": when.isoformat()}
    return Booked(Entry(number=None, date=when, lines=lines), vat_rows, generic, open_item)


def invoices_to_entries(items: list[dict[str, Any]], accounts: dict[str, str] | None = None,
                        party_accounts: dict[str, str] | None = None,
                        vat_accounts: dict[str, str] | None = None, digits: int = 0) -> tuple[list[Entry], dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise JournalError("no hay facturas")
    if len(items) > MAX_INVOICES:
        raise JournalError(f"máximo {MAX_INVOICES} facturas por llamada")
    acc = dict(DEFAULT_ACCOUNTS)
    for k, v in (accounts or {}).items():
        if k not in acc:
            raise JournalError(f"accounts: clave desconocida {k!r}; válidas: {', '.join(acc)}")
        if not str(v).strip().isdigit():
            raise JournalError(f"accounts.{k}: subcuenta numérica")
        acc[k] = str(v).strip()
    digits = digits or max([len(str(v).strip()) for v in (party_accounts or {}).values()] or [8])
    acc = {k: v.ljust(digits, "0") for k, v in acc.items()}
    parties = {str(k).strip().upper().replace(" ", "").replace("-", ""): str(v).strip()
               for k, v in (party_accounts or {}).items()}
    vat_acc = {str(k).replace("%", "").strip(): str(v).strip() for k, v in (vat_accounts or {}).items()}
    entries: list[Entry] = []
    review: list[dict[str, Any]] = []
    generic: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    book_rows: list[dict[str, Any]] = []
    open_items: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            review.append({"index": i, "reason": "no es un objeto"})
            continue
        key = (str(item.get("kind", "")).lower(), str(item.get("nif", "")).upper(), str(item.get("number", "")))
        if key in seen:
            review.append({"index": i, "number": item.get("number"), "reason": "factura duplicada (mismo NIF y número)"})
            continue
        seen.add(key)
        try:
            b = book(item, i, acc, parties, vat_acc)
        except JournalError as exc:
            review.append({"index": i, "number": item.get("number"), "reason": str(exc)})
            continue
        entries.append(b.entry)
        book_rows.extend(b.vat)
        open_items.append(b.open_item)
        if b.generic_party:
            generic.append(str(item.get("number")))
    vat_summary: dict[str, dict[str, Any]] = {}
    for r in book_rows:
        k = f"{r['kind']} {r['rate']}%"
        s = vat_summary.setdefault(k, {"kind": r["kind"], "rate": r["rate"], "base": 0, "vat": 0, "recargo": 0,
                                       "invoices": 0})
        s["base"] += r["base"]
        s["vat"] += r["vat"]
        s["recargo"] += r["recargo"]
        s["invoices"] += 1
    summary = [{**s, "base": fmt(s["base"]), "vat": fmt(s["vat"]), "recargo": fmt(s["recargo"])}
               for s in sorted(vat_summary.values(), key=lambda s: (s["kind"], Decimal(s["rate"])))]
    register = [{"kind": r["kind"], "date": r["date"], "number": r["number"], "nif": r["nif"], "name": r["name"],
                 "rate": r["rate"], "base": fmt(r["base"]), "vat": fmt(r["vat"]), "recargo": fmt(r["recargo"])}
                for r in book_rows]
    stats = {"invoices": len(items), "booked": len(entries), "review": review,
             "generic_party_account": generic, "vat_summary": summary, "vat_register": register,
             "open_items": open_items}
    return entries, stats
