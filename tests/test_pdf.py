"""Bank statement PDFs: deterministic table reading, balance chain checks, Norma 43 output, billing over MCP."""

from __future__ import annotations

import base64
from datetime import date

import pytest

from contabridge_mcp import n43, pdfbank
from contabridge_mcp.journal import JournalError
from pdf_builder import make_pdf, table_pdf
from test_e2e import balance, call, license_, server  # noqa: F401 - fixtures

IBAN = "IBAN ES91 2100 0418 4502 0005 1332"
CARGO_ABONO = ["Fecha", "F. valor", "Concepto", "Cargo", "Abono", "Saldo"]
COLS6 = [40, 100, 160, 400, 470, 550]


def caixa(rows=None, opening="10.000,00", closing="10.739,00") -> bytes:
    rows = rows or [
        ["02/03/2026", "02/03/2026", "TRANSF. TALLERES RUIZ SL", "", "1.210,00", "11.210,00"],
        ["05/03/2026", "05/03/2026", "RECIBO IBERDROLA CLIENTES", "121,00", "", "11.089,00"],
        ["", "", "FACTURA 2026-03 LUZ", "", "", ""],
        ["10/03/2026", "10/03/2026", "COMISION MANTENIMIENTO", "350,00", "", "10.739,00"],
    ]
    top = ["CaixaBank  Extracto de cuenta", IBAN] + ([f"Saldo inicial: {opening}"] if opening else [])
    return table_pdf(CARGO_ABONO, rows, COLS6, top=top, bottom=[f"Saldo final: {closing}"] if closing else [],
                     right={3, 4, 5})


def test_amount_and_date_tokens():
    assert pdfbank.parse_amount_token("1.234,56") == (123456, None)
    assert pdfbank.parse_amount_token("-1,234.56") == (123456, -1)
    assert pdfbank.parse_amount_token("12,00-") == (1200, -1)
    assert pdfbank.parse_amount_token("€1,000.00") == (100000, None)
    assert pdfbank.parse_amount_token("+5,00 €") == (500, 1)
    assert pdfbank.parse_amount_token("2026-03") is None
    assert pdfbank.parse_amount_token("1234") is None
    assert pdfbank.parse_date_token("05/03/2026")[0] == date(2026, 3, 5)
    assert pdfbank.parse_date_token("5 mar 2026")[0] == date(2026, 3, 5)
    assert pdfbank.parse_date_token("Mar 5, 2026")[0] == date(2026, 3, 5)
    assert pdfbank.parse_date_token("2026-03-05")[0] == date(2026, 3, 5)
    assert pdfbank.parse_date_token("31/02/2026") is None


def test_cargo_abono_columns_balanced():
    st = pdfbank.read_statement(caixa())
    assert [r.cents for r in st.rows] == [121000, -12100, -35000]
    assert st.rows[1].text() == "RECIBO IBERDROLA CLIENTES FACTURA 2026-03 LUZ"
    assert st.iban == "ES9121000418450200051332" and st.bank == "CaixaBank"
    assert st.checks["balanced"] and st.checks["closing_ok"] and st.checks["opening_source"] == "pdf"


def test_mismatch_is_flagged_not_fixed():
    rows = [["02/03/2026", "", "TRANSF. CLIENTE", "", "1.210,00", "11.210,00"],
            ["05/03/2026", "", "RECIBO LUZ", "121,00", "", "11.098,00"],  # should be 11.089,00
            ["10/03/2026", "", "COMISION", "350,00", "", "10.748,00"]]
    st = pdfbank.read_statement(caixa(rows, closing="10.748,00"))
    assert [r.status for r in st.rows] == ["ok", "mismatch", "ok"]
    bad = [r for r in st.rows if r.status == "mismatch"]
    assert any("11098.00" in r.issue for r in bad)
    assert st.rows[1].cents == -12100  # printed amount kept as is
    assert not st.checks["balanced"] and not st.checks["closing_ok"]


def test_single_amount_column_signed_and_descending_order():
    """BBVA/Santander style: one signed Importe column, newest first."""
    rows = [["10/03/2026", "", "COMISION", "-350,00", "10.739,00"],
            ["05/03/2026", "", "RECIBO IBERDROLA", "-121,00", "11.089,00"],
            ["02/03/2026", "", "TRANSFERENCIA RECIBIDA", "1.210,00", "11.210,00"]]
    pdf = table_pdf(["F. Operación", "F. Valor", "Concepto", "Importe", "Saldo"], rows, [40, 110, 180, 470, 550],
                    top=["BBVA"], right={3, 4})
    st = pdfbank.read_statement(pdf)
    assert st.descending and [r.cents for r in st.rows] == [121000, -12100, -35000]
    assert st.checks["opening_balance"] == "10000.00" and st.checks["opening_source"] == "derived from first row"
    assert st.checks["balanced"]


