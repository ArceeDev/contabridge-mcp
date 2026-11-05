"""Readers and writers for Spanish accounting import formats.

- a3: a3ASESOR/a3ECO/a3CON 'Enlace contable de entrada' SUENLACE.DAT, record type 0 (apuntes sin IVA).
  Format 5 (512-byte records, current) and format 4 (256-byte records, older versions).
- contaplus_csv: Sage ContaPlus 'Protocolo de comunicación' Diario in CSV (142 fields, ';'). Also read by Sage 50.
- contaplus_ascii: the same Diario as fixed-width ASCII (XDiario.txt), first 32 fields (same layout BulmaGés writes).
- csv: generic spreadsheet CSV with a header (Holded, Excel, any ERP export). Columns are auto-detected.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any, Callable

from .journal import Entry, JournalError, Line, parse_amount, parse_date

FORMATS = ("a3", "contaplus_csv", "contaplus_ascii", "csv")
EXTENSIONS = {"a3": "SUENLACE.DAT", "contaplus_csv": "XDiario.csv", "contaplus_ascii": "XDiario.txt",
              "csv": "diario.csv"}


class Report:
    """Warnings collected while reading or writing (truncations, skipped records)."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self._truncated: dict[str, int] = {}

    def fit(self, value: str, n: int, label: str) -> str:
        if len(value) > n:
            self._truncated[label] = self._truncated.get(label, 0) + 1
        return value[:n]

    def done(self) -> list[str]:
        for label, count in self._truncated.items():
            self.warnings.append(f"{count} valor(es) de '{label}' recortados a la longitud del formato")
        self._truncated.clear()
        return self.warnings


def _latin(text: str) -> str:
    """Characters outside Windows-1252 are replaced so the file can be written in the encoding these programs read."""
    return text.encode("cp1252", "replace").decode("cp1252").replace("\r", " ").replace("\n", " ")


def _dot_amount(raw: str, where: str) -> int:
    s = raw.strip()
    if not s:
        return 0
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        raise JournalError(f"{where}importe no válido: {raw!r}")
    return parse_amount(s, where)


def _ymd(raw: str, where: str) -> date:
    s = raw.strip()
    if not re.fullmatch(r"\d{8}", s):
        raise JournalError(f"{where}fecha AAAAMMDD no válida: {raw!r}")
    return parse_date(s, where)


# ---------------------------------------------------------------- A3 (SUENLACE.DAT)

def _a3_amount(cents: int) -> str:
    sign = "-" if cents < 0 else "+"
    a = abs(cents)
    if a >= 10 ** 12:
        raise JournalError(f"importe {a / 100} excede el máximo de A3 (10 enteros)")
    return f"{sign}{a // 100:010d}.{a % 100:02d}"


def write_a3(entries: list[Entry], rep: Report, company_code: str = "00001", a3_format: int = 5) -> str:
    if a3_format not in (4, 5):
        raise JournalError("a3_format debe ser 5 (registros de 512, versiones actuales) o 4 (256)")
    if not (company_code.isdigit() and 1 <= int(company_code) <= 99999):
        raise JournalError("company_code de A3 debe ser un número de 1 a 99999")
    emp = f"{int(company_code):05d}"
    rows: list[str] = []
    for e in entries:
        lines = [ln for ln in e.lines if ln.debit or ln.credit]
        if len(lines) < 2:
            raise JournalError(f"asiento {e.number}: A3 exige al menos 2 apuntes (I ... U)")
        for i, ln in enumerate(lines):
            pos = "I" if i == 0 else "U" if i == len(lines) - 1 else "M"
            dh, amount = ("D", ln.debit) if ln.debit else ("H", ln.credit)
            rec = (str(a3_format) + emp + e.date.strftime("%Y%m%d") + "0"
                   + f"{rep.fit(ln.account, 12, 'cuenta'):<12}"
                   + f"{rep.fit(_latin(ln.account_name), 30, 'descripción de cuenta'):<30}"
                   + dh + f"{rep.fit(_latin(ln.document), 10, 'documento'):<10}" + pos
                   + f"{rep.fit(_latin(ln.concept), 30, 'concepto'):<30}" + _a3_amount(amount))
            if a3_format == 4:
                rec += " " * 138 + " " + "E" + "N"
                assert len(rec) == 254
            else:
                rec += " " * 137 + " " + " " + " " * 256 + "E" + "N"
                assert len(rec) == 510
            rows.append(rec)
    return "\r\n".join(rows) + "\r\n"


