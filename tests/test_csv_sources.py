"""Bank/PSP CSV exports with their published column names (synthetic rows): Revolut, Stripe, PayPal, BBVA."""

from __future__ import annotations

from contabridge_mcp import bank


def _movs(text):
    res, info = bank.movements_from_csv(text)
    return [(m.operation_date, m.cents, m.concept_text) for m in res.accounts[0].movements], info


def test_revolut_amount_minus_fee_and_states():
    csv = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
           "TOPUP,Current,2026-03-01 09:00:00,2026-03-01 09:00:05,Top-Up by *1234,500.00,0.00,EUR,COMPLETED,500.00\n"
           "CARD_PAYMENT,Current,2026-03-02 10:00:00,2026-03-03 08:00:00,AWS EMEA,-25.30,0.00,EUR,COMPLETED,474.70\n"
           "TRANSFER,Current,2026-03-04 11:00:00,2026-03-04 11:00:01,To ACME SL,-100.00,0.20,EUR,COMPLETED,374.50\n"
           "CARD_PAYMENT,Current,2026-03-05 12:00:00,,Hotel,-80.00,0.00,EUR,REVERTED,\n")
    movs, info = _movs(csv)
    assert movs == [("2026-03-01", 50000, "Top-Up by *1234"), ("2026-03-03", -2530, "AWS EMEA"),
                    ("2026-03-04", -10020, "To ACME SL")]
    assert info["columns"]["date"] == "Completed Date" and info["skipped_rows"] == [5]


def test_stripe_balance_uses_net():
    csv = ("id,Type,Source,Amount,Fee,Net,Currency,Created (UTC),Available On (UTC),Description\n"
           "txn_1,charge,ch_1,100.00,3.15,96.85,eur,2026-03-01 10:00,2026-03-08 00:00,Pedido 1001\n"
           "txn_2,refund,re_1,-20.00,0.00,-20.00,eur,2026-03-02 11:00,2026-03-02 11:00,Devolución 1001\n")
    movs, info = _movs(csv)
    assert movs == [("2026-03-01", 9685, "Pedido 1001"), ("2026-03-02", -2000, "Devolución 1001")]
    assert info["columns"]["amount"] == "Net"


def test_paypal_spanish_neto_and_pending_skipped():
    csv = ('"Fecha","Hora","Zona horaria","Nombre","Tipo","Estado","Divisa","Bruto","Tarifa","Neto","Saldo"\n'
           '"01/03/2026","10:00:00","CET","Cliente Uno","Pago recibido","Completado","EUR","50,00","-2,05","47,95","47,95"\n'
           '"02/03/2026","11:00:00","CET","Cliente Dos","Pago recibido","Pendiente","EUR","30,00","-1,37","28,63","47,95"\n')
    movs, info = _movs(csv)
    assert movs == [("2026-03-01", 4795, "Cliente Uno")] and info["columns"]["amount"] == "Neto"


def test_bbva_movimiento_column_before_importe():
    csv = ("F.Valor;Fecha;Concepto;Movimiento;Importe;Divisa;Disponible;Divisa;Observaciones\n"
           "02/03/2026;02/03/2026;Transferencia recibida;TALLERES RUIZ SL;1.210,00;EUR;11.210,00;EUR;\n"
           "05/03/2026;05/03/2026;Adeudo recibo;IBERDROLA;-121,00;EUR;11.089,00;EUR;\n")
    movs, info = _movs(csv)
    assert [m[1] for m in movs] == [121000, -12100]
    assert info["columns"]["amount"] == "Importe" and info["columns"]["date"] == "Fecha"


def test_caixabank_movimiento_text_column():
    csv = ("Concepto;Fecha;Importe;Saldo;Movimiento\n"
           "RECIBO LUZ;05/03/2026;-121,00;11.089,00;IBERDROLA\n")
    movs, info = _movs(csv)
    assert movs == [("2026-03-05", -12100, "RECIBO LUZ")] and info["columns"]["amount"] == "Importe"
