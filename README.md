# contabridge-mcp

An MCP server that moves bookkeeping between Spanish accounting programs, built for AI agents that work for gestorías and accounting firms with many clients. It does not store any data between calls, and every result is deterministic.

| Tool | Credits | What it does |
| --- | --- | --- |
| `convert_journal` | 2 | Converts a libro diario between **a3ASESOR/a3ECO/a3CON** (`SUENLACE.DAT`), **Sage ContaPlus / Sage 50** (Diario CSV of 142 fields, or XDiario ASCII) and **spreadsheet CSV** (Holded, Excel, any ERP). It resizes subaccounts (`4300001` → `43000001`, `430.1` → `43000001`), renames accounts and renumbers entries. |
| `bank_to_journal` | 3 | Turns a **Norma 43** bank statement into journal entries against the 572 subaccount. Ordered rules map movements to counterpart accounts (by text, regex, AEB concept or direction), and movements that match no rule go to 555 and are listed. |
| `build_journal` | 2 | Writes entries the agent composed (invoices, payroll, accruals) in any of the formats. |
| `validate_journal` | 1 | Runs the checks A3 and ContaPlus run on import: balanced entries, debit or credit on each line (never both), PGC groups, fiscal year, mixed account lengths. |
| `resize_account`, `detect_format`, `pricing`, `credits_balance` | 0 | Free helpers. |

Output files are returned as `content` and as `content_base64`. The base64 version is Latin-1 with CRLF line endings (UTF-8 with BOM for CSV), which is what these programs import.

Charging works like this:
- A file that would be rejected (unbalanced, unreadable, missing columns) is refused **before** any credits are charged.
- A failure after charging is refunded.

## Formats and sources

- **A3**: "Enlace contable de entrada. Descripción de registros" (Wolters Kluwer). Record type 0, format 5 (512 bytes) and format 4 (256 bytes). When reading, invoice records (types 1, 2 and 9) are skipped and reported in a warning.
- **ContaPlus**: "Protocolo de comunicación de programas de gestión con ContaPlus" (Sage, 2023). Writes Asien, Fecha, SubCta, Contra, Concepto, Documento, MonedaUso=2, EuroDebe and EuroHaber. The fixed-width ASCII variant is **beta**: check it against an export from your own version.
- **CSV**: columns are detected in Spanish and English (`fecha`, `cuenta`, `debe`, `haber`, `importe`+`d/h`…), or set with `column_map`. A file without an entry number is grouped into entries by accumulating lines until they balance.

## Install

```bash
pip install git+https://github.com/ArceeDev/contabridge-mcp
KEYFLINT_LICENSE=LIC-... contabridge-mcp            # stdio
CONTABRIDGE_TRANSPORT=http contabridge-mcp --http    # hosted; license per request (Authorization: Bearer LIC-...)
```

Environment variables: `KEYFLINT_LICENSE`, `KEYFLINT_URL`, `KEYFLINT_PUBLIC_KEYS`, `KEYFLINT_CHECKOUT_URL`, `CONTABRIDGE_PRICES` (`convert_journal=3,bank_to_journal=5`), `CONTABRIDGE_MAX_BYTES`.

## Tests

```bash
pip install -e '.[test]' && pytest
```

The tests use a fake KeyFlint gateway and locally signed licenses, so they need no network. A3 and ContaPlus layouts are checked position by position against the official specifications.

MIT
