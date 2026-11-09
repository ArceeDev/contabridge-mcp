"""Norma 43 bank movements -> journal entries, with deterministic first-match rules."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import Any

from . import ids, n43
from .journal import Entry, JournalError, Line, fmt, parse_amount, parse_date


@dataclass
class Rule:
    account: str
    contains: list[str]
    regex: re.Pattern[str] | None
    common_concept: str
    direction: str
    concept: str
    index: int

    def matches(self, text: str, m: n43.Movement) -> bool:
        if self.direction and self.direction != ("cargo" if m.cents < 0 else "abono"):
            return False
        if self.common_concept and self.common_concept != m.common_concept:
            return False
        low = text.lower()
        if self.contains and not any(c in low for c in self.contains):
            return False
        if self.regex and not self.regex.search(text):
            return False
        return True


try:  # Python 3.11+
    from re import _constants as _sre_c, _parser as _sre_p  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Python 3.10
    import sre_constants as _sre_c  # type: ignore[no-redef]
    import sre_parse as _sre_p  # type: ignore[no-redef]

MAX_REGEX_LEN = 300
_REPEATS = (_sre_c.MAX_REPEAT, _sre_c.MIN_REPEAT, getattr(_sre_c, "POSSESSIVE_REPEAT", None))


def _risky(items: Any, in_repeat: bool = False) -> bool:
    """True for backreferences or a repeat nested in another repeat: the shapes behind catastrophic
    backtracking, e.g. (a+)+ or (a*)*b, which would hold the server's CPU for minutes."""
    for op, av in items:
        if op in (_sre_c.GROUPREF, getattr(_sre_c, "GROUPREF_EXISTS", None)):
            return True
        if op in _REPEATS and op is not None:
            lo, hi, sub = av
            many = hi is _sre_c.MAXREPEAT or hi > 1
            if many and in_repeat:
                return True
            if _risky(sub, in_repeat or many):
                return True
        elif op is _sre_c.SUBPATTERN:
            if _risky(av[-1], in_repeat):
                return True
        elif op is _sre_c.BRANCH:
            if any(_risky(b, in_repeat) for b in av[1]):
                return True
        elif op in (_sre_c.ASSERT, _sre_c.ASSERT_NOT):
            if _risky(av[1], in_repeat):
                return True
    return False


def compile_rule_regex(rx: str, where: str) -> re.Pattern[str]:
    if len(rx) > MAX_REGEX_LEN:
        raise JournalError(f"{where}: regex demasiado larga (máx. {MAX_REGEX_LEN} caracteres)")
    try:
        pattern = re.compile(rx, re.IGNORECASE)
    except re.error as exc:
        raise JournalError(f"{where}: regex no válida ({exc})") from exc
    if _risky(_sre_p.parse(rx)):
        raise JournalError(f"{where}: regex con cuantificadores anidados o referencias (p. ej. (a+)+); "
                           "simplifícala o usa 'contains'")
    return pattern


def build_rules(items: list[dict[str, Any]]) -> list[Rule]:
    out = []
    for i, it in enumerate(items or [], 1):
        if not isinstance(it, dict) or not str(it.get("account", "")).strip():
            raise JournalError(f"regla {i}: falta 'account'")
        contains = it.get("contains") or []
        if isinstance(contains, str):
            contains = [contains]
        rx = it.get("regex") or ""
        pattern = compile_rule_regex(str(rx), f"regla {i}") if rx else None
        direction = str(it.get("direction", "") or "").lower()
        if direction not in ("", "cargo", "abono"):
            raise JournalError(f"regla {i}: direction debe ser 'cargo' o 'abono'")
        cc = str(it.get("common_concept", "") or "").strip()
        if not (contains or pattern or cc or direction):
            raise JournalError(f"regla {i}: indica al menos contains, regex, common_concept o direction")
        out.append(Rule(account=str(it["account"]).strip(), contains=[str(c).lower() for c in contains],
                        regex=pattern, common_concept=cc.zfill(2) if cc else "", direction=direction,
                        concept=str(it.get("concept", "") or ""), index=i))
    return out


def _bank_account(acc: n43.Account, bank_account: str, bank_accounts: dict[str, str]) -> str:
    ccc = acc.ccc or ""
    iban = (ids.iban_from_ccc(ccc) or "") if ccc else ""
    for key, value in (bank_accounts or {}).items():
        k = re.sub(r"\s", "", key).upper()
        if k and (k == iban or k == ccc or iban.endswith(k) or ccc.endswith(k)):
            return value.strip()
    if not bank_account:
        raise JournalError(f"no hay subcuenta 572 para la cuenta bancaria {iban or ccc}: indica bank_account")
    return bank_account.strip()


