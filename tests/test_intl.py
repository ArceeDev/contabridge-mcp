"""QuickBooks (IIF, Online CSV) and Xero (manual journal CSV, bank statement CSV)."""

from __future__ import annotations

from datetime import date

import pytest

from contabridge_mcp import formats, intl, journal, n43
from contabridge_mcp.journal import Entry, JournalError, Line
from n43_builder import sample_statement
from test_core import sample_entries

INTL_ENTRIES = [
    Entry(7, date(2026, 3, 4), [Line("Office Supplies", debit=123456, concept="Staples, March"),
                                Line("Checking", credit=123456, concept="Staples, March")]),
    Entry(8, date(2026, 3, 31), [Line("Rent", debit=200000, concept="Rent"), Line("Payroll", debit=5000),
                                 Line("Checking", credit=205000, concept="Rent")]),
]


@pytest.mark.parametrize("raw,cents", [("1,234.56", 123456), ("$1,234", 123400), ("(45.30)", -4530), ("-650", -65000),
                                       ("1.500", 150), ("£12.5", 1250), ("", 0)])
def test_us_amounts(raw, cents):
    assert intl.amount(raw) == cents


def test_us_amount_rejects_garbage():
    with pytest.raises(JournalError):
        intl.amount("12,34,x")
    with pytest.raises(JournalError):
        intl.amount("1.234")  # three decimals


def test_date_styles():
    assert intl.parse_day("03/04/2026", "mdy") == date(2026, 3, 4)
    assert intl.parse_day("03/04/2026", "dmy") == date(2026, 4, 3)
    assert intl.parse_day("2026-04-03", "mdy") == date(2026, 4, 3)
    assert intl.parse_day("3/4/26", "mdy") == date(2026, 3, 4)
    with pytest.raises(JournalError):
        intl.parse_day("13/31/2026", "mdy")
    assert intl.day(date(2026, 3, 4), "mdy") == "03/04/2026"
    assert intl.day(date(2026, 3, 4), "dmy") == "04/03/2026"


@pytest.mark.parametrize("fmt", intl.FORMATS)
def test_roundtrip(fmt):
    rep = formats.Report()
    text = formats.write(INTL_ENTRIES, fmt, rep)
    assert formats.detect(text) == fmt
    back, src = formats.read(text, "auto", formats.Report())
    assert src == fmt
    assert [(e.date, [(ln.account, ln.debit, ln.credit) for ln in e.lines]) for e in back] == \
           [(e.date, [(ln.account, ln.debit, ln.credit) for ln in e.lines]) for e in INTL_ENTRIES]
    assert [e.number for e in back] == [7, 8]
    assert not [i for i in journal.validate(back, pgc=False) if i["level"] == "error"]


def test_iif_layout():
    text = intl.write_iif(INTL_ENTRIES[:1], formats.Report())
    lines = text.split("\r\n")
    assert lines[0].split("\t")[:5] == ["!TRNS", "TRNSID", "TRNSTYPE", "DATE", "ACCNT"]
    trns = lines[3].split("\t")
    spl = lines[4].split("\t")
    assert trns[0] == "TRNS" and trns[2] == "GENERAL JOURNAL" and trns[3] == "03/04/2026"
    assert trns[7] == "1234.56" and spl[7] == "-1234.56"  # TRNS amount = -(sum of SPL)
    assert lines[5] == "ENDTRNS"


def test_iif_reads_real_world_export_with_lists():
    text = ("!ACCNT\tNAME\tACCNTTYPE\nACCNT\tChecking\tBANK\n"
            "!TRNS\tTRNSID\tTRNSTYPE\tDATE\tACCNT\tAMOUNT\tMEMO\n!SPL\tSPLID\tTRNSTYPE\tDATE\tACCNT\tAMOUNT\tMEMO\n"
            "!ENDTRNS\nTRNS\t\tGENERAL JOURNAL\t7/1/1998\tChecking\t650\t\n"
            "SPL\t\tGENERAL JOURNAL\t7/1/1998\tExpense Account\t-650\t\nENDTRNS\n")
    rep = formats.Report()
    entries, src = formats.read(text, "auto", rep)
    assert src == "quickbooks_iif"
    assert entries[0].date == date(1998, 7, 1)
    assert entries[0].lines[0].debit == 65000 and entries[0].lines[1].credit == 65000
    assert any("ignorados" in w for w in rep.done())


