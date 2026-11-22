"""Fast deterministic document extraction (§5.4, §10, §19.6).

Features:
  - Fast native PDF extraction using pypdfium2 (zero-subprocess, native C++)
  - Zero-dependency DOCX and XLSX text extraction (using stdlib zipfile + XML regex)
  - Standard library email/EML/MSG parsing
  - Text, markdown, JSON, CSV, TSV parsing with encoding fallback
  - Honest reviewability states: READ, READ_OCR, PARTIAL, UNREADABLE_NEEDS_OCR, EMPTY
  - Chunking with locators ("p.1", "para 1", "Sheet!row 1")
"""

from __future__ import annotations

import csv
import email
import email.policy
import html
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from keel.policy import taint


@dataclass
class ExtractionResult:
    state: str  # READ, READ_OCR, PARTIAL, UNREADABLE_NEEDS_OCR, EMPTY, ERROR
    reason: str
    text: str
    pages: int | None = None
    chunks: list[tuple[str, str]] = field(default_factory=list)  # (locator, text)
    metadata: dict[str, Any] = field(default_factory=dict)


def _chunk_units(units: list[tuple[str, str]], max_chunk_len: int = 1500) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    for loc, text in units:
        clean = text.strip()
        if not clean:
            continue
        if len(clean) <= max_chunk_len:
            chunks.append((loc, clean))
            continue
        # Split larger blocks cleanly at newlines or sentences
        start = 0
        part = 1
        while start < len(clean):
            end = min(len(clean), start + max_chunk_len)
            if end < len(clean):
                nl = clean.rfind("\n", start + max_chunk_len // 2, end)
                if nl > start:
                    end = nl
                else:
                    dot = clean.rfind(". ", start + max_chunk_len // 2, end)
                    if dot > start:
                        end = dot + 1
            chunks.append((f"{loc} ¶{part}", clean[start:end].strip()))
            start = end
            part += 1
    return chunks


def _extract_pdf(path: Path) -> ExtractionResult:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ExtractionResult(
            state="ERROR",
            reason="pypdfium2 is not installed; PDF extraction unavailable",
            text="",
        )

    doc = pdfium.PdfDocument(str(path))
    num_pages = len(doc)
    units: list[tuple[str, str]] = []
    empty_pages: list[int] = []

    for i in range(num_pages):
        page = doc[i]
        textpage = page.get_textpage()
        page_text = textpage.get_text_range() or ""
        page_num = i + 1
        if len(page_text.strip()) < 15:
            empty_pages.append(page_num)
        units.append((f"p.{page_num}", page_text))

    full_text = "\n\n".join(f"--- Page {loc} ---\n{t}" for loc, t in units if t.strip())

    if empty_pages:
        if len(empty_pages) == num_pages:
            state = "UNREADABLE_NEEDS_OCR"
            reason = f"all {num_pages} pages have no text layer (scanned/raster PDF); OCR required"
        else:
            state = "PARTIAL"
            reason = f"{len(empty_pages)}/{num_pages} pages lack a text layer; remaining pages read natively"
    else:
        state = "READ"
        reason = "all pages read natively"

    return ExtractionResult(
        state=state,
        reason=reason,
        text=full_text,
        pages=num_pages,
        chunks=_chunk_units(units),
    )


def _extract_docx(path: Path) -> ExtractionResult:
    # Fast stdlib zipfile extraction of word/document.xml
    try:
        with zipfile.ZipFile(str(path)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as exc:
        return ExtractionResult(state="ERROR", reason=f"failed reading docx archive: {exc}", text="")

    # Match paragraphs (<w:p>)
    paragraphs = re.findall(r"<w:p\b[^>]*>(.*?)</w:p>", xml, flags=re.DOTALL)
    units: list[tuple[str, str]] = []
    p_num = 1
    for p_xml in paragraphs:
        # Extract text in <w:t> tags
        texts = re.findall(r"<w:t\b[^>]*>(.*?)</w:t>", p_xml)
        para_text = html.unescape("".join(texts)).strip()
        if para_text:
            units.append((f"para {p_num}", para_text))
            p_num += 1

    # Extract tables (<w:tbl>)
    tables = re.findall(r"<w:tbl\b[^>]*>(.*?)</w:tbl>", xml, flags=re.DOTALL)
    for t_idx, tbl_xml in enumerate(tables, 1):
        rows = re.findall(r"<w:tr\b[^>]*>(.*?)</w:tr>", tbl_xml, flags=re.DOTALL)
        for r_idx, row_xml in enumerate(rows, 1):
            cells = re.findall(r"<w:tc\b[^>]*>(.*?)</w:tc>", row_xml, flags=re.DOTALL)
            cell_texts = []
            for c_xml in cells:
                t = html.unescape("".join(re.findall(r"<w:t\b[^>]*>(.*?)</w:t>", c_xml))).strip()
                cell_texts.append(t)
            if any(cell_texts):
                units.append((f"table {t_idx} row {r_idx}", " | ".join(cell_texts)))

    full_text = "\n\n".join(t for _, t in units)
    state = "READ" if units else "EMPTY"
    reason = "docx content extracted" if units else "document contains no readable text"
    return ExtractionResult(
        state=state,
        reason=reason,
        text=full_text,
        chunks=_chunk_units(units),
    )


def _extract_xlsx(path: Path) -> ExtractionResult:
    # Fast stdlib zipfile extraction of xl/sharedStrings.xml + xl/worksheets/sheet*.xml
    try:
        with zipfile.ZipFile(str(path)) as z:
            # Read shared strings if present
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in z.namelist():
                ss_xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                for si in re.findall(r"<si\b[^>]*>(.*?)</si>", ss_xml, flags=re.DOTALL):
                    t = html.unescape("".join(re.findall(r"<t\b[^>]*>(.*?)</t>", si)))
                    shared_strings.append(t)

            units: list[tuple[str, str]] = []
            sheet_files = sorted([n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")])

            for s_idx, sf in enumerate(sheet_files, 1):
                s_xml = z.read(sf).decode("utf-8", "replace")
                rows = re.findall(r"<row\b[^>]*\br=\"(\d+)\"[^>]*>(.*?)</row>", s_xml, flags=re.DOTALL)
                for r_num, row_xml in rows:
                    cells = re.findall(r"<c\b[^>]*\br=\"([A-Z0-9]+)\"(?:[^>]*\bt=\"([a-z]+)\")?[^>]*>(.*?)</c>", row_xml, flags=re.DOTALL)
                    row_vals: list[str] = []
                    for ref, c_type, c_content in cells:
                        val_m = re.search(r"<v>(.*?)</v>", c_content)
                        if not val_m:
                            continue
                        val_raw = val_m.group(1)
                        if c_type == "s" and shared_strings:
                            val = shared_strings[int(val_raw)] if int(val_raw) < len(shared_strings) else val_raw
                        else:
                            val = val_raw
                        row_vals.append(f"{ref}:{val}")
                    if row_vals:
                        units.append((f"Sheet{s_idx}!row {r_num}", " | ".join(row_vals)))

        full_text = "\n".join(t for _, t in units)
        return ExtractionResult(
            state="READ" if units else "EMPTY",
            reason="xlsx structured cells extracted",
            text=full_text,
            chunks=_chunk_units(units),
        )
    except Exception as exc:
        return ExtractionResult(state="ERROR", reason=f"failed parsing xlsx: {exc}", text="")


def _extract_email(path: Path) -> ExtractionResult:
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    headers = [
        f"From: {msg.get('From', '')}",
        f"To: {msg.get('To', '')}",
        f"Subject: {msg.get('Subject', '')}",
        f"Date: {msg.get('Date', '')}",
    ]
    body_parts = []
    attachments = []

    for part in msg.walk():
        cd = part.get_content_disposition()
        if cd == "attachment":
            fn = part.get_filename() or "unnamed_attachment"
            attachments.append(fn)
            continue

        ct = part.get_content_type()
        if ct == "text/plain":
            body_parts.append(part.get_content())
        elif ct == "text/html" and not body_parts:
            html_text = re.sub(r"<[^>]+>", " ", part.get_content())
            body_parts.append(html.unescape(html_text))

    units = [("headers", "\n".join(headers))]
    for i, b in enumerate(body_parts, 1):
        units.append((f"body {i}", b))
    if attachments:
        units.append(("attachments_list", "Attachments: " + ", ".join(attachments)))

    full_text = "\n\n".join(t for _, t in units)
    return ExtractionResult(
        state="READ",
        reason="email headers and body parsed",
        text=full_text,
        chunks=_chunk_units(units),
        metadata={"attachments": attachments},
    )


def extract_document(path: Path | str) -> ExtractionResult:
    """Extracts text from a document based on file extension."""
    p = Path(path)
    if not p.is_file():
        return ExtractionResult(state="ERROR", reason=f"file not found: {p}", text="")

    suffix = p.suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(p)
    if suffix in (".docx", ".docm", ".dotx"):
        return _extract_docx(p)
    if suffix in (".xlsx", ".xlsm"):
        return _extract_xlsx(p)
    if suffix in (".eml", ".msg"):
        return _extract_email(p)
    if suffix in (".txt", ".md", ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".html", ".htm"):
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1", errors="replace")

        lines = text.splitlines()
        units = [(f"line {i}", ln) for i, ln in enumerate(lines, 1) if ln.strip()]
        return ExtractionResult(
            state="READ" if text.strip() else "EMPTY",
            reason="plain text loaded",
            text=text,
            chunks=_chunk_units(units),
        )

    return ExtractionResult(
        state="UNSUPPORTED",
        reason=f"unsupported document format {suffix!r}",
        text="",
    )
