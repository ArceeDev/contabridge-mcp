"""QuickBooks and Xero journal formats, and Norma 43 to Xero / QuickBooks bank statement CSV.

- quickbooks_iif: QuickBooks Desktop IIF, GENERAL JOURNAL transactions (TRNS/SPL/ENDTRNS, tab separated).
  AMOUNT is positive for debits and negative for credits; ACCNT is the QuickBooks account (name or number).
- quickbooks_csv: QuickBooks Online "Import journal entries" CSV (Journal No., Journal Date, Account Name,
  Debits, Credits, Description, Name).
- xero_csv: Xero "Import a manual journal" CSV template (*Narration, *Date, Description, *AccountCode,
  *TaxRate, *Amount, tracking). Lines sharing Narration and Date form one journal; Amount > 0 is a debit.

Amounts are written with a decimal point and no thousands separator. Dates follow `date_style`:
mdy (US, default for QuickBooks), dmy (UK/AU/NZ, default for Xero) or iso.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any, Iterable

from .journal import Entry, JournalError, Line

FORMATS = ("quickbooks_iif", "quickbooks_csv", "xero_csv")
EXTENSIONS = {"quickbooks_iif": "journal.iif", "quickbooks_csv": "quickbooks_journal.csv",
              "xero_csv": "xero_manual_journal.csv"}
DEFAULT_STYLE = {"quickbooks_iif": "mdy", "quickbooks_csv": "mdy", "xero_csv": "dmy"}
DATE_STYLES = ("mdy", "dmy", "iso")
BANK_TARGETS = ("xero", "quickbooks")

_IIF_COLS = ["TRNSID", "TRNSTYPE", "DATE", "ACCNT", "NAME", "CLASS", "AMOUNT", "DOCNUM", "MEMO"]
_QBO_HEADER = ["Journal No.", "Journal Date", "Account Name", "Debits", "Credits", "Description", "Name"]
_XERO_HEADER = ["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount",
                "TrackingName1", "TrackingOption1", "TrackingName2", "TrackingOption2"]


def style_for(fmt: str, style: str) -> str:
    style = (style or "").strip().lower() or DEFAULT_STYLE.get(fmt, "dmy")
    if style not in DATE_STYLES:
        raise JournalError(f"date_style debe ser uno de {', '.join(DATE_STYLES)}")
    return style


# ---------------------------------------------------------------- amounts and dates (US/UK conventions)

_US = re.compile(r"^[+-]?\d+(\.\d+)?$")


def amount(raw: str, where: str = "") -> int:
    """'1,234.56' / '$1,234' / '(45.30)' / '-650' -> cents. Comma is always a thousands separator here."""
    s = (raw or "").strip()
    for ch in ("$", "£", "€", "A$", "NZ$", " ", " ", ","):
        s = s.replace(ch, "")
    if not s:
        return 0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if not _US.match(s):
        raise JournalError(f"{where}importe no válido: {raw!r}")
    sign = -1 if s.startswith("-") else 1
    whole, _, frac = s.lstrip("+-").partition(".")
    if len(frac) > 2 and frac[2:].strip("0"):
        raise JournalError(f"{where}importe con más de 2 decimales: {raw!r}")
    cents = sign * (int(whole or "0") * 100 + int((frac + "00")[:2]))
    return -cents if neg else cents


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})$")


def parse_day(raw: str, style: str, where: str = "") -> date:
    s = (raw or "").strip().split(" ")[0]
    try:
        if m := _ISO.match(s):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _SLASH.match(s):
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            y += 2000 if y < 100 else 0
            return date(y, a, b) if style == "mdy" else date(y, b, a)
    except ValueError:
        pass
    raise JournalError(f"{where}fecha no válida para date_style={style}: {raw!r}")


def day(d: date, style: str) -> str:
    if style == "iso":
        return d.isoformat()
    return f"{d.month:02d}/{d.day:02d}/{d.year}" if style == "mdy" else f"{d.day:02d}/{d.month:02d}/{d.year}"


def _clean(v: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", v or "").strip()


def _safe(v: str) -> str:
    """Spreadsheet formula injection guard for CSV cells."""
    v = _clean(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def _lines(e: Entry) -> list[Line]:
    return [ln for ln in e.lines if ln.debit or ln.credit]


def _signed(ln: Line) -> int:
    return ln.debit - ln.credit


# ---------------------------------------------------------------- QuickBooks Desktop IIF

def write_iif(entries: list[Entry], rep: Any, style: str = "mdy") -> str:
    out = ["!TRNS\t" + "\t".join(_IIF_COLS), "!SPL\t" + "\t".join(["SPLID"] + _IIF_COLS[1:]), "!ENDTRNS"]
    for e in entries:
        lines = _lines(e)
        if len(lines) < 2:
            raise JournalError(f"asiento {e.number}: QuickBooks exige al menos 2 líneas")
        for i, ln in enumerate(lines):
            doc = ln.document or (str(e.number) if e.number is not None else "")
            row = ["TRNS" if i == 0 else "SPL", "", "GENERAL JOURNAL", day(e.date, style),
                   _clean(ln.account).replace('"', "'"), "", "", money(_signed(ln)),
                   rep.fit(_clean(doc), 21, "DOCNUM"), rep.fit(_clean(ln.concept).replace('"', "'"), 4095, "MEMO")]
            out.append("\t".join(row))
        out.append("ENDTRNS")
    return "\r\n".join(out) + "\r\n"


def read_iif(text: str, rep: Any, style: str = "mdy") -> list[Entry]:
    heads: dict[str, list[str]] = {}
    entries: list[Entry] = []
    cur: Entry | None = None
    skipped = 0
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        cells = raw.split("\t")
        kind = cells[0].strip().upper()
        if kind.startswith("!"):
            heads[kind[1:]] = [c.strip().upper() for c in cells]
            continue
        if kind == "ENDTRNS":
            cur = None
            continue
        if kind not in ("TRNS", "SPL"):
            skipped += 1
            continue
        head = heads.get(kind)
        if not head:
            raise JournalError(f"línea {no}: registro {kind} sin cabecera !{kind}")
        row = dict(zip(head, cells))
        where = f"línea {no}: "
        if kind == "TRNS" or cur is None:
            if kind == "SPL":
                rep.warnings.append(f"{where}SPL sin TRNS previo; se abre un asiento nuevo")
            doc = row.get("DOCNUM", "").strip()
            cur = Entry(number=int(doc) if doc.isdigit() and len(doc) < 10 else None,
                        date=parse_day(row.get("DATE", ""), style, where))
            entries.append(cur)
        cents = amount(row.get("AMOUNT", ""), where)
        cur.lines.append(Line(account=row.get("ACCNT", "").strip(), debit=max(cents, 0), credit=max(-cents, 0),
                              concept=row.get("MEMO", "").strip(), document=row.get("DOCNUM", "").strip(),
                              account_name=row.get("NAME", "").strip(), source_line=no))
    if skipped:
        rep.warnings.append(f"{skipped} registro(s) IIF que no son TRNS/SPL ignorados (listas, clientes...)")
    if not heads.get("TRNS"):
        raise JournalError("no es un fichero IIF de QuickBooks (falta la cabecera !TRNS)")
    return entries


# ---------------------------------------------------------------- shared CSV reading

def _norm(h: str) -> str:
    return re.sub(r"[\s_.*]+", " ", (h or "").replace("﻿", "").lower()).strip()


def _rows(text: str) -> tuple[list[str], list[list[str]]]:
    first = text.splitlines()[0] if text else ""
    delim = max((",", ";", "\t"), key=first.count)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]
    if not rows:
        raise JournalError("CSV vacío")
    return [_norm(h) for h in rows[0]], rows[1:]


def _group(items: Iterable[tuple[str, date, Line, int | None]], rep: Any) -> list[Entry]:
    """Group lines by key; lines with no key are grouped consecutively until they balance."""
    entries: list[Entry] = []
    by_key: dict[tuple[str, date], Entry] = {}
    cur: Entry | None = None
    running = 0
    for key, d, ln, number in items:
        if key:
            e = by_key.get((key, d))
            if e is None:
                e = by_key[(key, d)] = Entry(number=number, date=d)
                entries.append(e)
            e.lines.append(ln)
            continue
        if cur is None or (running == 0 and cur.lines) or cur.date != d:
            cur = Entry(number=None, date=d)
            entries.append(cur)
            running = 0
        cur.lines.append(ln)
        running += _signed(ln)
    return entries


def _col(head: list[str], *names: str) -> int | None:
    return next((head.index(n) for n in names if n in head), None)


def _cell(r: list[str], i: int | None) -> str:
    return r[i].strip() if i is not None and i < len(r) else ""


# ---------------------------------------------------------------- QuickBooks Online CSV

def write_qbo_csv(entries: list[Entry], rep: Any, style: str = "mdy") -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(_QBO_HEADER)
    for i, e in enumerate(entries, 1):
        no = str(e.number if e.number is not None else i)
        for ln in _lines(e):
            w.writerow([no, day(e.date, style), _safe(ln.account), money(ln.debit) if ln.debit else "",
                        money(ln.credit) if ln.credit else "", _safe(ln.concept), _safe(ln.account_name)])
    return buf.getvalue()


def read_qbo_csv(text: str, rep: Any, style: str = "mdy") -> list[Entry]:
    head, rows = _rows(text)
    c_no = _col(head, "journal no", "journalno", "journal number", "entry no")
    c_date = _col(head, "journal date", "journaldate", "date")
    c_acc = _col(head, "account name", "accountname", "account")
    c_deb, c_cred = _col(head, "debits", "debit"), _col(head, "credits", "credit")
    c_desc, c_name = _col(head, "description", "memo"), _col(head, "name")
    if None in (c_date, c_acc, c_deb, c_cred):
        raise JournalError("CSV de QuickBooks: faltan columnas Journal Date, Account Name, Debits o Credits")

    def items() -> Iterable[tuple[str, date, Line, int | None]]:
        for no, r in enumerate(rows, 2):
            where = f"fila {no}: "
            key = _cell(r, c_no)
            ln = Line(account=_cell(r, c_acc), debit=amount(_cell(r, c_deb), where),
                      credit=amount(_cell(r, c_cred), where), concept=_cell(r, c_desc),
                      account_name=_cell(r, c_name), source_line=no)
            yield key, parse_day(_cell(r, c_date), style, where), ln, int(key) if key.isdigit() and len(key) < 10 else None

    return _group(items(), rep)


# ---------------------------------------------------------------- Xero manual journal CSV

def write_xero_csv(entries: list[Entry], rep: Any, style: str = "dmy", tax_rate: str = "No VAT") -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(_XERO_HEADER)
    for i, e in enumerate(entries, 1):
        lines = _lines(e)
        first = next((ln.concept for ln in lines if ln.concept), "")
        narration = rep.fit(_safe(f"#{e.number if e.number is not None else i} {first}".strip()), 4000, "Narration")
        for ln in lines:
            w.writerow([narration, day(e.date, style), _safe(ln.concept), _safe(ln.account), tax_rate,
                        money(_signed(ln)), "", "", "", ""])
    return buf.getvalue()


_NARR_NO = re.compile(r"^#(\d{1,9})\b")


def read_xero_csv(text: str, rep: Any, style: str = "dmy") -> list[Entry]:
    head, rows = _rows(text)
    c_narr, c_date = _col(head, "narration"), _col(head, "date")
    c_desc, c_acc = _col(head, "description"), _col(head, "accountcode", "account code", "account")
    c_amt = _col(head, "amount")
    c_deb, c_cred = _col(head, "debit"), _col(head, "credit")
    if c_date is None or c_acc is None or (c_amt is None and c_deb is None):
        raise JournalError("CSV de Xero: faltan columnas Date, AccountCode o Amount")

    def items() -> Iterable[tuple[str, date, Line, int | None]]:
        for no, r in enumerate(rows, 2):
            where = f"fila {no}: "
            if c_amt is not None:
                cents = amount(_cell(r, c_amt), where)
                debit, credit = max(cents, 0), max(-cents, 0)
            else:
                debit, credit = amount(_cell(r, c_deb), where), amount(_cell(r, c_cred), where)
            narr = _cell(r, c_narr)
            m = _NARR_NO.match(narr)
            concept = _cell(r, c_desc) or _NARR_NO.sub("", narr).strip()
            yield (narr or "", parse_day(_cell(r, c_date), style, where),
                   Line(account=_cell(r, c_acc), debit=debit, credit=credit, concept=concept, source_line=no),
                   int(m[1]) if m else None)

    return _group(items(), rep)


# ---------------------------------------------------------------- detection and dispatch

def detect(lines: list[str]) -> str | None:
    if any(ln.lstrip("﻿").upper().startswith("!TRNS") for ln in lines):
        return "quickbooks_iif"
    head = _norm(lines[0]) if lines else ""
    if "narration" in head and "accountcode" in head.replace(" ", ""):
        return "xero_csv"
    if "journal no" in head and "journal date" in head:
        return "quickbooks_csv"
    return None


def read(text: str, fmt: str, rep: Any, style: str = "") -> list[Entry]:
    st = style_for(fmt, style)
    if fmt == "quickbooks_iif":
        return read_iif(text, rep, st)
    if fmt == "quickbooks_csv":
        return read_qbo_csv(text, rep, st)
    return read_xero_csv(text, rep, st)


def write(entries: list[Entry], fmt: str, rep: Any, style: str = "", tax_rate: str = "") -> str:
    st = style_for(fmt, style)
    if fmt == "quickbooks_iif":
        return write_iif(entries, rep, st)
    if fmt == "quickbooks_csv":
        return write_qbo_csv(entries, rep, st)
    return write_xero_csv(entries, rep, st, (tax_rate or "No VAT").strip())


# ---------------------------------------------------------------- Norma 43 -> bank statement CSV

def bank_statement(res: Any, target: str, style: str = "", account_filter: str = "") -> tuple[str, dict[str, Any]]:
    """Xero precoded bank statement CSV or QuickBooks Online 3-column bank CSV from a parsed Norma 43 file."""
    if target not in BANK_TARGETS:
        raise JournalError(f"target debe ser uno de {', '.join(BANK_TARGETS)}")
    st = (style or "").strip().lower() or ("mdy" if target == "quickbooks" else "dmy")
    if st not in DATE_STYLES:
        raise JournalError(f"date_style debe ser uno de {', '.join(DATE_STYLES)}")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["*Date", "*Amount", "Payee", "Description", "Reference", "Cheque Number"] if target == "xero"
               else ["Date", "Description", "Amount"])
    accounts, count, total = [], 0, 0
    flt = re.sub(r"\D", "", account_filter or "")
    for acc in res.accounts:
        ident = f"{acc.bank}{acc.branch}{acc.number}"
        if flt and not ident.endswith(flt[-10:]):
            continue
        accounts.append({"account": acc.ccc or ident, "holder": acc.holder, "movements": len(acc.movements)})
        for m in acc.movements:
            when = m.operation_date or m.value_date
            if not when:
                raise JournalError(f"movimiento {m.id} sin fecha")
            d = date.fromisoformat(when[:10])
            desc = _safe(m.description())
            ref = _safe((m.reference2 or m.reference1 or m.document_number or "").lstrip("0"))
            payee = _safe((m.sepa or {}).get("ordenante_nombre") or (m.sepa or {}).get("acreedor_nombre") or "")
            if target == "xero":
                w.writerow([day(d, st), money(m.cents), payee, desc, ref, ""])
            else:
                w.writerow([day(d, st), " ".join(x for x in (payee, desc) if x) or ref, money(m.cents)])
            count += 1
            total += m.cents
    if flt and not accounts:
        raise JournalError(f"ninguna cuenta del extracto termina en {account_filter!r}")
    return buf.getvalue(), {"accounts": accounts, "movements": count, "net": money(total), "date_style": st}
