"""Hostile inputs: ContaPlus detection with punctuation, broken CSV, ReDoS-prone rules."""
from __future__ import annotations

import time
from datetime import date

import pytest

from contabridge_mcp import bank, formats as F, journal as J


def _entries(concept):
    return [J.Entry(1, date(2026, 3, 1), [J.Line("4300001", debit=12100, concept=concept),
                                          J.Line("7000000", credit=12100, concept=concept)])]


@pytest.mark.parametrize("concept", ["Fras. 12, 13, 14", "Pago; cuota; marzo", "a;b;c,d,e"])
def test_contaplus_ascii_detected_despite_punctuation(concept):
    out = F.write(_entries(concept), "contaplus_ascii", F.Report())
    back, fmt = F.read(out, "auto", F.Report())
    assert fmt == "contaplus_ascii" and back[0].lines[0].debit == 12100


def test_bare_carriage_returns_and_broken_csv_are_clean_errors():
    text = "fecha;cuenta;debe;haber\r01/03/2026;4300001;121,00;\r01/03/2026;7000000;;121,00\r"
    back, _ = F.read(text, "auto", F.Report())
    assert len(back) == 1 and back[0].debit == 12100
    with pytest.raises(J.JournalError):
        F.read('fecha;cuenta;debe\n"01/03/2026;4300001\x00;1', "csv", F.Report())


@pytest.mark.parametrize("rx", ["(a+)+$", "^(a*)*b", r"(\w+\s?)*$", r"(x|y)\1", "a" * 301])
def test_catastrophic_regex_rules_are_refused(rx):
    t = time.perf_counter()
    with pytest.raises(J.JournalError):
        bank.build_rules([{"account": "70000000", "regex": rx}])
    assert time.perf_counter() - t < 1


@pytest.mark.parametrize("rx", ["IBERDROLA|ENDESA", r"^TRANSF.*F2026-\d{4}", r"(RECIBO|CUOTA) \d+", "((ab)?c)+"])
def test_ordinary_regex_rules_still_work(rx):
    assert bank.build_rules([{"account": "70000000", "regex": rx}])[0].regex is not None
