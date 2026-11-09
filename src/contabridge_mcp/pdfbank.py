"""Deterministic reader for digital bank statement PDFs (selectable text, no OCR, no AI).

The PDF text is laid out with pypdf's layout mode, the movements table is found from its header row, and every
row's amount and balance are read from their columns. Each row is then checked against the running balance:
previous balance + amount must equal the printed balance. A row that does not add up is flagged, never fixed;
a row whose sign cannot be proven from its column or from the balance goes to review and is not booked.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import ids, n43
from .journal import JournalError

MAX_PAGES = 200
MAX_ROWS = 20_000

_AMOUNT = re.compile(
    r"^(?:EUR|€|\$|£)?\s*(?P<pre>[+\-−])?\s*(?:EUR|€|\$|£)?\s*\(?(?P<num>\d{1,3}(?:[.,  ']\d{3})*[.,]\d{2}|\d+[.,]\d{2})\)?"
    r"\s*(?P<post>-|−)?\s*(?:EUR|€|\$|£)?\s*(?P<dc>D|C|DR|CR|H)?$", re.I)
_MONTHS = {m: i for i, names in enumerate((
    ("ene", "enero", "jan", "january"), ("feb", "febrero", "february"), ("mar", "marzo", "march"),
    ("abr", "abril", "apr", "april"), ("may", "mayo"), ("jun", "junio", "june"), ("jul", "julio", "july"),
    ("ago", "agosto", "aug", "august"), ("sep", "sept", "septiembre", "september", "set"),
    ("oct", "octubre", "october"), ("nov", "noviembre", "november"), ("dic", "diciembre", "dec", "december")), 1)
    for m in names}
_MON = r"([A-Za-zÀ-ÿ]{3,10})\.?"
_DATES = (
    (re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})\b"), ("d", "m", "y")),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    (re.compile(rf"^(\d{{1,2}})(?:\s+de)?[\s\-]+{_MON}(?:\s+de)?[\s\-]+(\d{{4}})\b"), ("d", "M", "y")),
    (re.compile(rf"^{_MON}\s+(\d{{1,2}}),?\s+(\d{{4}})\b"), ("M", "d", "y")),
)
_ROLE_WORDS = {
    "date": ("fecha", "f. operacion", "f.operacion", "fecha operacion", "date", "completed date", "booking date",
             "fecha contable", "data"),
    "value": ("f. valor", "f.valor", "fecha valor", "valor", "value date"),
    "debit": ("cargo", "cargos", "debe", "debito", "debit", "debits", "money out", "paid out", "salidas", "pagos",
              "withdrawals", "reintegros"),
    "credit": ("abono", "abonos", "haber", "credito", "credit", "credits", "money in", "paid in", "entradas",
               "ingresos", "deposits"),
    "amount": ("importe", "importe eur", "importe (eur)", "amount", "cantidad", "movimiento", "importe en eur"),
    "balance": ("saldo", "balance", "saldo eur", "saldo (eur)", "disponible", "saldo contable"),
    "concept": ("concepto", "descripcion", "description", "detalle", "movimiento", "concepto/referencia",
                "reference", "referencia", "details"),
}
_OPENING = re.compile(r"\b(saldo (inicial|anterior|de apertura|al inicio)|opening balance|balance brought forward|"
                      r"saldo a \d|starting balance)\b", re.I)
_CLOSING = re.compile(r"\b(saldo (final|actual|de cierre|al cierre)|closing balance|ending balance|"
                      r"balance carried forward)\b", re.I)
_BANKS = ("CaixaBank", "BBVA", "Santander", "Sabadell", "Bankinter", "ING", "Unicaja", "Kutxabank", "Abanca",
          "Ibercaja", "Cajamar", "Openbank", "Revolut", "Wise", "N26", "Qonto", "Deutsche Bank", "EVO", "Caja Rural",
          "Banca March", "Triodos", "MyInvestor", "Banco Mediolanum", "Arquia", "Laboral Kutxa", "Stripe", "PayPal")


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.strip().lower())


def parse_amount_token(tok: str) -> tuple[int, int | None] | None:
    """'1.234,56' -> (123456, None); '-12,00' / '12,00-' / '(12.00)' / '12,00 D' -> (1200, -1). None if not money."""
    m = _AMOUNT.match(tok.strip())
    if not m:
        return None
    num = re.sub(r"[  ']", "", m["num"])
    dec = num[-3]
    whole = num[:-3].replace("." if dec == "," else ",", "")
    if not whole.isdigit() and whole:
        return None
    cents = int(whole or "0") * 100 + int(num[-2:])
    sign = None
    if m["pre"] in ("-", "−") or m["post"] or tok.strip().startswith("(") or (m["dc"] or "").upper() in ("D", "DR"):
        sign = -1
    elif m["pre"] == "+" or (m["dc"] or "").upper() in ("C", "CR", "H"):
        sign = 1
    return cents, sign


def parse_date_token(tok: str, default_year: int | None = None) -> tuple[date, int] | None:
    """Date at the start of tok -> (date, characters used)."""
    s = tok.strip()
    for rx, order in _DATES:
        mt = rx.match(s)
        if not mt:
            continue
        parts = dict(zip(order, mt.groups()))
        if "M" in parts:
            mon = _MONTHS.get(_plain(parts["M"]).rstrip("."))
            if not mon:
                continue
        else:
            mon = int(parts["m"])
        year = int(parts["y"]) + (2000 if len(parts["y"]) == 2 else 0)
        try:
            return date(year, mon, int(parts["d"])), mt.end()
        except ValueError:
            return None
    return None


@dataclass
class Cell:
    start: int
    end: int
    text: str

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2


def _cells(line: str) -> list[Cell]:
    return [Cell(m.start(), m.end(), m.group()) for m in re.finditer(r"\S+(?: \S+)*", line)]


@dataclass
class Row:
    page: int
    line: int
    when: date
    value: date | None
    concept: str
    amount: int | None = None  # unsigned cents as printed
    sign: int | None = None  # from column / printed sign; None = unknown
    balance: int | None = None
    status: str = "ok"
    issue: str = ""
    cents: int | None = None  # signed, once resolved
    col: str = ""
    extra: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"page": self.page, "line": self.line, "date": self.when.isoformat(),
                "value_date": (self.value or self.when).isoformat(), "concept": self.text(),
                "amount": n43.fmt_cents(self.cents) if self.cents is not None else None,
                "printed_amount": n43.fmt_cents(self.amount) if self.amount is not None else None,
                "balance": n43.fmt_cents(self.balance) if self.balance is not None else None,
                "status": self.status, **({"issue": self.issue} if self.issue else {})}

    def text(self) -> str:
        return " ".join([self.concept, *self.extra]).strip()[:500]


@dataclass
class Statement:
    rows: list[Row]
    opening: int | None
    closing: int | None
    iban: str | None
    bank: str | None
    holder_hint: str
    columns: list[str]
    pages: int
    skipped: list[dict[str, Any]]
    descending: bool = False
    checks: dict[str, Any] = field(default_factory=dict)


def extract_lines(raw: bytes) -> list[list[str]]:
    """Text lines per page, laid out so columns keep their horizontal positions."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover
        raise JournalError("falta la dependencia pypdf: pip install 'contabridge-mcp[pdf]' o pip install pypdf") from exc
    if not raw.startswith(b"%PDF"):
        raise JournalError("el fichero no es un PDF (no empieza por %PDF)")
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise JournalError("el PDF está protegido con contraseña; ábrelo y guárdalo sin contraseña")
            except NotImplementedError as exc:
                raise JournalError("el PDF usa un cifrado no soportado; guárdalo sin protección") from exc
        if len(reader.pages) > MAX_PAGES:
            raise JournalError(f"el PDF tiene {len(reader.pages)} páginas; el máximo es {MAX_PAGES}")
        pages = [(p.extract_text(extraction_mode="layout") or "").splitlines() for p in reader.pages]
    except JournalError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise JournalError(f"no se puede leer el PDF: {exc}") from exc
    if not any(ln.strip() for pg in pages for ln in pg):
        raise JournalError("el PDF no tiene texto seleccionable (¿escaneado?). Solo se leen extractos digitales; "
                           "descarga el extracto desde la banca online o pide el fichero Norma 43")
    return pages


