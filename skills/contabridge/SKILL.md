---
name: contabridge-spanish-journals
description: Move Spanish bookkeeping between accounting programs and create journal entries. Convert a libro diario between a3ASESOR/a3ECO (SUENLACE.DAT), Sage ContaPlus / Sage 50 (XDiario) and Excel/Holded CSV, turn a Norma 43 bank statement into asientos, or validate a journal before import. Use for "pasar asientos a A3", "importar diario en ContaPlus", "enlace contable", "contabilizar el extracto", "ampliar dígitos de subcuenta".
---

# ContaBridge: Spanish journals (A3, ContaPlus, Sage 50, CSV)

Needs the `contabridge-mcp` server. Paid tools use KeyFlint credits. A file the target program would reject is refused before any charge, and failures are refunded.

## Setup

```json
{
  "mcpServers": {
    "contabridge": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ArceeDev/contabridge-mcp", "contabridge-mcp"],
      "env": { "KEYFLINT_LICENSE": "LIC-..." }
    }
  }
}
```

## When to use which tool

| The user has / wants | Tool | Credits |
|---|---|---|
| A diario from one program to import into another | `convert_journal` | 2 |
| Invoices (PDF, photo, Facturae) booked with VAT | `invoices_to_journal` | 3 |
| A bank statement (.n43, bank CSV or Excel read by you) booked as entries | `bank_to_journal` | 3 |
| Entries you composed (invoices, payroll, accruals) as an import file | `build_journal` | 2 |
| To check a diario before importing it | `validate_journal` | 1 |
| Resize a subaccount, identify a file format | `resize_account`, `detect_format` | 0 |

Formats: `a3` (512-byte records starting with `5`, or 256 starting with `4`), `contaplus_csv` (142 fields separated by `;`), `contaplus_ascii` (fixed width), `contasol` (ContaSol `APU.ods`, destination only: save `content_base64` as `APU.ods`), `csv` (any spreadsheet with a header).

## Workflow

1. **Pass the file untouched.** Send raw bytes as `content_base64` when it has Ñ or accents. Never retype it.
2. **Ask the target subaccount length** for ContaPlus or ContaSol (often 8 or 10) and pass `account_digits`. A3 accepts 6 to 12.
3. **Convert or book.** For `bank_to_journal` ask the 572 subaccount of the bank and build `rules` from the user's habits (supplier name → 4100xxxx or 62xxxxxx, AEB concept 17 commissions → 626, payroll → 465). First match wins. If the user also gave invoices, run `invoices_to_journal` first and pass its `open_items` so collections and payments land on each customer's 430 / supplier's 400.
4. **Save `content_base64` decoded to disk** with the returned `filename` (`SUENLACE.DAT`, `XDiario.csv`). Do not save `content` as UTF-8 for A3 or ContaPlus.
5. **Report** `summary` (entries, totals, balanced), every `warnings` line (truncated concepts, skipped invoice records), and for bank files the `unmatched` movements left in 555 for the user to assign.

## Rules

- Amounts are strings with 2 decimals; never use floats.
- Never invent a counterpart account: unmatched movements stay in the suspense account and are listed.
- After importing in ContaPlus, remind the user to renumber entries (ContaPlus appends imported entries).
- On error `-32062` (no credits) show the purchase link from the message.