def movements_to_entries(res: n43.ParseResult, bank_account: str, rules: list[Rule],
                         suspense_account: str, bank_accounts: dict[str, str] | None = None,
                         use_value_date: bool = False, open_items: list["OpenItem"] | None = None,
                         window_days: int = 60) -> tuple[list[Entry], dict[str, Any]]:
    entries: list[Entry] = []
    settled: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    hits: dict[int, int] = {}
    for acc in res.accounts:
        bank = _bank_account(acc, bank_account, bank_accounts or {})
        for m in acc.movements:
            text = m.description()
            when = (m.value_date if use_value_date else m.operation_date) or m.operation_date or m.value_date
            if not when:
                raise JournalError(f"movimiento {m.id} sin fecha")
            item, why = (match_open_item(m, parse_date(when), open_items, window_days)
                         if open_items else (None, ""))
            other = ""
            rule = None if item else next((r for r in rules if r.matches(text, m)), None)
            if item:
                item.used = True
                other = item.account
                settled.append({"movement": m.id, "open_item": item.index, "account": item.account,
                                "amount": n43.fmt_cents(m.cents), "evidence": why})
            elif why:
                ambiguous.append({"movement": m.id, "amount": n43.fmt_cents(m.cents), "reason": why})
            if rule:
                hits[rule.index] = hits.get(rule.index, 0) + 1
                other = rule.account
            elif not item:
                other = suspense_account
                unmatched.append({"movement": m.id, "date": when, "amount": n43.fmt_cents(m.cents),
                                  "description": text})
            concept = (rule.concept if rule and rule.concept else text)[:60]
            doc = (m.document_number or "").lstrip("0") or (m.reference1 or "").lstrip("0")
            amount = abs(m.cents)
            bank_line = Line(account=bank, debit=amount if m.cents > 0 else 0, credit=amount if m.cents < 0 else 0,
                             concept=concept, document=doc)
            other_line = Line(account=other, debit=amount if m.cents < 0 else 0, credit=amount if m.cents > 0 else 0,
                              concept=concept, document=doc)
            lines = [other_line, bank_line] if m.cents < 0 else [bank_line, other_line]
            entries.append(Entry(number=None, date=parse_date(when), lines=lines))
    stats = {"movements": len(entries), "matched": len(entries) - len(unmatched), "unmatched": unmatched,
             "rule_hits": {f"regla {k}": v for k, v in sorted(hits.items())},
             "suspense_account": suspense_account}
    if open_items:
        stats["open_items"] = {"settled": settled, "ambiguous": ambiguous[:200],
                               "still_open": [{"open_item": o.index, "account": o.account, "amount": fmt(o.cents)}
                                              for o in open_items if not o.used][:500]}
    return entries, stats


# --- Non-Norma 43 statements (bank Excel/CSV exports, or a movements list read by the assistant) -----------

_HEAD = {
    "date": ("fecha", "fecha operacion", "fecha operación", "f. operacion", "f. operación", "fecha contable",
             "date", "booking date", "transaction date", "fecha valor", "f. valor", "value date"),
    "value_date": ("fecha valor", "f. valor", "value date"),
    "amount": ("importe", "importe eur", "importe (eur)", "importe (€)", "cantidad", "amount", "movimiento"),
    "debit": ("cargo", "cargos", "debe", "debit", "salida", "pagos"),
    "credit": ("abono", "abonos", "haber", "credit", "entrada", "cobros", "ingresos"),
    "text": ("concepto", "descripcion", "descripción", "concepto común", "description", "concept", "texto",
             "detalle", "observaciones", "beneficiario", "referencia", "más datos", "mas datos"),
    "balance": ("saldo", "balance", "saldo (eur)"),
}
MAX_ROWS = 100_000


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower().replace("﻿", ""))


def _mov(i: int, when: str, cents: int, text: str, ref: str = "", value_date: str | None = None) -> n43.Movement:
    return n43.Movement(id=f"m{i + 1}", line=i + 1, office="", operation_date=when, value_date=value_date or when,
                        common_concept="", own_concept="", cents=cents, document_number="", reference1=ref,
                        reference2="", complementary=[{"text": text}] if text else [])


def _result(movs: list[n43.Movement]) -> n43.ParseResult:
    acc = n43.Account(index=0, line=0, bank="", branch="", number="", start=None, end=None, opening=0,
                      currency_code="978", mode="", holder="", movements=movs)
    return n43.ParseResult(accounts=[acc], issues=[], record_count=len(movs), has_end_record=True)


def movements_from_list(items: list[dict[str, Any]]) -> n43.ParseResult:
    """[{date, amount (+ in / - out), text, id?, reference?}] -> a one-account ParseResult."""
    if len(items) > MAX_ROWS:
        raise JournalError(f"máximo {MAX_ROWS} movimientos")
    movs = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise JournalError(f"movimiento {i + 1}: no es un objeto")
        where = f"movimiento {i + 1}: "
        when = parse_date(str(it.get("date", "") or ""), where).isoformat()
        cents = parse_amount(str(it.get("amount", "") or ""), where)
        if not cents:
            raise JournalError(f"{where}importe cero o vacío")
        text = " ".join(str(it.get(k) or "") for k in ("text", "description", "concept")).strip()
        m = _mov(i, when, cents, text, str(it.get("reference", "") or ""))
        if it.get("id"):
            m.id = str(it["id"])[:40]
        movs.append(m)
    return _result(movs)