def _header_roles(cells: list[Cell]) -> dict[str, Cell] | None:
    roles: dict[str, Cell] = {}
    for c in cells:
        t = _plain(c.text).rstrip(":")
        for role, words in _ROLE_WORDS.items():
            if t in words and role not in roles:
                if role == "concept" and t == "movimiento" and "amount" in roles:
                    continue
                roles[role] = c
                break
    has_money = "balance" in roles or "amount" in roles or ("debit" in roles and "credit" in roles)
    if "date" in roles and has_money and ("amount" in roles or "debit" in roles or "credit" in roles):
        return roles
    return None


def _assign(cells: list[Cell], roles: dict[str, Cell]) -> dict[str, Cell]:
    """Map trailing amount cells to the nearest money column of the header."""
    money = {r: c for r, c in roles.items() if r in ("debit", "credit", "amount", "balance")}
    out: dict[str, Cell] = {}
    for c in cells:
        best, dist = None, 10 ** 9
        for role, h in money.items():
            d = 0 if c.start <= h.end and h.start <= c.end else min(abs(c.start - h.end), abs(h.start - c.end))
            d2 = abs(c.mid - h.mid)
            score = d * 1000 + d2
            if score < dist:
                best, dist = role, score
        if best and best not in out:
            out[best] = c
        elif best:
            raise JournalError("dos importes en la misma columna")
    return out


