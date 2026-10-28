from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import os
import re
import unicodedata

try:
    import pymupdf as fitz
except ImportError:
    import fitz

MAX_FILE_BYTES = os.getenv('MAX_FILE_BYTES', 20*1024*1024) # per file
MAX_PAGES = os.getenv('MAX_PAGES', 100)
MIN_CHARS = 200

# OCR settings
PAGE_MIN_CHARS = 25         # a page with fewer characters than this has no usable text layer
MAX_OCR_PAGES = 30          # bounds time and cost per file
OCR_DPI = 200
OCR_MAX_SIDE_PX = 3000      # huge pages are rendered smaller, so one file cannot exhaust memory
OCR_MIN_CONFIDENCE = 0.70   # character-weighted mean confidence below this is flagged

class OcrUnavailable(Exception):
    """OCR was needed but the engine is not installed."""

@dataclass
class IngestResult:
    filename: str
    sha256: str
    size_bytes: int
    status: str = 'flagged' #'ok' or 'flagged'
    reason: str | None = None # set reason when status is flagged
    page_count: int = 0
    pages: list[str] = field(default_factory=list) #text per pages, index 0 = page 1
    ocr_pages: list[int] = field(default_factory=list)
    ocr_confidence: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.pages)

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "reason": self.reason,
            "page_count": self.page_count,
            "char_count": len(self.text),
            "ocr_pages": self.ocr_pages,
            "ocr_confidence": self.ocr_confidence,
            "warnings": self.warnings
        }

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()

_engine = None

def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise OcrUnavailable(
                "Some pages are scanned but OCR engine is not install",
                "(pip install rapidocr)"
            ) from e
        _engine = RapidOCR()
    return _engine

def _ocr_png(png: bytes) -> list[tuple[str, float]]:
    result, _ = _get_engine()(png)
    return [(text, float(score)) for _box, text, score in (result or [])]

def _render_dpi(page) -> int:
    longest_pt = max(page.rect.width, page.rect.height) or 1
    return max(1, min(OCR_DPI, int(OCR_MAX_SIDE_PX * 72 / longest_pt)))

def ingest_pdf(filename: str, data: bytes) -> IngestResult:
    res = IngestResult(
        filename=filename,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data)
    )

    if not data:
        res.reason = "Empty File"
        return res

    if len(data) > MAX_FILE_BYTES:
        res.reason = f"File is larger than {MAX_FILE_BYTES} MB."
        return res

    if b"%PDF-" not in data[:1024]:
        res.reason = "File is not a PDF."
        return res

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        res.reason = "PDF is corrupt or coudnt be opened."
        return res

    try:
        if doc.needs_pass:
            res.reason = "PDF is password protected"
            return res

        res.page_count = doc.page_count

        if res.page_count == 0:
            res.reason = "PDF is empty file."
            return res

        if res.page_count > MAX_PAGES:
            res.reason = f"PDF has mor than {MAX_PAGES} pages."
            return res

        try:
            res.pages = [page.get_text("text") for page in doc ]
        except Exception:
            res.pages = []
            res.reason = "Text cant e extracted from pdf"
            return res

        need_ocr = [i for i, t in enumerate(res.pages) if len(normalize(t)) < PAGE_MIN_CHARS]
        if len(need_ocr) > MAX_OCR_PAGES:
            res.pages = []
            res.reason = (
                f"{len(need_ocr)} pages have no text layer"
                f"OCR is limited to {MAX_OCR_PAGES} pages per file"
            )
            return res

        if need_ocr:
            ocr_lines: list[tuple[str, float]] = []
            try:
                for i in need_ocr:
                    page = doc[i]
                    png = page.get_pixmap(dpi=_render_dpi(page)).tobytes("png")
                    lines = _ocr_png(png)
                    res.pages[i] = "\n".join(text for text, _ in lines)
                    ocr_lines += lines
            except OcrUnavailable as e:
                res.pages = []
                res.reason = str(e)
                return res
            except Exception:
                res.pages = []
                res.reason = "OCR failed on this pdf"
                return res

            res.ocr_pages = [i + 1 for i in need_ocr]
            chars = sum(len(text) for text, _ in ocr_lines)
            if chars: 
                res.ocr_confidence = round(
                    sum(len(text) * conf for text, conf in ocr_lines) / chars, 3
                )
    finally:
        doc.close()

    if len(normalize(res.text)) <  MIN_CHARS:
        res.reason = (
            "Little or no readable text even after OCR (blank or unreadable scan)."
            if res.ocr_pages
            else "Very little text in this PDF.")
        return res

    if res.ocr_confidence is not None and res.ocr_confidence < OCR_MIN_CONFIDENCE:
        res.reason = (
             f"OCR confidence is too low ({res.ocr_confidence:.2f}) to trust the extracted text."
        )
        return res
 
    if res.ocr_confidence is not None:
        pages = ", ".join(str(p) for p in res.ocr_pages)
        res.warnings.append(
            f"OCR was used on page(s) {pages} (confidence {res.ocr_confidence:.2f}). "
            "Check prices, dates and counts against the original PDF."
        )
        
    res.status = 'ok'
    return res