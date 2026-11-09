# contabridge-mcp

<!-- mcp-name: io.github.ArceeDev/contabridge-mcp -->

An MCP server that moves bookkeeping between Spanish accounting programs and QuickBooks / Xero, built for AI agents that work for gestorías and accounting firms with many clients. It does not store any data between calls, and every result is deterministic.

| Tool | Credits | What it does |
| --- | --- | --- |
| `convert_journal` | 2 | Converts a libro diario between **a3ASESOR/a3ECO/a3CON** (`SUENLACE.DAT`), **Sage ContaPlus / Sage 50** (Diario CSV of 142 fields, or XDiario ASCII), **ContaSol** (`APU.ods`, destination only), **spreadsheet CSV** (Holded, Excel, any ERP), **QuickBooks** (Desktop IIF, Online journal CSV) and **Xero** (manual journal CSV). It resizes subaccounts (`4300001` → `43000001`, `430.1` → `43000001`), renames accounts and renumbers entries. |
| `invoices_to_journal` | 3 | Books **issued and received invoices** (and rectificativas) with VAT, recargo de equivalencia and IRPF split out (430/700/477, 600/472/4751/400). Checks base + IVA - IRPF = total; invoices that do not add up go to `review`, never booked. Returns the VAT summary per rate (libro registro / modelo 303) and `open_items` for `bank_to_journal`. |
| `bank_to_journal` | 3 | Turns a **Norma 43** statement, the bank's **CSV export** or a movements list into journal entries against the 572 subaccount. Pending invoices (`open_items`) are settled first against the customer/supplier subaccount on same amount + reference or name; then ordered rules (text, regex, AEB concept, direction); the rest go to 555 and are listed. Ties are never guessed. |
| `pdf_bank_to_journal` | 3 | Reads a **digital bank statement PDF** (selectable text: Revolut, Wise, N26, CaixaBank, BBVA, Santander... downloaded from online banking) deterministically, with no OCR and no AI. Checks every row against the running balance (previous balance + amount = printed balance) and opening + in - out = closing; rows that do not add up are flagged, rows whose sign cannot be proven go to `review` and are not booked. Returns ContaSol (APU.ods), A3, ContaPlus, CSV, QuickBooks/Xero or a Norma 43 (.n43) file. `strict_balance=true` refuses (free) unless everything adds up. |
| `build_journal` | 2 | Writes entries the agent composed (invoices, payroll, accruals) in any of the formats. |
| `bank_statement_csv` | 1 | Turns a **Norma 43** statement into a bank statement CSV for **Xero** (precoded template) or **QuickBooks Online** (3 columns). |
| `validate_journal` | 1 | Runs the checks A3 and ContaPlus run on import: balanced entries, debit or credit on each line (never both), PGC groups, fiscal year, mixed account lengths. |
| `resize_account`, `detect_format`, `pricing`, `credits_balance` | 0 | Free helpers. |

Output files are returned as `content` and as `content_base64`. The base64 version is Windows-1252 with CRLF line endings (UTF-8 with BOM for CSV), which is what these programs import.

Charging works like this:
- A file that would be rejected (unbalanced, unreadable, missing columns) is refused **before** any credits are charged.
- A failure after charging is refunded.

## Formats and sources

- **A3**: "Enlace contable de entrada. Descripción de registros" (Wolters Kluwer). Record type 0, format 5 (512 bytes) and format 4 (256 bytes). When reading, invoice records (types 1, 2 and 9) are skipped and reported in a warning.
- **ContaPlus**: "Protocolo de comunicación de programas de gestión con ContaPlus" (Sage, 2023). Writes Asien, Fecha, SubCta, Contra, Concepto, Documento, MonedaUso=2, EuroDebe and EuroHaber. The fixed-width ASCII variant (32 fields) uses the same layout as the open-source BulmaGés exporter.
- **ContaSol**: "Importación Excel / Calc" (Software DELSOL, GIP.C.01.10 rev. 9, 2023), Apuntes file `APU.ods`, columns A to Q, no header row, Moneda `E`, Diario `0`. The `.ods` bytes are deterministic. Import it in ContaSol from Utilidades > Importaciones. Subaccounts must have the company's digits: pass `account_digits`.
- **QuickBooks Desktop IIF**: `GENERAL JOURNAL` transactions (`!TRNS`/`!SPL`/`!ENDTRNS`, tab separated). Debits are positive and credits negative, so the TRNS amount is minus the sum of its SPL lines. Other IIF lists (accounts, customers) are skipped with a warning.
- **QuickBooks Online**: "Import journal entries" CSV with `Journal No., Journal Date, Account Name, Debits, Credits, Description, Name`.
- **Xero**: "Import a manual journal" template (`*Narration, *Date, Description, *AccountCode, *TaxRate, *Amount`, tracking). Lines with the same Narration and Date form one journal. `xero_tax_rate` sets the TaxRate as named in that organisation (UK `No VAT`, AU `BAS Excluded`, NZ `No GST`, US `Tax Exempt`).
- QuickBooks and Xero dates follow `date_style`: `mdy` (default for QuickBooks), `dmy` (default for Xero) or `iso`. Amounts use a decimal point, and a comma is always read as a thousands separator. Their accounts are names or codes, so they are validated against their own chart, not the PGC. Use `account_map` to translate them when converting to or from A3/ContaPlus.
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

## Visual cards (MCP Apps)

In Claude, ChatGPT, VS Code and Goose every paid tool and `credits_balance` renders as an interactive card in the
chat: credit balance with a refill bar and a buy button, or a result summary (key figures, issues, table preview and
credits charged). It is the `ui://contabridge/card.html` resource, self-contained with no network access. Other clients
keep getting the same JSON as before.

## Claude skill

`skills/contabridge/SKILL.md` (zipped in `distribution/contabridge-skill.zip`) tells the agent which tool to use, which account length to ask for, and how to save the file.

## Tests

```bash
pip install -e '.[test]' && pytest
```

The tests use a fake KeyFlint gateway and locally signed licenses, so they need no network. A3 and ContaPlus layouts are checked position by position against the official specifications.

## License

Business Source License 1.1. Source available; production use needs a KeyFlint license (or must be non-commercial). Each version becomes Apache 2.0 four years after release. See [LICENSE](LICENSE).
