"""Format, validation and bank-rule tests. Layout assertions read positions straight from the official specs."""

from __future__ import annotations

from datetime import date

import pytest

from contabridge_mcp import bank, formats, journal, n43
from contabridge_mcp.journal import Entry, JournalError, Line
from n43_builder import sample_statement


def sample_entries() -> list[Entry]:
    return [
        Entry(1, date(2026, 3, 31), [
            Line("64000000", debit=150000, concept="Nómina marzo", document="NOM03"),
            Line("47510000", credit=22500, concept="Nómina marzo", document="NOM03"),
            Line("57200001", credit=127500, concept="Nómina marzo", document="NOM03")]),
        Entry(2, date(2026, 4, 2), [
            Line("62800000", debit=4530, concept="Recibo luz Iberdrola", account_name="Suministros"),
            Line("57200001", credit=4530, concept="Recibo luz Iberdrola")]),
    ]


# ---------------------------------------------------------------- amounts, dates, accounts

@pytest.mark.parametrize("raw,cents", [("1.234,56", 123456), ("1,234.56", 123456), ("1234.5", 123450),
                                       ("-12", -1200), ("", 0), ("1.234.567,00", 123456700), ("12,5", 1250),
                                       ("(45,30)", -4530), ("+0000001000.00", 100000), ("0.123", None)])
def test_parse_amount(raw, cents):
    if cents is None:
        with pytest.raises(JournalError):
            journal.parse_amount(raw)
    else:
        assert journal.parse_amount(raw) == cents


@pytest.mark.parametrize("raw", ["31/03/2026", "2026-03-31", "20260331", "31-03-2026", "31.03.26"])
def test_parse_date(raw):
    assert journal.parse_date(raw) == date(2026, 3, 31)


def test_resize_and_expand():
    assert journal.resize_account("4300001", 8) == "43000001"
    assert journal.resize_account("430000001", 8) == "43000001"
    assert journal.resize_account("430.1", 8) == "43000001"
    assert journal.resize_account("5720", 8) == "57200000"
    with pytest.raises(JournalError):
        journal.resize_account("430012345", 8)


def test_validate_catches_import_errors():
    bad = [Entry(1, date(2025, 12, 31), [Line("6400000", debit=100), Line("57200001", credit=90)]),
           Entry(2, date(2026, 1, 2), [Line("ABC", debit=10, credit=10), Line("0123", debit=0)])]
    msgs = [i["message"] for i in journal.validate(bad, fiscal_year=2026)]
    assert any("descuadrado" in m and "0.10" in m for m in msgs)
    assert any("fuera del ejercicio" in m for m in msgs)
    assert any("debe y haber" in m for m in msgs)
    assert any("no numérica" in m for m in msgs)
    assert any("grupos 1-9" in m for m in msgs)
    assert any("longitudes de cuenta mezcladas" in m for m in msgs)
    assert journal.validate(sample_entries(), 2026) == []


# ---------------------------------------------------------------- A3

@pytest.mark.parametrize("fmt,length", [(5, 510), (4, 254)])
def test_a3_layout_matches_spec(fmt, length):
    rep = formats.Report()
    out = formats.write_a3(sample_entries(), rep, "123", fmt)
    assert out.endswith("\r\n")
    recs = out.split("\r\n")[:-1]
    assert len(recs) == 5 and all(len(r) == length for r in recs)
    r = recs[0]
    assert r[0] == str(fmt)                     # 1 tipo de formato
    assert r[1:6] == "00123"                    # 2-6 empresa
    assert r[6:14] == "20260331"                # 7-14 fecha
    assert r[14] == "0"                         # 15 tipo de registro
    assert r[15:27] == "64000000    "           # 16-27 cuenta
    assert r[57] == "D"                         # 58 D/H
    assert r[58:68] == "NOM03     "             # 59-68 documento
    assert r[68] == "I"                         # 69 línea de apunte
    assert r[69:99].rstrip() == "Nómina marzo"  # 70-99 descripción
    assert r[99:113] == "+0000001500.00"        # 100-113 importe
    assert [x[68] for x in recs] == ["I", "M", "U", "I", "U"]
    assert recs[1][57] == "H" and recs[1][99:113] == "+0000000225.00"
    if fmt == 5:
        assert r[508:510] == "EN"               # 509 moneda, 510 generado
    else:
        assert r[252:254] == "EN"               # 253 moneda, 254 generado
    assert recs[3][27:57].rstrip() == "Suministros"