def _labelled_amount(line: str) -> int | None:
    vals = [parse_amount_token(c.text) for c in _cells(line)]
    vals = [v for v in vals if v]
    if not vals:
        tail = re.findall(r"[+\-−]?\d[\d.,  ']*[.,]\d{2}-?", line)
        vals = [v for v in (parse_amount_token(t) for t in tail) if v]
    if not vals:
        return None
    cents, sign = vals[-1]
    return cents * (sign or 1)


def read_statement(raw: bytes) -> Statement:
    pages = extract_lines(raw)
    rows: list[Row] = []
    skipped: list[dict[str, Any]] = []
    opening = closing = None
    iban = None
    roles: dict[str, Cell] | None = None
    columns: list[str] = []
    full = "\n".join(ln for pg in pages for ln in pg)
    for m in re.finditer(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,4})?)\b", full):
        cand = m.group(1).replace(" ", "")
        if ids.validate_iban(cand).get("valid"):
            iban = cand
            break
    plain_full = _plain(full)
    bank = next((b for b in _BANKS if re.search(rf"\b{re.escape(_plain(b))}\b", plain_full)), None)
    last: Row | None = None
    for pno, lines in enumerate(pages, 1):
        last = None
        for lno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            cells = _cells(line)
            hdr = _header_roles(cells)
            if hdr:
                roles, last = hdr, None
                columns = [r for r, _ in sorted(hdr.items(), key=lambda kv: kv[1].start)]
                continue
            stripped = line.strip()
            label = _plain(stripped)
            got = parse_date_token(stripped)
            is_open = _OPENING.search(label)
            is_close = _CLOSING.search(label)
            if (is_open or is_close) and not (got and not (is_open or is_close)):
                val = _labelled_amount(stripped)
                if val is not None:
                    if is_open and opening is None and not rows:
                        opening = val
                    elif is_close:
                        closing = val
                    last = None
                    continue
            if not got or roles is None:
                if last is not None and not any(parse_amount_token(c.text) for c in cells) \
                        and not re.search(r"\b(pagina|page|total|suma|hoja)\b", label):
                    last.extra.append(stripped)
                elif roles is not None and any(parse_amount_token(c.text) for c in cells) and not got:
                    if not re.search(r"\b(total|suma|sumas|totales|subtotal)\b", label):
                        skipped.append({"page": pno, "line": lno, "text": stripped[:160]})
                    last = None
                continue
            when, used = got
            offset = len(line) - len(line.lstrip()) + used
            rest = line[offset:]
            value = None
            m2 = parse_date_token(rest.strip())
            if m2:
                value = m2[0]
                rest_off = offset + (len(rest) - len(rest.lstrip())) + m2[1]
                rest = line[rest_off:]
                offset = rest_off
            tail_cells = [Cell(c.start + offset, c.end + offset, c.text) for c in _cells(rest)]
            money: list[Cell] = []
            while tail_cells and parse_amount_token(tail_cells[-1].text):
                money.insert(0, tail_cells.pop())
            concept = " ".join(c.text for c in tail_cells)
            if not money:
                if _OPENING.search(_plain(concept)) or _CLOSING.search(_plain(concept)):
                    continue
                skipped.append({"page": pno, "line": lno, "text": stripped[:160]})
                last = None
                continue
            row = Row(page=pno, line=lno, when=when, value=value, concept=concept)
            try:
                mapped = _assign(money, roles)
            except JournalError:
                mapped = {}
            if not mapped or len(mapped) != len(money):
                if len(money) >= 2:
                    mapped = {"amount": money[-2], "balance": money[-1]}
                else:
                    mapped = {"amount": money[0]}
            if "balance" in mapped:
                b = parse_amount_token(mapped["balance"].text)
                row.balance = b[0] * (b[1] or 1)
            amt_roles = [r for r in ("debit", "credit", "amount") if r in mapped]
            if len(amt_roles) != 1:
                row.status, row.issue = "review", "no se identifica una única columna de importe en la fila"
            else:
                r = amt_roles[0]
                cents, sign = parse_amount_token(mapped[r].text)
                row.amount, row.col = cents, r
                if r == "debit":
                    row.sign = -1
                elif r == "credit":
                    row.sign = 1
                else:
                    row.sign = sign
            if _OPENING.search(_plain(concept)) and row.balance is not None and not rows and row.amount is None:
                opening = row.balance
                continue
            rows.append(row)
            last = row
            if len(rows) > MAX_ROWS:
                raise JournalError(f"máximo {MAX_ROWS} movimientos por PDF")
    if roles is None:
        raise JournalError("no se encuentra la cabecera de la tabla de movimientos (Fecha, Concepto, Importe/Cargo/"
                           "Abono, Saldo). Este formato de PDF no se puede leer de forma fiable")
    if not rows:
        raise JournalError("no se encontraron movimientos en el PDF")
    head = next((ln.strip() for pg in pages for ln in pg if ln.strip()), "")
    st = Statement(rows=rows, opening=opening, closing=closing, iban=iban, bank=bank, holder_hint=head[:80],
                   columns=columns, pages=len(pages), skipped=skipped[:100])
    _verify(st)
    return st


