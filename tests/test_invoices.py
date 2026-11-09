"""invoices_to_journal + bank_to_journal with open items, CSV exports and movement lists."""
from __future__ import annotations

import pytest

from contabridge_mcp import bank, invoices
from contabridge_mcp.journal import JournalError
from n43_builder import sample_statement
from test_e2e import balance, call, license_, server  # noqa: F401  (fixtures)

ISSUED = {"kind": "issued", "number": "F2026-0101", "date": "2026-09-30", "nif": "A11111111", "name": "Cliente Uno SA",
          "taxes": [{"base": "1000.00", "rate": "21"}], "total": "1210.00"}
RECEIVED = {"kind": "received", "number": "P-77", "date": "2026-09-15", "nif": "B22222222", "name": "Asesor Libre",
            "base": "500.00", "vat_rate": "21", "irpf_rate": "15", "total": "530.00",
            "counterpart_account": "62300000"}


def _lines(entry):
    return [(ln.account, ln.debit, ln.credit) for ln in entry.lines]


def test_issued_and_received_split_vat_and_irpf():
    entries, st = invoices.invoices_to_entries([ISSUED, RECEIVED], party_accounts={"A11111111": "43000001"})
    assert _lines(entries[0]) == [("43000001", 121000, 0), ("70000000", 0, 100000), ("47700000", 0, 21000)]
    assert _lines(entries[1]) == [("62300000", 50000, 0), ("47200000", 10500, 0), ("47510000", 0, 7500),
                                  ("40000000", 0, 53000)]
    assert all(e.debit == e.credit for e in entries)
    assert st["generic_party_account"] == ["P-77"]
    assert st["open_items"][0] == {"account": "43000001", "amount": "1210.00", "reference": "F2026-0101",
                                   "name": "Cliente Uno SA", "date": "2026-09-30"}
    assert st["open_items"][1]["amount"] == "-530.00"
    assert {s["kind"] + s["rate"]: s["vat"] for s in st["vat_summary"]} == {"issued21": "210.00", "received21": "105.00"}


def test_mismatched_total_goes_to_review_never_booked():
    bad = {**ISSUED, "number": "F9", "total": "1300.00"}
    entries, st = invoices.invoices_to_entries([bad, ISSUED])
    assert len(entries) == 1 and st["review"][0]["number"] == "F9" and "no cuadra" in st["review"][0]["reason"]


def test_one_cent_rounding_absorbed_and_multi_rate_recargo():
    inv = {"kind": "received", "number": "T1", "date": "2026-10-01", "taxes": [
        {"base": "10.33", "rate": "21", "recargo_rate": "5.2"}, {"base": "4.99", "rate": "10"}], "total": "18.54"}
    entries, st = invoices.invoices_to_entries([inv], vat_accounts={"21": "47200021", "re5.2": "47200052"})
    e = entries[0]
    assert e.debit == e.credit == 1854
    assert ("47200052", 54, 0) in _lines(e)


def test_rectificativa_swaps_sides_and_duplicates_flagged():
    rect = {**ISSUED, "number": "R1", "taxes": [{"base": "-100.00", "rate": "21"}], "total": "-121.00"}
    entries, st = invoices.invoices_to_entries([rect, rect])
    assert _lines(entries[0])[0] == ("43000000", 0, 12100)
    assert "duplicada" in st["review"][0]["reason"]


def test_csv_export_and_open_items_settle_customer_account():
    csv_text = ("Banco Demo;;\nExtracto cuenta;;\nFecha;Concepto;Importe;Saldo\n"
                "02/10/2026;TRANSF CLIENTE UNO SA PAGO F2026-0101;1.210,00;5.000,00\n"
                "05/10/2026;RECIBO IBERDROLA;-45,30;4.954,70\n;;Total;\n")
    res, info = bank.movements_from_csv(csv_text)
    assert info["header_row"] == 3 and info["skipped_rows"] == [6]
    items = bank.build_open_items([{"account": "43000001", "amount": "1210.00", "reference": "F2026-0101"}])
    entries, st = bank.movements_to_entries(res, "57200001", [], "55500000", open_items=items)
    assert entries[0].lines[1].account == "43000001"
    assert st["open_items"]["settled"][0]["evidence"] == "importe + referencia"
    assert entries[1].lines[0].account == "55500000"


def test_ambiguous_open_items_are_not_guessed():
    res = bank.movements_from_list([{"date": "2026-10-02", "amount": "100.00", "text": "INGRESO"}])
    items = bank.build_open_items([{"account": "43000001", "amount": "100", "date": "2026-10-01"},
                                   {"account": "43000002", "amount": "100", "date": "2026-10-01"}])
    entries, st = bank.movements_to_entries(res, "57200001", [], "55500000", open_items=items)
    assert entries[0].lines[1].account == "55500000"
    assert "2 facturas posibles" in st["open_items"]["ambiguous"][0]["reason"]


def test_csv_without_header_is_refused():
    with pytest.raises(JournalError):
        bank.movements_from_csv("a;b;c\n1;2;3\n")


async def test_invoices_to_journal_e2e(server):
    before = await balance(server)
    err, data, text = await call(server, "invoices_to_journal", invoices=[ISSUED, RECEIVED], target_format="a3",
                                 party_accounts={"A11111111": "43000001"})
    assert not err, text
    assert data["summary"]["entries"] == 2 and data["summary"]["balanced"]
    assert data["billing"]["credits_charged"] == 3 and await balance(server) == before - 3
    assert data["open_items"][0]["account"] == "43000001"
    err, data2, text = await call(server, "bank_to_journal", content=sample_statement(), bank_account="57200001",
                                  open_items=data["open_items"])
    assert not err, text
    assert data2["bank"]["open_items"]["settled"][0]["account"] == "43000001"


async def test_invoices_all_bad_is_free(server):
    before = await balance(server)
    err, _, text = await call(server, "invoices_to_journal", invoices=[{**ISSUED, "total": "1"}])
    assert err and "ninguna factura" in text and await balance(server) == before
