"""Norma 43 bank movements -> journal entries, with deterministic first-match rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import ids, n43
from .journal import Entry, JournalError, Line, parse_date


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
                         use_value_date: bool = False) -> tuple[list[Entry], dict[str, Any]]:
    entries: list[Entry] = []
    unmatched: list[dict[str, Any]] = []
    hits: dict[int, int] = {}
    for acc in res.accounts:
        bank = _bank_account(acc, bank_account, bank_accounts or {})
        for m in acc.movements:
            text = m.description()
            when = (m.value_date if use_value_date else m.operation_date) or m.operation_date or m.value_date
            if not when:
                raise JournalError(f"movimiento {m.id} sin fecha")
            rule = next((r for r in rules if r.matches(text, m)), None)
            if rule:
                hits[rule.index] = hits.get(rule.index, 0) + 1
                other = rule.account
            else:
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
    return entries, stats