def test_a3_round_trip_and_skips_invoice_records():
    rep = formats.Report()
    text = formats.write_a3(sample_entries(), rep)
    invoice = "5" + "00001" + "20260401" + "1" + " " * 495  # a type-1 record is skipped with a warning
    entries = formats.read_a3(text + invoice + "\r\n", rep)
    assert [len(e.lines) for e in entries] == [3, 2]
    assert entries[0].lines[2].credit == 127500 and entries[1].lines[0].account == "62800000"
    assert any("tipo 1" in w for w in rep.done())


def test_a3_rejects_unclosed_entry():
    text = formats.write_a3(sample_entries(), formats.Report()).split("\r\n")[0]
    with pytest.raises(JournalError, match="sin cerrar"):
        formats.read_a3(text, formats.Report())


# ---------------------------------------------------------------- ContaPlus

def test_contaplus_csv_fields():
    out = formats.write_contaplus_csv(sample_entries(), formats.Report())
    rows = [r.split(";") for r in out.strip().split("\r\n")]
    assert all(len(r) == 142 for r in rows)
    r = rows[3]  # entry 2, debit line
    assert r[0] == "2" and r[1] == "20260402" and r[2] == "62800000"
    assert r[3] == "57200001"          # Contra (two-line entry)
    assert r[5] == "Recibo luz Iberdrola"
    assert r[26] == "2"                # MonedaUso euros
    assert r[27] == "45.30" and r[28] == "0.00"   # EuroDebe / EuroHaber
    assert rows[0][3] == ""            # three-line entry has no single contrapartida


def test_contaplus_round_trips():
    for w, r in ((formats.write_contaplus_csv, formats.read_contaplus_csv),
                 (formats.write_contaplus_ascii, formats.read_contaplus_ascii)):
        back = r(w(sample_entries(), formats.Report()), formats.Report())
        assert [(e.number, e.date, [(ln.account, ln.debit, ln.credit) for ln in e.lines]) for e in back] == \
               [(e.number, e.date, [(ln.account, ln.debit, ln.credit) for ln in e.lines]) for e in sample_entries()]


def test_contaplus_truncates_concept_with_warning():
    e = sample_entries()
    e[0].lines[0].concept = "X" * 40
    rep = formats.Report()
    out = formats.write_contaplus_csv(e, rep)
    assert out.split(";")[5] == "X" * 25
    assert any("concepto" in w for w in rep.done())


# ---------------------------------------------------------------- generic CSV

def test_csv_holded_style_with_numbers():
    text = ("Asiento;Fecha;Cuenta;Concepto;Debe;Haber\n"
            "1;31/03/2026;64000000;Nómina;1.500,00;\n1;31/03/2026;47510000;Nómina;;225,00\n"
            "1;31/03/2026;57200001;Nómina;;1.275,00\n")
    entries, fmt = formats.read(text, "auto", formats.Report())
    assert fmt == "csv" and len(entries) == 1 and entries[0].debit == entries[0].credit == 150000


def test_csv_without_entry_number_groups_until_balanced():
    text = ("date,account,description,amount\n2026-04-02,62800000,Luz,45.30\n2026-04-02,57200001,Luz,-45.30\n"
            "2026-04-02,57200001,Cobro,100\n2026-04-02,43000001,Cobro,-100\n")
    rep = formats.Report()
    entries = formats.read_csv(text, rep)
    assert [len(e.lines) for e in entries] == [2, 2]
    assert entries[1].lines[0].debit == 10000 and entries[1].lines[1].credit == 10000