def movements_from_csv(text: str) -> tuple[n43.ParseResult, dict[str, Any]]:
    """Bank CSV export (Excel saved as CSV): finds the header row, the date, amount (or cargo/abono) and
    concept columns, and refuses what it cannot read instead of guessing."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sample = "\n".join(lines[:50])
    delim = max((";", ",", "\t", "|"), key=sample.count)
    rows = list(csv.reader(lines, delimiter=delim))
    header_at, cols = -1, {}
    for r, row in enumerate(rows[:40]):
        names = [_norm(c) for c in row]
        found: dict[str, int] = {}
        for key, aliases in _HEAD.items():
            for c, name in enumerate(names):
                if name in aliases and c not in found.values():
                    found.setdefault(key, c)
                    break
        if "date" in found and ("amount" in found or ("debit" in found and "credit" in found)):
            header_at, cols = r, found
            break
    if header_at < 0:
        raise JournalError("no encuentro la cabecera del extracto: necesito columnas de fecha y de importe "
                           "(o cargo/abono). Pásalo como `movements` si el banco usa otros nombres.")
    movs: list[n43.Movement] = []
    skipped: list[int] = []
    for r, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(c.strip() for c in row):
            continue
        get = lambda k: row[cols[k]].strip() if k in cols and cols[k] < len(row) else ""  # noqa: E731
        try:
            when = parse_date(get("date"), f"fila {r}: ").isoformat()
            if "amount" in cols:
                cents = parse_amount(get("amount"), f"fila {r}: ")
            else:
                cents = parse_amount(get("credit"), f"fila {r}: ") - abs(parse_amount(get("debit"), f"fila {r}: "))
        except JournalError:
            skipped.append(r)  # totals / footer rows
            continue
        if not cents:
            skipped.append(r)
            continue
        vd = get("value_date")
        try:
            vd = parse_date(vd, "").isoformat() if vd and cols.get("value_date") != cols.get("date") else when
        except JournalError:
            vd = when
        movs.append(_mov(len(movs), when, cents, get("text"), value_date=vd))
        if len(movs) > MAX_ROWS:
            raise JournalError(f"máximo {MAX_ROWS} movimientos")
    if not movs:
        raise JournalError("el CSV no tiene movimientos legibles")
    return _result(movs), {"delimiter": delim, "header_row": header_at + 1,
                           "columns": {k: rows[header_at][v] for k, v in cols.items()}, "skipped_rows": skipped[:50]}


# --- Open items (facturas pendientes de cobro/pago) -> the customer/supplier subaccount -------------------

def _alnum(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


@dataclass
class OpenItem:
    index: int
    account: str
    cents: int
    reference: str
    name_tokens: list[str]
    when: Any
    used: bool = False


def build_open_items(items: list[dict[str, Any]]) -> list[OpenItem]:
    out = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict) or not str(it.get("account", "")).strip():
            raise JournalError(f"open_items[{i}]: necesita account (430/400 del cliente o proveedor)")
        cents = parse_amount(str(it.get("amount", "") or ""), f"open_items[{i}]: ")
        if not cents:
            raise JournalError(f"open_items[{i}]: amount vacío")
        when = parse_date(str(it["date"]), f"open_items[{i}]: ") if it.get("date") else None
        name = (str(it.get("name", "") or "")).lower()
        out.append(OpenItem(i, str(it["account"]).strip(), cents, _alnum(str(it.get("reference", "") or "")),
                            [t for t in re.findall(r"[a-záéíóúñü0-9]+", name) if len(t) >= 4], when))
    return out


def match_open_item(m: n43.Movement, when: Any, items: list[OpenItem], window_days: int) -> tuple[OpenItem | None, str]:
    """Same amount and sign, then: unique reference in the bank text > unique name > unique amount in window."""
    cands = [o for o in items if not o.used and o.cents == m.cents
             and (o.when is None or abs((when - o.when).days) <= window_days or when >= o.when)]
    if not cands:
        return None, ""
    text = _alnum(m.search_text())
    by_ref = [o for o in cands if len(o.reference) >= 3 and o.reference in text]
    if len(by_ref) == 1:
        return by_ref[0], "importe + referencia"
    if len(by_ref) > 1:
        return None, "varias facturas con la misma referencia e importe"
    low = m.search_text().lower()
    by_name = [o for o in cands if o.name_tokens and any(t in low for t in o.name_tokens)]
    if len(by_name) == 1:
        return by_name[0], "importe + nombre"
    if len(cands) == 1 and not by_name and cands[0].when is not None and abs((when - cands[0].when).days) <= window_days:
        return cands[0], "importe único en la ventana de fechas"
    return None, f"{len(cands)} facturas posibles con el mismo importe" if len(cands) > 1 else ""