def _chain(rows: list[Row], opening: int | None) -> int:
    """Resolve signs and check balances in place; returns the number of rows that are not ok."""
    prev = opening
    bad = 0
    for i, r in enumerate(rows):
        r.cents, r.issue = None, r.issue if r.status == "review" and r.amount is None else ""
        if r.status == "review" and r.amount is None:
            bad += 1
            prev = r.balance if r.balance is not None else None
            continue
        r.status = "ok"
        if prev is None and r.balance is not None and r.sign is not None:
            prev = r.balance - r.sign * r.amount
        if r.sign is None:
            if prev is not None and r.balance is not None:
                if prev + r.amount == r.balance:
                    r.cents = r.amount
                elif prev - r.amount == r.balance:
                    r.cents = -r.amount
            if r.cents is None:
                r.status = "review"
                r.issue = "no se puede saber si es cargo o abono: el saldo no lo confirma"
                bad += 1
                prev = r.balance if r.balance is not None else None
                continue
        else:
            r.cents = r.sign * r.amount
        if r.balance is not None and prev is not None and prev + r.cents != r.balance:
            r.status = "mismatch"
            r.issue = (f"no cuadra: saldo anterior {n43.fmt_cents(prev)} + importe {n43.fmt_cents(r.cents)} = "
                       f"{n43.fmt_cents(prev + r.cents)}, pero el PDF indica {n43.fmt_cents(r.balance)}")
            bad += 1
        if r.balance is not None:
            prev = r.balance
        elif prev is not None:
            prev += r.cents
    return bad


