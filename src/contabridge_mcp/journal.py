"""Canonical journal (libro diario) model, validation and account-code tooling.

Amounts are integer cents end to end. An entry (asiento) is a dated list of lines (apuntes); each line has
exactly one of debit (debe) or credit (haber).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable


class JournalError(ValueError):
    pass


@dataclass
class Line:
    account: str
    debit: int = 0
    credit: int = 0
    concept: str = ""
    document: str = ""
    account_name: str = ""
    source_line: int | None = None


@dataclass
class Entry:
    number: int | None
    date: date
    lines: list[Line] = field(default_factory=list)

    @property
    def debit(self) -> int:
        return sum(ln.debit for ln in self.lines)

    @property
    def credit(self) -> int:
        return sum(ln.credit for ln in self.lines)


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


_AMOUNT = re.compile(r"^[+-]?\d+(\.\d+)?$")


def parse_amount(raw: str, where: str = "") -> int:
    """'1.234,56' / '1,234.56' / '1234.5' / '-12' / '' -> cents. Never uses float."""
    s = (raw or "").strip().replace(" ", "").replace(" ", "").replace("€", "")
    if not s:
        return 0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(",", ".")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1 or re.fullmatch(r"[+-]?\d{1,3}(\.\d{3})+", s) and not re.fullmatch(r"[+-]?0\.\d{3}", s):
        s = s.replace(".", "")
    if not _AMOUNT.match(s):
        raise JournalError(f"{where}importe no válido: {raw!r}")
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    whole, _, frac = s.partition(".")
    if len(frac) > 2 and frac[2:].strip("0"):
        raise JournalError(f"{where}importe con más de 2 decimales: {raw!r}")
    cents = int(whole or "0") * 100 + int((frac + "00")[:2])
    return -sign * cents if neg else sign * cents


_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), (1, 2, 3)),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), (1, 2, 3)),
    (re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$"), (3, 2, 1)),
    (re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2})$"), (3, 2, 1)),
)


def parse_date(raw: str, where: str = "") -> date:
    s = (raw or "").strip()
    for rx, (y, m, d) in _DATE_PATTERNS:
        mt = rx.match(s)
        if mt:
            year = int(mt.group(y))
            if year < 100:
                year += 2000
            try:
                return date(year, int(mt.group(m)), int(mt.group(d)))
            except ValueError:
                break
    raise JournalError(f"{where}fecha no válida: {raw!r}")


def expand_account(code: str, digits: int) -> str:
    """Spanish 'punto' shorthand: '430.1' with 8 digits -> '43000001'."""
    c = code.strip()
    if "." not in c:
        return c
    head, _, tail = c.partition(".")
    fill = digits - len(head) - len(tail)
    if fill < 0 or not (head + tail).isdigit():
        raise JournalError(f"no se puede expandir la cuenta {code!r} a {digits} dígitos")
    return head + "0" * fill + tail


def resize_account(code: str, digits: int, split: int = 4) -> str:
    """Lengthen or shorten a subaccount by inserting/removing zeros after the first `split` digits.

    Mirrors the 'ampliar/reducir dígitos de subcuenta' of Spanish accounting packages: 4300001 (7) -> 43000001 (8).
    Shortening only removes zeros; it refuses when significant digits would be lost.
    """
    c = expand_account(code, digits)
    if len(c) == digits:
        return c
    if len(c) < digits:
        cut = min(split, len(c))
        return c[:cut] + "0" * (digits - len(c)) + c[cut:]
    extra = len(c) - digits
    cut = min(split, digits)
    if c[cut:cut + extra].strip("0"):
        raise JournalError(f"la cuenta {code!r} no cabe en {digits} dígitos sin perder dígitos significativos")
    return c[:cut] + c[cut + extra:]


def remap(entries: Iterable[Entry], account_map: dict[str, str] | None, digits: int | None,
          split: int = 4) -> list[Entry]:
    amap = {k.strip(): v.strip() for k, v in (account_map or {}).items()}
    out = list(entries)
    for e in out:
        for ln in e.lines:
            acc = amap.get(ln.account, ln.account)
            if digits:
                acc = resize_account(acc, digits, split)
            elif "." in acc:
                raise JournalError(f"la cuenta {acc!r} usa punto: indica account_digits para expandirla")
            ln.account = acc
    return out


PGC_GROUPS = {"1": "Financiación básica", "2": "Activo no corriente", "3": "Existencias",
              "4": "Acreedores y deudores por operaciones comerciales", "5": "Cuentas financieras",
              "6": "Compras y gastos", "7": "Ventas e ingresos", "8": "Gastos imputados al patrimonio neto",
              "9": "Ingresos imputados al patrimonio neto"}


def validate(entries: list[Entry], fiscal_year: int | None = None) -> list[dict[str, Any]]:
    """Checks every accounting package enforces on import, so the file is fixed before it is rejected."""
    issues: list[dict[str, Any]] = []

    def add(level: str, entry: int, msg: str, line: int | None = None) -> None:
        d: dict[str, Any] = {"level": level, "entry": entry, "message": msg}
        if line is not None:
            d["source_line"] = line
        issues.append(d)

    lengths: dict[int, int] = {}
    for i, e in enumerate(entries, 1):
        ref = e.number if e.number is not None else i
        if len(e.lines) < 2:
            add("error", ref, "el asiento tiene menos de 2 apuntes")
        if e.debit != e.credit:
            add("error", ref, f"asiento descuadrado: debe {fmt(e.debit)} ≠ haber {fmt(e.credit)} "
                              f"(diferencia {fmt(e.debit - e.credit)})")
        if fiscal_year and e.date.year != fiscal_year:
            add("error", ref, f"fecha {e.date.isoformat()} fuera del ejercicio {fiscal_year}")
        for ln in e.lines:
            if ln.debit and ln.credit:
                add("error", ref, f"el apunte de {ln.account} tiene debe y haber a la vez", ln.source_line)
            if not ln.debit and not ln.credit:
                add("warning", ref, f"apunte de importe cero en {ln.account}", ln.source_line)
            if ln.debit < 0 or ln.credit < 0:
                add("warning", ref, f"importe negativo en {ln.account} (algunos programas lo rechazan)",
                    ln.source_line)
            if not ln.account.isdigit():
                add("error", ref, f"cuenta no numérica: {ln.account!r}", ln.source_line)
                continue
            if ln.account[0] not in PGC_GROUPS:
                add("error", ref, f"cuenta {ln.account} fuera de los grupos 1-9 del PGC", ln.source_line)
            lengths[len(ln.account)] = lengths.get(len(ln.account), 0) + 1
    if len(lengths) > 1:
        detail = ", ".join(f"{n} dígitos: {c}" for n, c in sorted(lengths.items()))
        issues.append({"level": "warning", "entry": None,
                       "message": f"longitudes de cuenta mezcladas ({detail}); usa account_digits para unificarlas"})
    return issues


def summary(entries: list[Entry]) -> dict[str, Any]:
    debit = sum(e.debit for e in entries)
    credit = sum(e.credit for e in entries)
    dates = [e.date for e in entries]
    return {"entries": len(entries), "lines": sum(len(e.lines) for e in entries),
            "total_debit": fmt(debit), "total_credit": fmt(credit), "balanced": debit == credit,
            "from": min(dates).isoformat() if dates else None, "to": max(dates).isoformat() if dates else None,
            "accounts": len({ln.account for e in entries for ln in e.lines})}


def entries_dict(entries: list[Entry], cap: int) -> list[dict[str, Any]]:
    out = []
    for e in entries[:cap]:
        out.append({"number": e.number, "date": e.date.isoformat(), "lines": [
            {"account": ln.account, "debit": fmt(ln.debit), "credit": fmt(ln.credit), "concept": ln.concept,
             "document": ln.document, **({"account_name": ln.account_name} if ln.account_name else {})}
            for ln in e.lines]})
    return out


def entries_from_json(items: list[dict[str, Any]]) -> list[Entry]:
    """Agent-built entries: [{date, number?, lines: [{account, debit|credit, concept?, document?}]}]."""
    out: list[Entry] = []
    for i, it in enumerate(items, 1):
        where = f"asiento {i}: "
        if not isinstance(it, dict) or not isinstance(it.get("lines"), list):
            raise JournalError(f"{where}se espera {{date, lines: [...]}}")
        e = Entry(number=int(it["number"]) if it.get("number") not in (None, "") else None,
                  date=parse_date(str(it.get("date", "")), where))
        for ln in it["lines"]:
            e.lines.append(Line(account=str(ln.get("account", "")).strip(),
                                debit=parse_amount(str(ln.get("debit", "") or ""), where),
                                credit=parse_amount(str(ln.get("credit", "") or ""), where),
                                concept=str(ln.get("concept", "") or ""), document=str(ln.get("document", "") or ""),
                                account_name=str(ln.get("account_name", "") or "")))
        out.append(e)
    return out


def number_entries(entries: list[Entry], start: int | None) -> None:
    """Renumber from `start`, or fill only the missing numbers after the highest one present."""
    if start is not None:
        for i, e in enumerate(entries):
            e.number = start + i
        return
    nxt = max((e.number for e in entries if e.number is not None), default=0) + 1
    for e in entries:
        if e.number is None:
            e.number = nxt
            nxt += 1
