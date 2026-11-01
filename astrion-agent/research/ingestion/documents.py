"""Bounded PDF/text extraction. OCR is explicit and never implies faithful layout."""

from __future__ import annotations
import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_FILE_BYTES = 25_000_000
MAX_TEXT_CHARS = 2_000_000


def extract_document(name, data, *, ocr=False):
    suffix = Path(name).suffix.lower()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("document_too_large: limit 25 MB")
    if suffix in {".md", ".txt"}:
        if len(data) > 1_000_000:
            raise ValueError("资料超过 1 MB")
        text = data.decode("utf-8")
        if not text.strip():
            raise ValueError("资料不能为空")
        return text, {"format": suffix[1:], "pages": [], "sections": sections(text), "warnings": []}
    if suffix != ".pdf":
        raise ValueError("支持 UTF-8 TXT/MD 和 PDF")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("pdf_unavailable: install requirements-research.txt") from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted_pdf: decrypt before importing")
        if not 1 <= len(reader.pages) <= 300:
            raise ValueError("pdf_page_limit: 1..300 pages")
    except Exception as exc:
        raise ValueError(f"pdf_invalid: {type(exc).__name__}") from exc
    pages, texts, line, total = [], [], 1, 0
    for index, page in enumerate(reader.pages):
        warnings = ["layout_unverified", "tables_and_equations_may_be_incomplete"]
        method = "pdf_text"
        try:
            body = page.extract_text() or ""
        except Exception:
            body = ""
            warnings.append("text_extraction_failed")
        scan = len(re.sub(r"\s", "", body)) < 30
        if scan:
            warnings.append("scan_or_sparse_page")
            if ocr:
                try:
                    body = ocr_page(data, index)
                    method = "ocr"
                    warnings.append("ocr_unverified")
                except (ImportError, ValueError, OSError, subprocess.SubprocessError) as exc:
                    warnings.append("ocr_failed:" + type(exc).__name__)
        if not body.strip():
            body = f"[Page {index + 1}: no readable text extracted]"
            warnings.append("missing_text")
        body = body.strip()
        total += len(body)
        if total > MAX_TEXT_CHARS:
            raise ValueError("extracted_text_too_large")
        end = line + len(body.splitlines()) - 1
        pages.append(
            {
                "page": index + 1,
                "start_line": line,
                "end_line": end,
                "method": method,
                "scan_suspected": scan,
                "warnings": warnings,
            }
        )
        texts.append(body)
        line = end + 1
    text = "\n".join(texts)
    outline = []

    def walk(items):
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                try:
                    page_no = reader.get_destination_page_number(item)
                    if 0 <= page_no < len(pages):
                        outline.append(
                            {
                                "title": str(item.title),
                                "page": page_no + 1,
                                "start_line": pages[page_no]["start_line"],
                            }
                        )
                except Exception:
                    pass

    try:
        walk(reader.outline)
    except Exception:
        pass
    outline.sort(key=lambda s: s["start_line"])
    for i, item in enumerate(outline):
        item["id"] = i + 1
        item["end_line"] = outline[i + 1]["start_line"] - 1 if i + 1 < len(outline) else line - 1
        item["end_line"] = max(item["start_line"], item["end_line"])
    return text, {
        "format": "pdf",
        "pages": pages,
        "sections": outline or sections(text),
        "warnings": ["layout_unverified", "tables_and_equations_may_be_incomplete"],
        "section_detection": "pdf_outline" if outline else "heading_heuristic",
    }


def sections(text):
    lines = text.splitlines()
    found = [
        {"id": 0, "title": line.strip("# ").strip(), "start_line": i + 1}
        for i, line in enumerate(lines)
        if re.match(r"^#{1,6}\s+\S|^\d+(?:\.\d+)*[. ]\s*[A-Za-z\u3400-\u9fff]", line)
        and len(line) < 180
    ]
    for i, item in enumerate(found):
        item["id"] = i + 1
        item["end_line"] = found[i + 1]["start_line"] - 1 if i + 1 < len(found) else len(lines)
    return found


def ocr_page(data, index):
    if not shutil.which("tesseract"):
        raise ValueError("ocr_unavailable: tesseract required")
    import pypdfium2 as pdfium

    with tempfile.TemporaryDirectory(prefix="astrion-ocr-") as directory:
        pdf = pdfium.PdfDocument(data)
        try:
            page = pdf[index]
            width, height = page.get_size()
            if width * height * 4 > 30_000_000:
                raise ValueError("ocr_page_too_large")
            bitmap = page.render(scale=2)
            try:
                bitmap.to_pil().save(Path(directory) / "page.png")
            finally:
                bitmap.close()
                page.close()
        finally:
            pdf.close()
        result = subprocess.run(
            ["tesseract", str(Path(directory) / "page.png"), "stdout", "-l", "eng+chi_sim"],
            capture_output=True,
            timeout=45,
            check=True,
        )
        if len(result.stdout) > 1_000_000:
            raise ValueError("ocr_output_too_large")
        return result.stdout.decode("utf-8")
