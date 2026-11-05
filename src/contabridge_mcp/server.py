"""ContaBridge MCP: move journal entries between Spanish accounting programs (A3, ContaPlus, Sage 50, CSV)
and turn Norma 43 bank statements into journal entries, for AI agents working for gestorías.

Paid tools debit KeyFlint credits only after the input has been read successfully, and refund them if the
tool body fails. Helper tools are free.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
from contextlib import contextmanager
from typing import Any, Iterator

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, bank, formats, journal, n43
from .billing import Billing

DEFAULT_PRICES = {"convert_journal": 2, "validate_journal": 1, "bank_to_journal": 3, "build_journal": 2}
MAX_BYTES = int(os.environ.get("CONTABRIDGE_MAX_BYTES", 10_000_000))
PREVIEW = 50


def _prices() -> dict[str, int]:
    prices = dict(DEFAULT_PRICES)
    for item in filter(None, (p.strip() for p in os.environ.get("CONTABRIDGE_PRICES", "").split(","))):
        name, _, n = item.partition("=")
        if name.strip() in prices and n.strip().isdigit() and int(n) > 0:
            prices[name.strip()] = int(n)
    return prices


PRICES = _prices()
billing = Billing(hosted=os.environ.get("CONTABRIDGE_TRANSPORT", "stdio") != "stdio")
LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True)

mcp = MCPServer(
    "contabridge-mcp",
    title="Puente contable: A3, ContaPlus, Sage y Norma 43",
    version=__version__,
    website_url="https://github.com/ArceeDev/contabridge-mcp",
    instructions=(
        "Spanish bookkeeping files for gestorías and accountants. `convert_journal` moves a libro diario between "
        "a3ASESOR/a3ECO (SUENLACE.DAT), Sage ContaPlus and Sage 50 (XDiario CSV/ASCII) and spreadsheet CSV "
        "(Holded, Excel, any ERP), resizing subaccounts (4300001 -> 43000001) and remapping accounts. "
        "`bank_to_journal` turns a Norma 43 statement into journal entries against the 572 bank subaccount with "
        "your rules; unmatched movements go to a suspense account (555) and are listed, never guessed. "
        "`build_journal` writes entries you composed into any of the formats. `validate_journal` runs the checks "
        "these programs run on import (balanced entries, debit xor credit, PGC groups, fiscal year). "
        "Amounts are strings with 2 decimals. Output files come as `content` and as `content_base64` in Latin-1 "
        "with CRLF, which is what A3 and ContaPlus read: save the base64 bytes to disk. Free: `resize_account`, "
        "`detect_format`, `pricing`, `credits_balance`. Unreadable input is never charged; failures are refunded."
    ),
)


@contextmanager
def _user_errors() -> Iterator[None]:
    try:
        yield
    except (journal.JournalError, n43.N43Error, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _headers(ctx: Context) -> Any:
    try:
        return ctx.headers
    except ValueError:
        return None


def _note(receipt: dict, credits: int) -> dict[str, Any]:
    return {"credits_charged": credits, "credits_remaining": receipt.get("remaining_balance")}


def _file(fmt: str, text: str) -> dict[str, Any]:
    raw = text.encode("latin-1", "replace") if fmt != "csv" else ("﻿" + text).encode("utf-8")
    return {"format": fmt, "filename": formats.EXTENSIONS[fmt], "encoding": "latin-1" if fmt != "csv" else "utf-8-sig",
            "content": text, "content_base64": base64.b64encode(raw).decode()}


def _prepare(entries: list[journal.Entry], account_digits: int, account_split: int,
             account_map: dict[str, str] | None, start_entry_number: int | None) -> list[journal.Entry]:
    if account_digits and not 4 <= account_digits <= 12:
        raise journal.JournalError("account_digits debe estar entre 4 y 12")
    entries = journal.remap(entries, account_map, account_digits or None, account_split)
    journal.number_entries(entries, start_entry_number if start_entry_number and start_entry_number > 0 else None)
    return entries


def _emit(entries: list[journal.Entry], target: str, rep: formats.Report, opts: dict[str, Any],
          strict: bool, fiscal_year: int | None) -> dict[str, Any]:
    issues = journal.validate(entries, fiscal_year)
    errors = [i for i in issues if i["level"] == "error"]
    if errors and strict:
        raise journal.JournalError(
            f"{len(errors)} error(es) de validación; el programa de destino rechazaría el fichero. Primero: "
            f"{errors[0]['message']} (asiento {errors[0]['entry']}). Usa strict=false para generarlo igualmente.")
    text = formats.write(entries, target, rep, **opts)
    return {**_file(target, text), "summary": journal.summary(entries), "issues": issues, "warnings": rep.done()}


@mcp.tool(annotations=LOCAL)
async def convert_journal(ctx: Context, target_format: str, content: str = "", content_base64: str = "",
                          source_format: str = "auto", account_digits: int = 0, account_split: int = 4,
                          account_map: dict[str, str] | None = None, start_entry_number: int = 0,
                          company_code: str = "00001", a3_format: int = 5, column_map: dict[str, str] | None = None,
                          csv_delimiter: str = "", decimal_comma: bool = True, fiscal_year: int = 0,
                          strict: bool = True) -> dict[str, Any]:
    """Convert a libro diario (journal) between Spanish accounting programs.

    Formats: `a3` (a3ASESOR/a3ECO/a3CON SUENLACE.DAT, apuntes tipo 0), `contaplus_csv` (Sage ContaPlus / Sage 50
    Diario CSV, 142 fields), `contaplus_ascii` (XDiario.txt fixed width, beta), `csv` (spreadsheet with header:
    Holded, Excel, any ERP; columns auto-detected, override with column_map). The source is auto-detected.

    Args:
        target_format: a3 | contaplus_csv | contaplus_ascii | csv.
        content: The file as text. content_base64: raw bytes as base64 (Latin-1/CP1252 files with Ñ).
        source_format: auto (default) or one of the formats.
        account_digits: Resize every subaccount to this length (ContaPlus uses a fixed length, e.g. 8);
            also expands '430.1' shorthand. 0 keeps them.
        account_split: Where zeros are inserted/removed when resizing (default 4: 4300001 -> 43000001).
        account_map: Exact account renames applied first, e.g. {"5720001": "57200005"}.
        start_entry_number: Renumber entries from this number (ContaPlus requires numbers; A3 does not use them).
        company_code: A3 company code (1-99999). a3_format: 5 (512-byte records, current) or 4 (256, old).
        column_map: For csv sources, e.g. {"debit": "Importe D", "account": "Cta"}. Keys: number, date, account,
            account_name, debit, credit, amount, dh, concept, document.
        csv_delimiter: For csv source/target; auto-detected on read, ';' on write.
        decimal_comma: csv output with decimal comma (Spanish Excel). fiscal_year: flag dates outside it.
        strict: Refuse to write a file with validation errors (default), so the target program never rejects it.
    """
    with _user_errors():
        text, encoding = n43.read_input(content, content_base64, MAX_BYTES)
    billing.client(_headers(ctx)).check()
    rep = formats.Report()
    with _user_errors():
        entries, src = await asyncio.to_thread(formats.read, text, source_format, rep, column_map, csv_delimiter)
        if not entries:
            raise journal.JournalError("el fichero no contiene apuntes")
        entries = _prepare(entries, account_digits, account_split, account_map, start_entry_number or None)
        opts = {"company_code": company_code, "a3_format": a3_format, "delimiter": csv_delimiter or ";",
                "decimal_comma": decimal_comma}
        if target_format not in formats.FORMATS:
            raise journal.JournalError(f"target_format debe ser uno de {', '.join(formats.FORMATS)}")
        # Built before the debit: a file the target program would reject is never charged.
        out = _emit(entries, target_format, rep, opts, strict, fiscal_year or None)
    async with billing.charge(PRICES["convert_journal"], "convert_journal", _headers(ctx)) as receipt:
        pass
    return {**out, "source_format": src, "source_encoding": encoding,
            "billing": _note(receipt, PRICES["convert_journal"])}


@mcp.tool(annotations=LOCAL)
async def validate_journal(ctx: Context, content: str = "", content_base64: str = "", source_format: str = "auto",
                           fiscal_year: int = 0, column_map: dict[str, str] | None = None,
                           csv_delimiter: str = "", include_entries: bool = False) -> dict[str, Any]:
    """Check a journal file the way A3/ContaPlus check it on import, before you send it.

    Reports unbalanced entries (with the difference), lines with both or neither debit and credit, non-numeric
    accounts or accounts outside PGC groups 1-9, mixed subaccount lengths and dates outside `fiscal_year`.
    `valid` is false when any error is found. include_entries returns the first 50 entries parsed.
    """
    with _user_errors():
        text, encoding = n43.read_input(content, content_base64, MAX_BYTES)
    billing.client(_headers(ctx)).check()
    rep = formats.Report()
    with _user_errors():
        entries, src = await asyncio.to_thread(formats.read, text, source_format, rep, column_map, csv_delimiter)
    async with billing.charge(PRICES["validate_journal"], "validate_journal", _headers(ctx)) as receipt:
        issues = journal.validate(entries, fiscal_year or None)
        out: dict[str, Any] = {"valid": not any(i["level"] == "error" for i in issues), "source_format": src,
                               "summary": journal.summary(entries), "issues": issues[:500],
                               "issues_total": len(issues), "warnings": rep.done()}
        if include_entries:
            out["entries"] = journal.entries_dict(entries, PREVIEW)
    return {**out, "billing": _note(receipt, PRICES["validate_journal"])}


@mcp.tool(annotations=LOCAL)
async def bank_to_journal(ctx: Context, bank_account: str, target_format: str = "csv", content: str = "",
                          content_base64: str = "", rules: list[dict[str, Any]] | None = None,
                          suspense_account: str = "", bank_accounts: dict[str, str] | None = None,
                          use_value_date: bool = False, account_digits: int = 0, account_split: int = 4,
                          start_entry_number: int = 1, company_code: str = "00001", a3_format: int = 5,
                          decimal_comma: bool = True) -> dict[str, Any]:
    """Turn a Norma 43 (Cuaderno 43) bank statement into journal entries ready to import.

    One entry per movement: abono -> Debe bank / Haber counterpart; cargo -> Debe counterpart / Haber bank.
    The counterpart comes from the first matching rule; with no match it goes to `suspense_account`
    (default 555 'partidas pendientes de aplicación', padded to the bank account length) and the movement is
    listed in `unmatched`, so nothing is guessed.

    Args:
        bank_account: The 572 subaccount of the bank, e.g. "57200001".
        target_format: csv (default) | a3 | contaplus_csv | contaplus_ascii.
        content / content_base64: The .n43 file (base64 for CP850/Latin-1 bytes).
        rules: Ordered list, first match wins. Each: {"account": "62800000", "contains": ["IBERDROLA"] or "regex":
            "...", "common_concept": "03" (AEB concept, e.g. 03 recibos, 04 transferencias, 12 comisiones),
            "direction": "cargo"|"abono", "concept": optional fixed text}. Text matching is case-insensitive
            over the movement description (own concept + record-23 texts).
        bank_accounts: For files with several bank accounts: {IBAN or last digits: "57200002"}.
        use_value_date: Date entries by value date instead of operation date.
        account_digits / account_split: Resize every subaccount (see convert_journal).
        start_entry_number: First entry number (default 1).
    """
    with _user_errors():
        text, _ = n43.read_input(content, content_base64, MAX_BYTES)
        parsed_rules = bank.build_rules(rules or [])
        if target_format not in formats.FORMATS:
            raise journal.JournalError(f"target_format debe ser uno de {', '.join(formats.FORMATS)}")
        if not bank_account.strip().isdigit() or not bank_account.strip().startswith("57"):
            raise journal.JournalError("bank_account debe ser una subcuenta numérica del grupo 57 (p. ej. 57200001)")
    billing.client(_headers(ctx)).check()
    with _user_errors():
        res = await asyncio.to_thread(n43.parse, text)
        n43_errors = [i for i in res.issues if i.get("severity") == "error"]
        suspense = suspense_account.strip() or "555".ljust(len(bank_account.strip()), "0")
        entries, stats = bank.movements_to_entries(res, bank_account, parsed_rules, suspense, bank_accounts,
                                                   use_value_date)
        if not entries:
            raise journal.JournalError("el extracto no contiene movimientos")
        entries = _prepare(entries, account_digits, account_split, None, start_entry_number or 1)
    rep = formats.Report()
    opts = {"company_code": company_code, "a3_format": a3_format, "delimiter": ";", "decimal_comma": decimal_comma}
    with _user_errors():
        out = _emit(entries, target_format, rep, opts, True, None)
    async with billing.charge(PRICES["bank_to_journal"], "bank_to_journal", _headers(ctx)) as receipt:
        pass
    stats["unmatched_total"] = len(stats["unmatched"])
    stats["unmatched"] = stats["unmatched"][:200]
    return {**out, "bank": stats, "norma43_issues": n43_errors[:50],
            "billing": _note(receipt, PRICES["bank_to_journal"])}


@mcp.tool(annotations=LOCAL)
async def build_journal(ctx: Context, entries: list[dict[str, Any]], target_format: str,
                        account_digits: int = 0, account_split: int = 4, start_entry_number: int = 0,
                        company_code: str = "00001", a3_format: int = 5, decimal_comma: bool = True,
                        fiscal_year: int = 0) -> dict[str, Any]:
    """Write journal entries you composed (e.g. from invoices or a payroll summary) as an import file.

    entries: [{"date": "2026-03-31", "number": 1 (optional), "lines": [{"account": "64000000", "debit": "1500.00",
    "concept": "Nómina marzo"}, {"account": "47510000", "credit": "225.00"}, ...]}]. Entries must balance:
    an unbalanced entry is refused before any charge, with the difference.
    """
    with _user_errors():
        parsed = journal.entries_from_json(entries)
        if not parsed:
            raise journal.JournalError("no hay asientos")
        if target_format not in formats.FORMATS:
            raise journal.JournalError(f"target_format debe ser uno de {', '.join(formats.FORMATS)}")
        parsed = _prepare(parsed, account_digits, account_split, None, start_entry_number or None)
        errors = [i for i in journal.validate(parsed, fiscal_year or None) if i["level"] == "error"]
        if errors:
            raise journal.JournalError("; ".join(f"asiento {e['entry']}: {e['message']}" for e in errors[:10]))
    billing.client(_headers(ctx)).check()
    rep = formats.Report()
    opts = {"company_code": company_code, "a3_format": a3_format, "delimiter": ";", "decimal_comma": decimal_comma}
    with _user_errors():
        out = _emit(parsed, target_format, rep, opts, True, fiscal_year or None)
    async with billing.charge(PRICES["build_journal"], "build_journal", _headers(ctx)) as receipt:
        pass
    return {**out, "billing": _note(receipt, PRICES["build_journal"])}


@mcp.tool(annotations=LOCAL)
def resize_account(account: str, digits: int, split: int = 4) -> dict[str, Any]:
    """Resize or expand a Spanish subaccount code. Free. '430.1' -> '43000001'; 4300001 (7) -> 43000001 (8)."""
    with _user_errors():
        out = journal.resize_account(account, digits, split)
    return {"account": out, "group": journal.PGC_GROUPS.get(out[:1])}


@mcp.tool(annotations=LOCAL)
def detect_format(content: str = "", content_base64: str = "") -> dict[str, Any]:
    """Tell which accounting import format a file is (a3, contaplus_csv, contaplus_ascii, csv). Free."""
    with _user_errors():
        text, encoding = n43.read_input(content, content_base64, MAX_BYTES)
        return {"format": formats.detect(text), "encoding": encoding}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def pricing() -> dict[str, Any]:
    """Credit price of each tool and where to buy credits. Free."""
    return {"credits_per_call": {**PRICES, "resize_account": 0, "detect_format": 0},
            "refunds": "Unreadable or invalid input is never charged; a tool that fails after charging is refunded.",
            "buy_credits": billing.checkout_url or "Set KEYFLINT_CHECKOUT_URL"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def credits_balance(ctx: Context) -> dict[str, Any]:
    """Remaining credits on your KeyFlint license. Free."""
    return await billing.balance(_headers(ctx))


def main() -> None:
    p = argparse.ArgumentParser(prog="contabridge-mcp", description=__doc__)
    p.add_argument("--http", action="store_true", help="Serve Streamable HTTP (hosted, license per request).")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    args = p.parse_args()
    if args.http:
        billing.hosted = True
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=True)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