def test_csv_column_map_and_formula_injection():
    text = "Cta;Dia;Imp D;Imp H;Txt\n62800000;02/04/2026;10,00;;=HYPERLINK(1)\n57200001;02/04/2026;;10,00;x\n"
    entries = formats.read_csv(text, formats.Report(), {"account": "Cta", "date": "Dia", "debit": "Imp D",
                                                        "credit": "Imp H", "concept": "Txt"})
    journal.number_entries(entries, 1)
    out = formats.write_csv(entries, formats.Report())
    assert ";'=HYPERLINK(1);" in out and "10,00" in out


def test_detect():
    assert formats.detect(formats.write_a3(sample_entries(), formats.Report())) == "a3"
    assert formats.detect(formats.write_contaplus_csv(sample_entries(), formats.Report())) == "contaplus_csv"
    assert formats.detect(formats.write_contaplus_ascii(sample_entries(), formats.Report())) == "contaplus_ascii"
    assert formats.detect("fecha;cuenta;debe;haber\n") == "csv"


# ---------------------------------------------------------------- Norma 43 -> journal

RULES = [{"contains": "electrica", "account": "62800000", "concept": "Suministro eléctrico"},
         {"common_concept": "17", "account": "62600000"},
         {"contains": ["nomina"], "account": "46500000"},
         {"direction": "abono", "regex": r"F2026-\d{4}", "account": "43000000"}]


def test_bank_to_entries():
    res = n43.parse(sample_statement())
    entries, stats = bank.movements_to_entries(res, "57200001", bank.build_rules(RULES), "55500000")
    assert len(entries) == 8 and stats["matched"] == 7
    assert [u["amount"] for u in stats["unmatched"]] == ["-300.00"]  # PROVEEDOR XYZ, no rule
    luz = entries[1]
    assert (luz.lines[0].account, luz.lines[0].debit) == ("62800000", 4530)
    assert (luz.lines[1].account, luz.lines[1].credit) == ("57200001", 4530)
    assert luz.lines[0].concept == "Suministro eléctrico"
    cobro = entries[0]
    assert (cobro.lines[0].account, cobro.lines[0].debit, cobro.lines[1].account) == ("57200001", 121000, "43000000")
    assert entries[3].lines[0].account == "62600000"  # comisión by AEB concept 17
    assert entries[7].lines[0].account == "55500000"
    assert journal.validate(entries) == []


def test_bad_rules():
    with pytest.raises(JournalError, match="falta 'account'"):
        bank.build_rules([{"contains": "x"}])
    with pytest.raises(JournalError, match="regex"):
        bank.build_rules([{"account": "6", "regex": "("}])


def test_contaplus_ascii_matches_bulmages_layout():
    """Same widths, justification and fixed values the BulmaGés XDiario exporter writes."""
    rec = formats.write_contaplus_ascii(sample_entries(), formats.Report()).split("\r\n")[3]
    assert len(rec) == formats.CP_ASCII_WIDTH == 297
    assert rec[0:6] == "     2" and rec[6:14] == "20260402" and rec[14:26] == "62800000    "
    assert rec[38:54] == "            0.00"          # PtaDebe right-aligned
    assert rec[54:79] == "Recibo luz Iberdrola     "  # Concepto
    off = {name: sum(w for _, w in formats._CP_LAYOUT[:i]) for i, (name, _) in enumerate(formats._CP_LAYOUT)}
    assert rec[off["MonedaUso"]] == "2"
    assert rec[off["EuroDebe"]:off["EuroDebe"] + 16] == "           45.30"
    assert rec[off["NoConv"]] == "F" and rec[off["NumeroInv"]:].strip() == ""


def test_a3_euro_sign_survives_cp1252():
    e = sample_entries()
    e[1].lines[0].concept = "Cuota 10€"
    text = formats.write_a3(e, formats.Report())
    assert "Cuota 10€".encode("cp1252") in text.encode("cp1252")