def test_unsigned_amount_resolved_by_balance_or_sent_to_review():
    rows = [["02/03/2026", "", "INGRESO", "1.210,00", "11.210,00"],
            ["05/03/2026", "", "RECIBO", "121,00", "11.089,00"]]
    hdr = ["Fecha", "F. valor", "Concepto", "Importe", "Saldo"]
    st = pdfbank.read_statement(table_pdf(hdr, rows, [40, 110, 180, 470, 550], top=["Saldo anterior 10.000,00"],
                                          right={3, 4}))
    assert [r.cents for r in st.rows] == [121000, -12100] and st.checks["balanced"]
    # Without opening balance the first unsigned row cannot be proven.
    st = pdfbank.read_statement(table_pdf(hdr, rows, [40, 110, 180, 470, 550], right={3, 4}))
    assert st.rows[0].status == "review" and st.rows[0].cents is None
    assert st.rows[1].cents == -12100 and not st.checks["balanced"]
    assert len(pdfbank.to_parse_result(st).accounts[0].movements) == 1


def test_revolut_english_money_in_out_text_dates():
    rows = [["1 Mar 2026", "Opening", "", "", ""],
            ["3 Mar 2026", "Stripe payout", "", "€1,000.00", "€1,500.00"],
            ["4 Mar 2026", "AWS EMEA", "€25.30", "", "€1,474.70"]]
    pdf = table_pdf(["Date", "Description", "Money out", "Money in", "Balance"], rows, [40, 130, 400, 470, 550],
                    top=["Revolut Business", "Opening balance €500.00"], bottom=["Closing balance €1,474.70"],
                    right={2, 3, 4})
    st = pdfbank.read_statement(pdf)
    assert [r.cents for r in st.rows] == [100000, -2530] and st.bank == "Revolut"
    assert st.checks["balanced"]


def test_multi_page_balanced():
    rows, bal = [], 1000000
    for i in range(60):
        amt = 1000 + i
        bal += amt if i % 2 else -amt
        a = f"{amt // 100},{amt % 100:02d}"
        rows.append([f"{1 + i // 3:02d}/03/2026", "", f"MOV {i}", "" if i % 2 else a, a if i % 2 else "",
                     f"{bal // 100},{bal % 100:02d}"])
    st = pdfbank.read_statement(table_pdf(CARGO_ABONO, rows, COLS6, rows_per_page=25, top=["Saldo inicial 10000,00"],
                                          right={3, 4, 5}))
    assert st.pages == 3 and len(st.rows) == 60 and st.checks["balanced"]


def test_refuses_scanned_and_non_pdf_and_no_table():
    with pytest.raises(JournalError, match="no es un PDF"):
        pdfbank.read_statement(b"hola")
    with pytest.raises(JournalError, match="texto seleccionable"):
        pdfbank.read_statement(make_pdf([[]]))
    with pytest.raises(JournalError, match="cabecera"):
        pdfbank.read_statement(make_pdf([[(40, 800, "Carta comercial sin tabla 12,00")]]))


def test_norma43_round_trip():
    st = pdfbank.read_statement(caixa())
    text, warns = pdfbank.to_norma43(st, holder="Acme Soluciones SL")
    assert not warns and all(len(x) == 80 for x in text.split("\r\n") if x)
    res = n43.parse(text)
    assert res.valid, res.issues
    acc = res.accounts[0]
    assert (acc.bank, acc.branch, acc.number) == ("2100", "0418", "0200051332")
    assert acc.opening == 1000000 and acc.closing in (None, 1073900) or acc.footer
    assert [m.cents for m in acc.movements] == [121000, -12100, -35000]
    assert "IBERDROLA" in acc.movements[1].concept_text


def test_deterministic():
    a = pdfbank.summary(pdfbank.read_statement(caixa()))
    b = pdfbank.summary(pdfbank.read_statement(caixa()))
    assert a == b


async def test_tool_contasol_and_charge(server):
    before = await balance(server)
    err, data, text = await call(server, "pdf_bank_to_journal", content_base64=base64.b64encode(caixa()).decode(),
                                 rules=[{"account": "62800000", "contains": ["IBERDROLA"]}])
    assert not err, text
    assert data["filename"] == "APU.ods" and data["statement"]["balance_check"]["balanced"]
    assert data["bank"]["matched"] == 1 and data["summary"]["entries"] == 3
    assert data["billing"]["credits_charged"] == 3 and await balance(server) == before - 3


async def test_tool_norma43_and_a3(server):
    err, data, text = await call(server, "pdf_bank_to_journal", content_base64=base64.b64encode(caixa()).decode(),
                                 target_format="norma43")
    assert not err, text
    raw = base64.b64decode(data["content_base64"]).decode("cp1252")
    assert raw.startswith("11210004180200051332") and n43.parse(raw).valid
    err, data, text = await call(server, "pdf_bank_to_journal", content_base64=base64.b64encode(caixa()).decode(),
                                 target_format="a3")
    assert not err and data["filename"] == "SUENLACE.DAT"


async def test_tool_strict_and_bad_pdf_are_free(server):
    before = await balance(server)
    rows = [["02/03/2026", "", "TRANSF", "", "1.210,00", "11.210,00"],
            ["05/03/2026", "", "RECIBO", "121,00", "", "11.098,00"]]
    err, _, text = await call(server, "pdf_bank_to_journal", strict_balance=True,
                              content_base64=base64.b64encode(caixa(rows, closing="")).decode())
    assert err and "no cuadra" in text
    err, _, text = await call(server, "pdf_bank_to_journal", content_base64=base64.b64encode(b"%PDF-1.4 roto").decode())
    assert err
    assert await balance(server) == before