def read_a3(text: str, rep: Report) -> list[Entry]:
    entries: list[Entry] = []
    cur: Entry | None = None
    skipped: dict[str, int] = {}
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        where = f"línea {no}: "
        rec = raw.ljust(113)
        if rec[0] not in "45":
            raise JournalError(f"{where}no es un registro SUENLACE (posición 1 debe ser 4 o 5)")
        kind = rec[14]
        if kind != "0":
            skipped[kind] = skipped.get(kind, 0) + 1
            continue
        d = _ymd(rec[6:14], where)
        dh, pos = rec[57], rec[68]
        if dh not in "DH" or pos not in "IMU":
            raise JournalError(f"{where}tipo de importe (D/H) o línea de apunte (I/M/U) no válidos")
        amount = _dot_amount(rec[99:113], where)
        ln = Line(account=rec[15:27].strip(), account_name=rec[27:57].strip(), document=rec[58:68].strip(),
                  concept=rec[69:99].strip(), debit=amount if dh == "D" else 0, credit=amount if dh == "H" else 0,
                  source_line=no)
        if pos == "I":
            if cur is not None:
                raise JournalError(f"{where}nuevo asiento (I) sin cerrar el anterior con U")
            cur = Entry(number=None, date=d)
        elif cur is None:
            raise JournalError(f"{where}apunte {pos} sin inicio de asiento (I)")
        cur.lines.append(ln)
        if pos == "U":
            entries.append(cur)
            cur = None
    if cur is not None:
        raise JournalError("el fichero termina con un asiento sin cerrar (falta la línea U)")
    names = {"1": "facturas", "2": "rectificativas", "9": "detalle IVA", "C": "cuentas", "V": "vencimientos"}
    for k, n in skipped.items():
        rep.warnings.append(f"{n} registro(s) tipo {k} ({names.get(k, 'otros')}) ignorados: solo se leen apuntes tipo 0")
    return entries


# ---------------------------------------------------------------- ContaPlus

CP_FIELDS = 142
# (index 0-based, name, width) of the Diario fields this tool reads and writes; widths are the XBase lengths.
_CP_LAYOUT = [("Asien", 6), ("Fecha", 8), ("SubCta", 12), ("Contra", 12), ("PtaDebe", 16), ("Concepto", 25),
              ("PtaHaber", 16), ("Factura", 8), ("Baseimpo", 16), ("IVA", 5), ("Recequiv", 5), ("Documento", 10),
              ("Departa", 3), ("Clave", 6), ("Estado", 1), ("NCasado", 6), ("TCasado", 1), ("Trans", 6),
              ("Cambio", 16), ("DebeME", 16), ("HaberME", 16), ("Auxiliar", 1), ("Serie", 1), ("Sucursal", 4),
              ("CodDivisa", 5), ("ImpAuxME", 16), ("MonedaUso", 1), ("EuroDebe", 16), ("EuroHaber", 16),
              ("BaseEuro", 16), ("NoConv", 1), ("NumeroInv", 10)]
_CP_IDX = {name: i for i, (name, _) in enumerate(_CP_LAYOUT)}
_CP_NUMERIC = {"Asien", "PtaDebe", "PtaHaber", "Factura", "Baseimpo", "IVA", "Recequiv", "NCasado", "TCasado",
               "Trans", "Cambio", "DebeME", "HaberME", "ImpAuxME", "EuroDebe", "EuroHaber", "BaseEuro"}


def _contra(e: Entry, ln: Line) -> str:
    """Contrapartida: the other account when the entry has exactly two lines."""
    others = [o.account for o in e.lines if o is not ln]
    return others[0] if len(e.lines) == 2 else ""


