"""Tiny synthetic PDF writer for tests: text-only pages with Helvetica, no external dependency."""

from __future__ import annotations


def _esc(s: str) -> bytes:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252")


def make_pdf(pages: list[list[tuple[float, float, str]]], size: float = 9) -> bytes:
    """pages: [[(x, y, text), ...], ...] in points from the bottom-left of an A4 page."""
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
                         b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for items in pages:
        stream = b"".join(b"BT /F1 %g Tf %g %g Td (%s) Tj ET\n" % (size, x, y, _esc(t)) for x, y, t in items)
        objs.append(b"<< /Length %d >>\nstream\n%sendstream" % (len(stream), stream))
        content = len(objs)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                    b"/Contents %d 0 R >>" % content)
        kids.append(len(objs))
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def table_pdf(header: list[str], rows: list[list[str]], cols: list[float], top: list[str] = (),
              bottom: list[str] = (), rows_per_page: int = 40, right: set[int] = frozenset()) -> bytes:
    """Lay a statement table out like a bank PDF. `right` columns are right-aligned at cols[i] (approx.)."""
    def line(cells: list[str], y: float) -> list[tuple[float, float, str]]:
        out = []
        for i, c in enumerate(cells):
            if c:
                x = cols[i] - len(c) * 4.6 if i in right else cols[i]
                out.append((x, y, c))
        return out

    pages, chunks = [], [rows[i:i + rows_per_page] for i in range(0, len(rows), rows_per_page)] or [[]]
    for n, chunk in enumerate(chunks):
        y, items = 800.0, []
        for t in (top if n == 0 else []):
            items.append((40, y, t)); y -= 14
        items += line(header, y); y -= 14
        for r in chunk:
            items += line(r, y); y -= 12
        if n == len(chunks) - 1:
            for t in bottom:
                y -= 4; items.append((40, y, t)); y -= 12
        pages.append(items)
    return make_pdf(pages)
