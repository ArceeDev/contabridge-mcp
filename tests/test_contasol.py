"""ContaSol APU.ods: layout A-Q (DELSOL GIP.C.01.10 rev. 9), deterministic bytes, errors before any charge."""
from __future__ import annotations

import base64
import csv
import io
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

import pytest

from contabridge_mcp import formats
from contabridge_mcp.journal import Entry, JournalError, Line
from test_e2e import DIARIO, balance, call, license_, server  # noqa: F401  (fixtures)

NS = {"office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
      "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
      "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}


def _entries() -> list[Entry]:
    return [Entry(7, date(2026, 3, 31), [Line("64000001", debit=150000, concept="Nómina marzo", document="N-03"),
                                         Line("47510001", credit=22500, concept="Nómina marzo"),
                                         Line("57200001", credit=127500, concept="Nómina\tmarzo")])]


def test_rows_follow_columns_a_to_q():
    rep = formats.Report()
    rows = [r.split("\t") for r in formats.write_contasol(_entries(), rep).split("\r\n") if r]
    assert len(rows) == 3 and all(len(r) == 17 for r in rows)
    assert rows[0] == ["0", "31/03/2026", "7", "1", "64000001", "0", "Nómina marzo", "N-03", "1500.00", "0.00",
                       "E", "0", "", "0", "0", "0", ""]
    assert [r[3] for r in rows] == ["1", "2", "3"]                   # Orden, unique inside the asiento
    assert rows[1][8:10] == ["0.00", "225.00"]                        # the other side is 0, as required
    assert rows[2][6] == "Nómina marzo"                                # a tab never splits a column
    assert rep.done() == []


def test_ods_is_valid_and_byte_identical():
    tsv = formats.write_contasol(_entries(), formats.Report())
    a, b = formats.encode("contasol", tsv), formats.encode("contasol", tsv)
    assert a == b
    z = zipfile.ZipFile(io.BytesIO(a))
    first = z.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    assert z.read("mimetype") == b"application/vnd.oasis.opendocument.spreadsheet"
    root = ET.fromstring(z.read("content.xml"))
    cells = root.find(".//table:table-row", NS).findall("table:table-cell", NS)
    vt = "{%s}value-type" % NS["office"]
    assert [c.get(vt) for c in cells[:5]] == ["float", "date", "float", "float", "float"]
    assert cells[1].get("{%s}date-value" % NS["office"]) == "2026-03-31"
    assert cells[8].get("{%s}value" % NS["office"]) == "1500.00"
    assert cells[6].get(vt) == "string"


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")
def test_libreoffice_opens_it(tmp_path):
    src = tmp_path / "APU.ods"
    src.write_bytes(formats.encode("contasol", formats.write_contasol(_entries(), formats.Report())))
    subprocess.run(["soffice", "--headless", "--convert-to", "csv:Text - txt - csv (StarCalc):59,34,76",
                    "--outdir", str(tmp_path), str(src)], check=True, capture_output=True, timeout=120)
    rows = list(csv.reader((tmp_path / "APU.csv").read_text("utf-8").splitlines(), delimiter=";"))
    assert len(rows) == 3 and rows[0][2:5] == ["7", "1", "64000001"] and rows[0][8] == "1500"


def test_refuses_what_contasol_would_reject():
    with pytest.raises(JournalError, match="1 a 99999"):
        formats.write_contasol([Entry(None, date(2026, 1, 1), [Line("57200001", debit=1)])], formats.Report())
    with pytest.raises(JournalError, match="10 dígitos"):
        formats.write_contasol([Entry(1, date(2026, 1, 1), [Line("Bank", debit=1)])], formats.Report())
    rep = formats.Report()
    formats.write_contasol([Entry(1, date(2026, 1, 1), [Line("5720001", debit=1), Line("4300001", credit=1),
                                                        Line("70000001", credit=0)])], rep)
    assert any("account_digits" in w for w in rep.done())
    with pytest.raises(JournalError, match="solo formato de destino"):
        formats.read("x", "contasol", formats.Report())


async def test_convert_to_contasol_over_mcp(server):  # noqa: F811
    before = await balance(server)
    err, data, text = await call(server, "convert_journal", content=DIARIO, target_format="contasol",
                                 account_digits=8, start_entry_number=1)
    assert not err, text
    assert data["filename"] == "APU.ods" and data["encoding"] == "binary"
    raw = base64.b64decode(data["content_base64"])
    assert zipfile.ZipFile(io.BytesIO(raw)).read("mimetype").startswith(b"application/vnd.oasis")
    assert data["content"].split("\t")[4] == "64000001"
    assert await balance(server) == before - data["billing"]["credits_charged"]
