# contabridge-mcp

An MCP server that moves bookkeeping between Spanish accounting programs, built for AI agents that work for gestorías and accounting firms with many clients. It does not store any data between calls, and every result is deterministic.

| Tool | Credits | What it does |
| --- | --- | --- |
| `convert_journal` | 2 | Converts a libro diario between **a3ASESOR/a3ECO/a3CON** (`SUENLACE.DAT`), **Sage ContaPlus / Sage 50** (Diario CSV of 142 fields, or XDiario ASCII) and **spreadsheet CSV** (Holded, Excel, any ERP). It resizes subaccounts (`4300001` → `43000001`, `430.1` → `43000001`), renames accounts and renumbers entries. |
| `bank_to_journal` | 3 | Turns a **Norma 43** bank statement into journal entries against the 572 subaccount. Ordered rules map movements to counterpart accounts (by text, regex, AEB concept or direction), and movements that match no rule go to 555 and are listed. |
| `build_journal` | 2 | Writes entries the agent composed (invoices, payroll, accruals) in any of the formats. |
| `validate_journal` | 1 | Runs the checks A3 and ContaPlus run on import: balanced entries, debit or credit on each line (never both), PGC groups, fiscal year, mixed account lengths. |
| `resize_account`, `detect_format`, `pricing`, `credits_balance` | 0 | Free helpers. |

Output files are returned as `content` and as `content_base64`. The base64 version is Windows-1252 with CRLF line endings (UTF-8 with BOM for CSV), which is what these programs import.

Charging works like this:
- A file that would be rejected (unbalanced, unreadable, missing columns) is refused **before** any credits are charged.
- A failure after charging is refunded.

## Formats and sources

- **A3**: "Enlace contable de entrada. Descripción de registros" (Wolters Kluwer). Record type 0, format 5 (512 bytes) and format 4 (256 bytes). When reading, invoice records (types 1, 2 and 9) are skipped and reported in a warning.
- **ContaPlus**: "Protocolo de comunicación de programas de gestión con ContaPlus" (Sage, 2023). Writes Asien, Fecha, SubCta, Contra, Concepto, Documento, MonedaUso=2, EuroDebe and EuroHaber. The fixed-width ASCII variant (32 fields) uses the same layout as the open-source BulmaGés exporter.
- **CSV**: columns are detected in Spanish and English (`fecha`, `cuenta`, `debe`, `haber`, `importe`+`d/h`…), or set with `column_map`. A file without an entry number is grouped into entries by accumulating lines until they balance.

## Install

All you need is your KeyFlint license (`LIC-...`). The gateway URL, its public key and the checkout link have defaults.

**Claude Desktop, one click:** download `contabridge-mcp-X.Y.Z.mcpb` from the [latest release](https://github.com/ArceeDev/contabridge-mcp/releases/latest), double-click it and paste your license. No terminal, no JSON.

**Claude Code:**

```bash
claude mcp add contabridge -e KEYFLINT_LICENSE=LIC-... -- uvx contabridge-mcp
```

**Cursor, Windsurf, Claude Desktop (JSON):**

```json
{
  "mcpServers": {
    "contabridge": {
      "command": "uvx",
      "args": ["contabridge-mcp"],
      "env": { "KEYFLINT_LICENSE": "LIC-..." }
    }
  }
}
```

Optional: `KEYFLINT_URL`, `KEYFLINT_PUBLIC_KEYS` (fetched from the gateway and cached in `~/.keyflint/keyring.json` when unset), `KEYFLINT_CHECKOUT_URL`.

Hosted mode: `CONTABRIDGE_TRANSPORT=http contabridge-mcp --http` (license per request, `Authorization: Bearer LIC-...`).

Tuning: `CONTABRIDGE_PRICES` (`convert_journal=3,bank_to_journal=5`), `CONTABRIDGE_MAX_BYTES`.

## Claude skill

`skills/contabridge/SKILL.md` (zipped in `distribution/contabridge-skill.zip`) tells the agent which tool to use, which account length to ask for, and how to save the file.

## Tests

```bash
pip install -e '.[test]' && pytest
```

The tests use a fake KeyFlint gateway and locally signed licenses, so they need no network. A3 and ContaPlus layouts are checked position by position against the official specifications.

MIT