def _verify(st: Statement) -> None:
    rows = st.rows
    # A single Importe column that prints minus signs is a signed column: there, no sign means money in.
    if any(r.col == "amount" and r.sign == -1 for r in rows):
        for r in rows:
            if r.col == "amount" and r.sign is None:
                r.sign = 1
    first, last = rows[0].when, rows[-1].when
    candidates = [False, True] if first <= last else [True, False]
    best = None
    for desc in candidates:
        seq = list(reversed(rows)) if desc else list(rows)
        bad = _chain(seq, st.opening)
        if best is None or bad < best[0]:
            best = (bad, desc)
        if bad == 0:
            break
    bad, desc = best
    st.descending = desc
    if desc:
        st.rows = list(reversed(rows))
    _chain(st.rows, st.opening)
    rows = st.rows
    booked = [r for r in rows if r.cents is not None]
    total_in = sum(r.cents for r in booked if r.cents > 0)
    total_out = -sum(r.cents for r in booked if r.cents < 0)
    opening = st.opening
    if opening is None:
        first_ok = next((r for r in rows if r.cents is not None and r.balance is not None), None)
        if first_ok is not None and first_ok is rows[0]:
            opening = first_ok.balance - first_ok.cents
    computed = opening + total_in - total_out if opening is not None else None
    printed = st.closing if st.closing is not None else next((r.balance for r in reversed(rows)
                                                              if r.balance is not None), None)
    st.checks = {
        "rows": len(rows),
        "ok": sum(r.status == "ok" for r in rows),
        "mismatch": sum(r.status == "mismatch" for r in rows),
        "review": sum(r.status == "review" for r in rows),
        "opening_balance": n43.fmt_cents(opening) if opening is not None else None,
        "opening_source": "pdf" if st.opening is not None else ("derived from first row" if opening is not None
                                                                  else None),
        "total_in": n43.fmt_cents(total_in),
        "total_out": n43.fmt_cents(total_out),
        "computed_closing": n43.fmt_cents(computed) if computed is not None else None,
        "printed_closing": n43.fmt_cents(printed) if printed is not None else None,
        "closing_source": "pdf" if st.closing is not None else ("last row balance" if printed is not None else None),
        "closing_ok": computed is not None and printed is not None and computed == printed,
        "order": "descending in PDF (reversed)" if st.descending else "ascending",
    }
    st.checks["balanced"] = (st.checks["closing_ok"] and not st.checks["mismatch"] and not st.checks["review"])
    st.opening = opening


def to_parse_result(st: Statement) -> n43.ParseResult:
    """Only rows with a proven sign become movements; review rows are left out (and listed by the tool)."""
    movs = []
    for i, r in enumerate(x for x in st.rows if x.cents):
        movs.append(n43.Movement(id=f"p{r.page}l{r.line}", line=i + 1, office="", operation_date=r.when.isoformat(),
                                 value_date=(r.value or r.when).isoformat(), common_concept="", own_concept="",
                                 cents=r.cents, document_number="", reference1="", reference2="",
                                 complementary=[{"text": r.text()}] if r.text() else [],
                                 balance_after=r.balance or 0, is_debit=r.cents < 0))
    bank_code, branch, number = _ccc(st.iban)
    acc = n43.Account(index=0, line=0, bank=bank_code, branch=branch, number=number, start=None, end=None,
                      opening=st.opening or 0, currency_code="978", mode="3", holder="", movements=movs)
    return n43.ParseResult(accounts=[acc], issues=[], record_count=len(movs), has_end_record=True)