def _cp_values(e: Entry, ln: Line, rep: Report) -> dict[str, str]:
    return {"Asien": str(e.number), "Fecha": e.date.strftime("%Y%m%d"), "SubCta": rep.fit(ln.account, 12, "cuenta"),
            "Contra": _contra(e, ln)[:12], "Concepto": rep.fit(_latin(ln.concept), 25, "concepto"),
            "Documento": rep.fit(_latin(ln.document), 10, "documento"), "MonedaUso": "2",
            "EuroDebe": _plain(ln.debit), "EuroHaber": _plain(ln.credit), "PtaDebe": "0.00", "PtaHaber": "0.00",
            "DebeME": "0.00", "HaberME": "0.00", "BaseEuro": "0.00", "NoConv": "F"}


def _plain(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def write_contaplus_csv(entries: list[Entry], rep: Report) -> str:
    out = []
    for e in entries:
        if e.number is None or not 0 < e.number < 10 ** 6:
            raise JournalError("ContaPlus necesita números de asiento de 1 a 999999 (usa start_entry_number)")
        for ln in e.lines:
            vals = _cp_values(e, ln, rep)
            row = [""] * CP_FIELDS
            for name, v in vals.items():
                if name not in ("DebeME", "HaberME", "BaseEuro", "NoConv"):
                    row[_CP_IDX[name]] = v.replace(";", ",")
            out.append(";".join(row))
    return "\r\n".join(out) + "\r\n"


def _cp_entries(rows: list[tuple[int, Callable[[str], str]]], rep: Report) -> list[Entry]:
    entries: list[Entry] = []
    by_key: dict[tuple[int, date], Entry] = {}
    for no, get in rows:
        where = f"línea {no}: "
        num_raw = get("Asien").strip()
        if not num_raw.isdigit():
            raise JournalError(f"{where}número de asiento no válido: {num_raw!r}")
        d = _ymd(get("Fecha"), where)
        moneda = get("MonedaUso").strip()
        if moneda == "1":
            debit, credit = _dot_amount(get("PtaDebe"), where), _dot_amount(get("PtaHaber"), where)
            rep.warnings.append(f"{where}asiento en pesetas (MonedaUso=1): importes leídos sin convertir")
        else:
            debit, credit = _dot_amount(get("EuroDebe"), where), _dot_amount(get("EuroHaber"), where)
        key = (int(num_raw), d)
        e = by_key.get(key)
        if e is None:
            e = by_key[key] = Entry(number=int(num_raw), date=d)
            entries.append(e)
        e.lines.append(Line(account=get("SubCta").strip(), debit=debit, credit=credit, concept=get("Concepto").strip(),
                            document=get("Documento").strip(), source_line=no))
    return entries


def read_contaplus_csv(text: str, rep: Report) -> list[Entry]:
    rows = []
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        cells = raw.split(";")
        if len(cells) < _CP_IDX["EuroHaber"] + 1:
            raise JournalError(f"línea {no}: {len(cells)} campos; el Diario de ContaPlus en CSV tiene {CP_FIELDS}")
        cells = [c.strip().strip('"') for c in cells]
        rows.append((no, lambda name, c=cells: c[_CP_IDX[name]]))
    return _cp_entries(rows, rep)


def _cp_offsets() -> dict[str, tuple[int, int]]:
    pos, out = 0, {}
    for name, width in _CP_LAYOUT:
        out[name] = (pos, pos + width)
        pos += width
    return out


_CP_OFF = _cp_offsets()
CP_ASCII_WIDTH = sum(w for _, w in _CP_LAYOUT)


def write_contaplus_ascii(entries: list[Entry], rep: Report) -> str:
    out = []
    for e in entries:
        if e.number is None or not 0 < e.number < 10 ** 6:
            raise JournalError("ContaPlus necesita números de asiento de 1 a 999999 (usa start_entry_number)")
        for ln in e.lines:
            vals = _cp_values(e, ln, rep)
            rec = ""
            for name, width in _CP_LAYOUT:
                v = vals.get(name, "")
                rec += f"{v:>{width}}" if name in _CP_NUMERIC else f"{v:<{width}}"
            out.append(rec)
    return "\r\n".join(out) + "\r\n"


def read_contaplus_ascii(text: str, rep: Report) -> list[Entry]:
    need = _CP_OFF["EuroHaber"][1]
    rows = []
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        rec = raw.ljust(need)
        rows.append((no, lambda name, r=rec: r[_CP_OFF[name][0]:_CP_OFF[name][1]]))
    return _cp_entries(rows, rep)


# ---------------------------------------------------------------- Generic CSV

_SYNONYMS = {
    "number": ("asiento", "n asiento", "nº asiento", "num asiento", "numero asiento", "número asiento", "numero",
               "número", "entry", "entry number", "journal entry", "id asiento"),
    "date": ("fecha", "fecha asiento", "date", "fecha contable"),
    "account": ("cuenta", "subcuenta", "cuenta contable", "codigo cuenta", "código cuenta", "account", "account code"),
    "account_name": ("nombre cuenta", "descripcion cuenta", "descripción cuenta", "titulo cuenta", "título cuenta",
                     "account name", "nombre subcuenta"),
    "debit": ("debe", "cargo", "debit", "importe debe"),
    "credit": ("haber", "abono", "credit", "importe haber"),
    "amount": ("importe", "amount", "saldo"),
    "dh": ("d/h", "dh", "debe/haber", "signo", "tipo"),
    "concept": ("concepto", "descripcion", "descripción", "description", "concept", "detalle", "glosa"),
    "document": ("documento", "doc", "document", "factura", "nº factura", "referencia", "reference", "ref"),
}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().strip('"').lower().replace("_", " ").replace(".", ""))