def test_qbo_csv_header_and_us_numbers():
    text = intl.write_qbo_csv(INTL_ENTRIES, formats.Report())
    assert text.splitlines()[0] == "Journal No.,Journal Date,Account Name,Debits,Credits,Description,Name"
    assert "7,03/04/2026,Office Supplies,1234.56,,\"Staples, March\"," in text


def test_xero_template_and_tax_rate():
    text = intl.write_xero_csv(INTL_ENTRIES, formats.Report(), tax_rate="BAS Excluded")
    head, first = text.splitlines()[:2]
    assert head.startswith("*Narration,*Date,Description,*AccountCode,*TaxRate,*Amount")
    assert first.startswith('"#7 Staples, March",04/03/2026,"Staples, March",Office Supplies,BAS Excluded,1234.56')
    assert "-2050.00" in text


def test_xero_debit_credit_columns_and_grouping_without_narration():
    text = "Date,AccountCode,Debit,Credit\n31/03/2026,200,,10.00\n31/03/2026,090,10.00,\n31/03/2026,400,5.00,\n" \
           "31/03/2026,090,,5.00\n"
    entries = intl.read_xero_csv(text, formats.Report())
    assert len(entries) == 2 and entries[0].lines[0].credit == 1000


def test_spanish_to_xero_with_account_map_and_back_to_a3():
    rep = formats.Report()
    es = sample_entries()
    out = formats.write(es, "xero_csv", rep)
    back, _ = formats.read(out, "xero_csv", formats.Report())
    assert back[0].lines[0].account == "64000000"
    a3 = formats.write(back, "a3", formats.Report())
    assert formats.detect(a3) == "a3"


def test_pgc_validation_flags_names_and_hints_account_map():
    issues = journal.validate(INTL_ENTRIES)
    assert any("account_map" in i["message"] for i in issues)
    assert not journal.validate(INTL_ENTRIES, pgc=False)


def test_empty_account_is_an_error_outside_pgc():
    e = [Entry(1, date(2026, 1, 1), [Line("", debit=100), Line("Cash", credit=100)])]
    assert any(i["message"] == "apunte sin cuenta" for i in journal.validate(e, pgc=False))


def test_formula_injection_guarded():
    e = [Entry(1, date(2026, 1, 1), [Line("Cash", debit=100, concept="=HYPERLINK(1)"), Line("Sales", credit=100)])]
    assert "'=HYPERLINK" in intl.write_xero_csv(e, formats.Report())
    assert "'=HYPERLINK" in intl.write_qbo_csv(e, formats.Report())


@pytest.mark.parametrize("target,head", [("xero", "*Date,*Amount,Payee,Description,Reference,Cheque Number"),
                                         ("quickbooks", "Date,Description,Amount")])
def test_bank_statement_csv(target, head):
    res = n43.parse(sample_statement())
    text, stats = intl.bank_statement(res, target)
    lines = text.splitlines()
    assert lines[0] == head
    assert stats["movements"] == len(lines) - 1 > 0
    total = sum(m.cents for a in res.accounts for m in a.movements)
    assert stats["net"] == intl.money(total)


def test_bank_statement_bad_target_and_filter():
    res = n43.parse(sample_statement())
    with pytest.raises(JournalError):
        intl.bank_statement(res, "sage")
    with pytest.raises(JournalError):
        intl.bank_statement(res, "xero", account_filter="99999")