def _ccc(iban: str | None) -> tuple[str, str, str]:
    if iban and iban.startswith("ES") and len(iban) == 24:
        return iban[4:8], iban[8:12], iban[14:24]
    return "", "", ""


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("ñ", "\x00").replace("Ñ", "\x01"))
    s = s.encode("ascii", "ignore").decode().replace("\x00", "Ñ").replace("\x01", "Ñ")
    return re.sub(r"\s+", " ", s).upper()


def to_norma43(st: Statement, holder: str = "", ccc: str = "") -> tuple[str, list[str]]:
    """Write the booked rows as a Cuaderno 43 file (records 11, 22, 23, 33, 88), CRLF, 80 columns."""
    warnings = []
    digits = re.sub(r"\D", "", ccc)
    if digits:
        if len(digits) != 20:
            raise JournalError("account_ccc debe tener 20 dígitos (entidad, oficina, DC y cuenta)")
        bank_code, branch, number = digits[:4], digits[4:8], digits[10:]
    else:
        bank_code, branch, number = _ccc(st.iban)
    if not bank_code:
        bank_code, branch, number = "0000", "0000", "0000000000"
        warnings.append("sin IBAN español en el PDF: la cuenta del registro 11 va a ceros; indica account_ccc")
    rows = [r for r in st.rows if r.cents]
    if not rows:
        raise JournalError("no hay movimientos verificados para escribir el Norma 43")
    opening = st.opening if st.opening is not None else 0
    start, end = rows[0].when, rows[-1].when

    def ymd(d: date) -> str:
        return d.strftime("%y%m%d")

    def pad(s: str, n: int) -> str:
        return f"{s:<{n}}"[:n]

    out = ["11" + bank_code + branch + number + ymd(start) + ymd(end) + ("1" if opening < 0 else "2")
           + f"{abs(opening):014d}" + "978" + "3" + pad(_ascii(holder), 26) + "   "]
    for r in rows:
        out.append("22" + "    " + branch + ymd(r.when) + ymd(r.value or r.when) + "99" + "000"
                   + ("1" if r.cents < 0 else "2") + f"{abs(r.cents):014d}" + "0" * 10 + "0" * 12 + " " * 16)
        text = _ascii(r.text())
        chunks = [text[i:i + 38] for i in range(0, min(len(text), 380), 38)]
        for k in range(0, len(chunks), 2):
            out.append("23" + f"{k // 2 + 1:02d}" + pad(chunks[k], 38) + pad(chunks[k + 1] if k + 1 < len(chunks)
                                                                             else "", 38))
    debits = [-r.cents for r in rows if r.cents < 0]
    credits = [r.cents for r in rows if r.cents > 0]
    final = opening + sum(credits) - sum(debits)
    out.append("33" + bank_code + branch + number + f"{len(debits):05d}" + f"{sum(debits):014d}"
               + f"{len(credits):05d}" + f"{sum(credits):014d}" + ("1" if final < 0 else "2")
               + f"{abs(final):014d}" + "978" + "    ")
    out.append("88" + "9" * 18 + f"{len(out):06d}" + " " * 54)
    assert all(len(x) == 80 for x in out)
    return "\r\n".join(out) + "\r\n", warnings


def summary(st: Statement, max_rows: int = 500) -> dict[str, Any]:
    flagged = [r.as_dict() for r in st.rows if r.status != "ok"]
    return {"bank_detected": st.bank, "iban": st.iban, "pages": st.pages, "columns": st.columns,
            "balance_check": st.checks, "flagged_rows": flagged[:200], "flagged_total": len(flagged),
            "rows": [r.as_dict() for r in st.rows[:max_rows]], "rows_total": len(st.rows),
            "skipped_lines": st.skipped,
            "note": "Lectura determinista del texto del PDF: no se usa OCR ni IA y no se corrige ningún importe."}
