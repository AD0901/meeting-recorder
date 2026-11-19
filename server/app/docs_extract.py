"""会议资料文本提取（增强纪要功能）。
支持 docx / xlsx / pptx / pdf / html / md / txt，全部在服务端本地解析，
不调用任何云服务；上传的原文件在解析后立即删除，不在云端保留。
"""
from __future__ import annotations

from pathlib import Path

SUPPORTED = {".docx", ".pptx", ".xlsx", ".pdf", ".html", ".htm", ".md", ".txt"}
MAX_FILE_BYTES = 20 * 1024 * 1024   # 单文件 20MB
MAX_CHARS_PER_FILE = 40000          # 每文件最多取 4 万字符（控制 LLM 上下文）


class ExtractError(Exception):
    pass


def _docx_text(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"## 工作表：{ws.title}")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 2000:   # 大表截断
                parts.append("…（行数过多，已截断）")
                break
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                parts.append(" | ".join(cells))
    wb.close()
    return "\n".join(parts)


def _pptx_text(path: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [sh.text for sh in slide.shapes if sh.has_text_frame and sh.text.strip()]
        if texts:
            parts.append(f"## 第 {i} 页\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts = []
    for i, page in enumerate(reader.pages):
        if i >= 200:
            parts.append("…（页数过多，已截断）")
            break
        parts.append(page.extract_text() or "")
    text = "\n".join(parts)
    if not text.strip():
        raise ExtractError("这个 PDF 提取不到文字（可能是扫描件/图片型），目前仅支持文字型 PDF")
    return text


def _html_text(path: Path) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(path.read_text(errors="replace"), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def extract_text(path: Path) -> str:
    """按后缀提取纯文本；失败抛 ExtractError（文件名会带进错误提示）。"""
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ExtractError(f"不支持的格式 {ext}（支持 Word/PPT/Excel/PDF/HTML/MD/TXT）")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ExtractError("文件超过 20MB 上限")
    try:
        if ext in (".md", ".txt"):
            text = path.read_text(errors="replace")
        elif ext in (".html", ".htm"):
            text = _html_text(path)
        elif ext == ".docx":
            text = _docx_text(path)
        elif ext == ".xlsx":
            text = _xlsx_text(path)
        elif ext == ".pptx":
            text = _pptx_text(path)
        else:
            text = _pdf_text(path)
    except ExtractError:
        raise
    except Exception as e:  # 文件损坏、加密等
        raise ExtractError(f"解析失败：{type(e).__name__}: {e}") from e
    text = text.strip()
    if not text:
        raise ExtractError("文件里没有提取到文字内容")
    if len(text) > MAX_CHARS_PER_FILE:
        text = text[:MAX_CHARS_PER_FILE] + "\n…（内容过长，已截断）"
    return text
