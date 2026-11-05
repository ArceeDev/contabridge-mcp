"""MCP end to end: tools over the protocol, debits, no charge on bad input, paywall, hosted licenses."""

from __future__ import annotations

import base64
import importlib
import json

import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError

from conftest import PUB
from n43_builder import sample_statement
from test_core import RULES

DIARIO = ("Asiento;Fecha;Cuenta;Concepto;Debe;Haber\n"
          "1;31/03/2026;6400001;Nómina marzo;1.500,00;\n1;31/03/2026;4751001;Nómina marzo;;225,00\n"
          "1;31/03/2026;5720001;Nómina marzo;;1.275,00\n")


@pytest.fixture(scope="module")
def license_(gateway):
    return gateway["issue"]("buyer@gestoria.test")


def _env(monkeypatch, gateway, key):
    monkeypatch.setenv("KEYFLINT_LICENSE", key)
    monkeypatch.setenv("KEYFLINT_PUBLIC_KEYS", PUB)
    monkeypatch.setenv("KEYFLINT_URL", gateway["url"])
    monkeypatch.setenv("KEYFLINT_CHECKOUT_URL", "https://example.com/buy")
    monkeypatch.setenv("KEYFLINT_CLOCK_STATE", "off")
    monkeypatch.delenv("CONTABRIDGE_TRANSPORT", raising=False)
    monkeypatch.delenv("CONTABRIDGE_PRICES", raising=False)


@pytest.fixture()
def server(gateway, license_, monkeypatch):
    _env(monkeypatch, gateway, license_["license_key"])
    import contabridge_mcp.server as srv

    return importlib.reload(srv)


async def call(srv, name, **args):
    async with Client(srv.mcp) as c:
        res = await c.call_tool(name, args)
    text = res.content[0].text if res.content else ""
    data = res.structured_content or (json.loads(text) if text.startswith("{") else None)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return res.is_error, data, text


async def balance(srv):
    _, data, _ = await call(srv, "credits_balance")
    return data["balance"]


async def test_tools_listed(server):
    async with Client(server.mcp) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
    assert {"convert_journal", "validate_journal", "bank_to_journal", "build_journal", "resize_account",
            "detect_format", "pricing", "credits_balance"} <= tools


async def test_convert_csv_to_a3_charges_and_resizes(server):
    before = await balance(server)
    err, data, text = await call(server, "convert_journal", content=DIARIO, target_format="a3", account_digits=8)
    assert not err, text
    assert data["source_format"] == "csv" and data["filename"] == "SUENLACE.DAT"
    raw = base64.b64decode(data["content_base64"])
    recs = raw.split(b"\r\n")[:-1]
    assert len(recs) == 3 and all(len(r) == 510 for r in recs)
    assert recs[0][15:27] == b"64000001    "
    assert "Nómina".encode("latin-1") in recs[0]
    assert data["billing"]["credits_charged"] == 2
    assert await balance(server) == before - 2


async def test_convert_a3_to_contaplus_round_trip(server):
    _, a3, _ = await call(server, "convert_journal", content=DIARIO, target_format="a3", account_digits=8)
    err, cp, text = await call(server, "convert_journal", content_base64=a3["content_base64"],
                               target_format="contaplus_csv", start_entry_number=101)
    assert not err, text
    assert cp["source_format"] == "a3" and cp["summary"]["total_debit"] == "1500.00"
    assert cp["content"].split(";")[0] == "101"


async def test_unbalanced_file_is_refused_free(server):
    before = await balance(server)
    bad = DIARIO.replace("1.275,00", "1.270,00")
    err, _, text = await call(server, "convert_journal", content=bad, target_format="a3")
    assert err and "descuadrado" in text
    assert await balance(server) == before


async def test_garbage_is_free(server):
    before = await balance(server)
    err, _, text = await call(server, "convert_journal", content="hola\nmundo\n", target_format="a3")
    assert err and "columnas" in text
    assert await balance(server) == before


async def test_validate(server):
    err, data, text = await call(server, "validate_journal", content=DIARIO, fiscal_year=2025)
    assert not err, text
    assert data["valid"] is False and "fuera del ejercicio" in data["issues"][0]["message"]
    assert data["billing"]["credits_charged"] == 1


async def test_bank_to_journal(server):
    err, data, text = await call(server, "bank_to_journal", content=sample_statement(), bank_account="57200001",
                                 rules=RULES, target_format="contaplus_csv")
    assert not err, text
    assert data["bank"]["matched"] == 7 and data["bank"]["unmatched_total"] == 1
    assert data["summary"]["entries"] == 8 and data["summary"]["balanced"]
    assert data["billing"]["credits_charged"] == 3
    assert "55500000" in data["content"]


async def test_bank_to_journal_bad_account_is_free(server):
    before = await balance(server)
    err, _, text = await call(server, "bank_to_journal", content=sample_statement(), bank_account="43000000")
    assert err and "grupo 57" in text
    assert await balance(server) == before


async def test_build_journal(server):
    entries = [{"date": "2026-04-30", "lines": [{"account": "62900000", "debit": "10,00", "concept": "Correos"},
                                                {"account": "57000000", "credit": "10,00"}]}]
    err, data, text = await call(server, "build_journal", entries=entries, target_format="csv",
                                 start_entry_number=7)
    assert not err, text
    assert data["content"].splitlines()[1].startswith("7;30/04/2026;62900000")
    entries[0]["lines"][1]["credit"] = "9,00"
    err, _, text = await call(server, "build_journal", entries=entries, target_format="a3")
    assert err and "descuadrado" in text


async def test_free_tools(server):
    _, data, _ = await call(server, "resize_account", account="430.1", digits=8)
    assert data["account"] == "43000001" and data["group"].startswith("Acreedores")
    _, data, _ = await call(server, "detect_format", content=DIARIO)
    assert data["format"] == "csv"
    _, data, _ = await call(server, "pricing")
    assert data["credits_per_call"]["bank_to_journal"] == 3


async def test_no_license_is_refused(gateway, monkeypatch):
    _env(monkeypatch, gateway, "LIC-not-a-real-license")
    import contabridge_mcp.server as srv

    srv = importlib.reload(srv)
    async with Client(srv.mcp) as c:
        with pytest.raises(MCPError) as exc:
            await c.call_tool("convert_journal", {"content": DIARIO, "target_format": "a3"})
    assert exc.value.code == -32060 and "example.com/buy" in str(exc.value)


async def test_hosted_requires_license_header(gateway, license_, monkeypatch):
    _env(monkeypatch, gateway, license_["license_key"])
    monkeypatch.setenv("CONTABRIDGE_TRANSPORT", "http")
    import contabridge_mcp.server as srv

    srv = importlib.reload(srv)
    with pytest.raises(Exception) as exc:
        srv.billing.client(None)
    assert "Bearer" in str(exc.value)
    assert srv.billing.client({"Authorization": f"Bearer {license_['license_key']}"}) is not None