def _columns(header: list[str], mapping: dict[str, str] | None) -> dict[str, int]:
    norm = [_norm(h) for h in header]
    cols: dict[str, int] = {}
    for key, col in (mapping or {}).items():
        if key not in _SYNONYMS:
            raise JournalError(f"column_map: clave desconocida {key!r} (válidas: {', '.join(_SYNONYMS)})")
        if _norm(col) not in norm:
            raise JournalError(f"column_map: la columna {col!r} no está en la cabecera")
        cols[key] = norm.index(_norm(col))
    for key, names in _SYNONYMS.items():
        if key in cols:
            continue
        for i, h in enumerate(norm):
            if h in names and i not in cols.values():
                cols[key] = i
                break
    missing = [k for k in ("date", "account") if k not in cols]
    if "debit" not in cols and "amount" not in cols:
        missing.append("debit/credit o amount")
    if missing:
        raise JournalError(f"no encuentro las columnas {', '.join(missing)} en la cabecera {header}; "
                           "indícalas con column_map")
    return cols


def _sniff(text: str) -> str:
    first = text.splitlines()[0] if text else ""
    return max((";", ",", "\t", "|"), key=first.count)


def read_csv(text: str, rep: Report, column_map: dict[str, str] | None = None, delimiter: str = "") -> list[Entry]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter or _sniff(text))
    rows = [r for r in reader]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise JournalError("CSV vacío")
    cols = _columns(rows[0], column_map)
    entries: list[Entry] = []
    by_num: dict[str, Entry] = {}
    cur: Entry | None = None
    running = 0

    def cell(r: list[str], key: str) -> str:
        i = cols.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    for no, r in enumerate(rows[1:], 2):
        where = f"fila {no}: "
        d = parse_date(cell(r, "date"), where)
        if "debit" in cols or "credit" in cols:
            debit, credit = parse_amount(cell(r, "debit"), where), parse_amount(cell(r, "credit"), where)
        else:
            amt, dh = parse_amount(cell(r, "amount"), where), cell(r, "dh").upper()[:1]
            if dh in ("D", "C") and "dh" in cols:
                debit, credit = (abs(amt), 0) if dh == "D" else (0, abs(amt))
            elif dh == "H":
                debit, credit = 0, abs(amt)
            else:
                debit, credit = (amt, 0) if amt >= 0 else (0, -amt)
        if debit < 0 and not credit:
            debit, credit = 0, -debit
        elif credit < 0 and not debit:
            debit, credit = -credit, 0
        ln = Line(account=cell(r, "account"), debit=debit, credit=credit, concept=cell(r, "concept"),
                  document=cell(r, "document"), account_name=cell(r, "account_name"), source_line=no)
        num = cell(r, "number")
        if "number" in cols and num:
            key = f"{num}|{d.isoformat()}"
            e = by_num.get(key)
            if e is None:
                e = by_num[key] = Entry(number=int(num) if num.isdigit() else None, date=d)
                entries.append(e)
            e.lines.append(ln)
            continue
        # No entry number: group consecutive lines until they balance.
        if cur is None or (running == 0 and cur.lines) or cur.date != d:
            if cur is not None and running != 0:
                rep.warnings.append(f"{where}nuevo asiento por cambio de fecha con el anterior descuadrado")
            cur = Entry(number=None, date=d)
            entries.append(cur)
            running = 0
        cur.lines.append(ln)
        running += debit - credit
    if "number" not in cols:
        rep.warnings.append("sin columna de asiento: los apuntes se agrupan hasta que cuadran")
    return entries


