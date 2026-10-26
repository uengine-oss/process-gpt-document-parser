"""HWP·HWPX·DOC·DOCX·PDF 를 같은 블록 구조로 읽는 문서 파서.

    from process_gpt_document_parser import parse
    doc = parse("보고서.hwp", describe=my_vlm)
    print(doc.to_markdown())
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from typing import Optional, Union

from .errors import EncryptedDocumentError, ParseError, UnsupportedFormatError
from .images import Describe, Ocr, describe_images
from .model import Block, Cell, Comment, Document, Image, Table

__version__ = "0.2.0"

FORMATS = ("hwp", "hwpx", "doc", "docx", "pdf")

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_format(data: bytes, filename: Optional[str] = None) -> str:
    """내용으로 포맷을 고른다. 확장자는 내용이 애매할 때만 본다."""
    if data[:1024].lstrip().startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith(b"HWP Document File V3"):  # HWP 3.x 는 OLE 가 아닌 단일 파일
        return "hwp"
    if data[:8] == _OLE_MAGIC:
        import olefile

        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            if ole.exists("FileHeader"):
                return "hwp"
            if ole.exists("WordDocument"):
                return "doc"
        raise UnsupportedFormatError("OLE 파일이지만 HWP·DOC 이 아니다")
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            if "word/document.xml" in names:
                return "docx"
            if any(n.startswith("Contents/section") for n in names):
                return "hwpx"
        raise UnsupportedFormatError("ZIP 파일이지만 HWPX·DOCX 가 아니다")
    ext = Path(filename or "").suffix.lower().lstrip(".")
    raise UnsupportedFormatError(f"알 수 없는 포맷{f' (.{ext})' if ext else ''}")


def parse(
    source: Union[str, os.PathLike, bytes],
    *,
    filename: Optional[str] = None,
    describe: Optional[Describe] = None,
    ocr: Optional[Ocr] = None,
    max_workers: int = 4,
) -> Document:
    """문서를 읽어 Document 를 돌려준다.

    describe: (그림 bytes, mime) → 설명. 주면 그림마다 설명을 단다.
    ocr: 쪽 PNG → 글. 주면 글 레이어가 없는 PDF 쪽을 받아쓴다.
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        filename = filename or os.fspath(source)
        data = Path(source).read_bytes()

    try:
        fmt = detect_format(data, filename)
        if fmt == "pdf":
            from .formats import pdf

            doc = pdf.parse(data, ocr=ocr, max_workers=max_workers, images=describe is not None)
        else:
            from importlib import import_module

            doc = import_module(f".formats.{fmt}", __name__).parse(data)
    except ParseError:
        raise
    except Exception as exc:  # 망가진 파일의 구조체·인덱스 오류는 ParseError 하나로 낸다
        raise ParseError(f"문서 구조가 망가졌다: {type(exc).__name__}: {exc}") from exc
    if filename:
        doc.metadata.setdefault("filename", os.path.basename(filename))
    if describe:
        describe_images(doc, describe, max_workers=max_workers)
    return doc


__all__ = [
    "parse", "detect_format", "FORMATS",
    "Document", "Block", "Table", "Cell", "Image", "Comment",
    "ParseError", "EncryptedDocumentError", "UnsupportedFormatError",
]