def _safe(v: str) -> str:
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def write_csv(entries: list[Entry], rep: Report, delimiter: str = ";", decimal_comma: bool = True,
              date_format: str = "%d/%m/%Y") -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(["Asiento", "Fecha", "Cuenta", "Nombre cuenta", "Concepto", "Documento", "Debe", "Haber"])

    def money(c: int) -> str:
        s = _plain(c)
        return s.replace(".", ",") if decimal_comma else s

    for e in entries:
        for ln in e.lines:
            w.writerow([e.number or "", e.date.strftime(date_format), ln.account, _safe(ln.account_name),
                        _safe(ln.concept), _safe(ln.document), money(ln.debit), money(ln.credit)])
    return buf.getvalue()


# ---------------------------------------------------------------- dispatch

def detect(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    if not lines:
        raise JournalError("fichero vacío")
    if all(len(ln.rstrip("\r")) in (254, 256, 510, 512) and ln[0] in "45" and ln[1:14].isdigit() for ln in lines):
        return "a3"
    if all(ln.count(";") >= 100 for ln in lines):
        return "contaplus_csv"
    if all(_looks_cp_ascii(ln) for ln in lines):
        return "contaplus_ascii"
    return "csv"


_CP_AMOUNT = re.compile(r"^ *-?\d+\.\d{2}$")


def _looks_cp_ascii(ln: str) -> bool:
    """Fixed-width ContaPlus record: numeric Asien and Fecha, and amounts where EuroDebe/EuroHaber sit.

    Checked by position, so concepts full of ';' or ',' do not make it look like a CSV.
    """
    (d0, d1), (h0, h1) = _CP_OFF["EuroDebe"], _CP_OFF["EuroHaber"]
    return (len(ln) >= h1 and ln[:6].strip().isdigit() and ln[6:14].isdigit()
            and bool(_CP_AMOUNT.match(ln[d0:d1])) and bool(_CP_AMOUNT.match(ln[h0:h1])))


def read(text: str, fmt: str, rep: Report, column_map: dict[str, str] | None = None,
         delimiter: str = "") -> tuple[list[Entry], str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # old Mac / mixed line endings
    fmt = detect(text) if fmt in ("", "auto") else fmt
    try:
        return _read(text, fmt, rep, column_map, delimiter), fmt
    except csv.Error as exc:
        raise JournalError(f"CSV mal formado: {exc}") from exc


def _read(text: str, fmt: str, rep: Report, column_map: dict[str, str] | None, delimiter: str) -> list[Entry]:
    if fmt == "a3":
        return read_a3(text, rep)
    if fmt == "contaplus_csv":
        return read_contaplus_csv(text, rep)
    if fmt == "contaplus_ascii":
        return read_contaplus_ascii(text, rep)
    if fmt == "csv":
        return read_csv(text, rep, column_map, delimiter)
    raise JournalError(f"formato de origen desconocido {fmt!r}; válidos: auto, {', '.join(FORMATS)}")


def write(entries: list[Entry], fmt: str, rep: Report, **opts: Any) -> str:
    if fmt == "a3":
        return write_a3(entries, rep, opts.get("company_code") or "00001", int(opts.get("a3_format") or 5))
    if fmt == "contaplus_csv":
        return write_contaplus_csv(entries, rep)
    if fmt == "contaplus_ascii":
        return write_contaplus_ascii(entries, rep)
    if fmt == "csv":
        return write_csv(entries, rep, opts.get("delimiter") or ";", opts.get("decimal_comma", True))
    raise JournalError(f"formato de destino desconocido {fmt!r}; válidos: {', '.join(FORMATS)}")
