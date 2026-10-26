"""HWP 5.0 바이너리 — 한글과컴퓨터가 공개한 「한글 문서 파일 형식 5.0」을 참고해 구현했다.

OLE 컨테이너 안의 DocInfo(문단 모양·그림 목록)와 BodyText/Section* (배포용은 ViewText/Section*)
레코드 트리를 읽어 문단·표·그림을 문서 순서대로 낸다. 출력은 같은 문서의 HWPX 파서와 맞춘다.

본 제품은 한글과컴퓨터의 한/글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
"""
from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import olefile

from ..errors import EncryptedDocumentError, ParseError, UnsupportedFormatError
from ..images import mime_from_name, sniff_mime
from ..model import Block, Cell, Document, Image, Table

# 레코드 태그 (HWPTAG_BEGIN = 0x10)
DOCUMENT_PROPERTIES = 16
BIN_DATA = 18
PARA_SHAPE = 25
DISTRIBUTE_DOC_DATA = 28
PARA_HEADER = 66
PARA_TEXT = 67
CTRL_HEADER = 71
LIST_HEADER = 72
TABLE = 77
SHAPE_COMPONENT_PICTURE = 85

# 글자 컨트롤은 1글자, 인라인·확장 컨트롤은 8글자(16바이트)를 차지한다
_CHAR_CTRL = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}
_INLINE_CTRL = {4, 5, 6, 7, 8, 9, 19, 20}

_FH_COMPRESSED = 0x01
_FH_PASSWORD = 0x02
_FH_DISTRIBUTION = 0x04


@dataclass
class Record:
    tag: int
    level: int
    data: bytes


def read_records(data: bytes) -> List[Record]:
    out: List[Record] = []
    pos, n = 0, len(data)
    while pos + 4 <= n:
        head = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag, level, size = head & 0x3FF, (head >> 10) & 0x3FF, (head >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                break
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        out.append(Record(tag, level, data[pos:pos + size]))
        pos += size
    return out


def _inflate(data: bytes) -> bytes:
    for wbits in (-15, 15):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    return data


def _ctrl_id(data: bytes) -> str:
    return data[:4][::-1].decode("latin-1") if len(data) >= 4 else ""


# ── 배포용 문서 ───────────────────────────────────────────────────────────────

def _msvc_rand(seed: int):
    while True:
        seed = (seed * 214013 + 2531011) & 0xFFFFFFFF
        yield (seed >> 16) & 0x7FFF


def _distribution_key(payload: bytes) -> bytes:
    """DISTRIBUTE_DOC_DATA 256바이트를 풀어 AES 키(16바이트)를 얻는다."""
    data = bytearray(payload[:256])
    seed = struct.unpack_from("<I", data, 0)[0]
    rand = _msvc_rand(seed)
    key = run = 0
    for i in range(256):
        if run == 0:
            key = next(rand) & 0xFF
            run = (next(rand) & 0x0F) + 1
        if i >= 4:
            data[i] ^= key
        run -= 1
    offset = 4 + (seed & 0x0F)
    return bytes(data[offset:offset + 16])


def _decrypt_view_text(raw: bytes, compressed: bool) -> bytes:
    head = read_records(raw[:4 + 256])
    if not head or head[0].tag != DISTRIBUTE_DOC_DATA or len(head[0].data) < 256:
        raise ParseError("배포용 HWP: 암호 정보 레코드가 없다")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise ParseError("배포용 HWP 를 읽으려면 cryptography 가 필요하다 "
                         "(pip install process-gpt-document-parser[hwp-distribution])") from exc
    body = raw[4 + 256:]
    body = body[:len(body) - len(body) % 16]
    dec = Cipher(algorithms.AES(_distribution_key(head[0].data)), modes.ECB()).decryptor()
    plain = dec.update(body) + dec.finalize()
    return _inflate(plain) if compressed else plain


# ── 본문 레코드 트리 ──────────────────────────────────────────────────────────

@dataclass
class Control:
    id: str
    data: bytes
    children: List[Record]  # 이 컨트롤 아래 레코드(레벨은 원래 값)


@dataclass
class Paragraph:
    shape_id: int
    units: List[int]  # PARA_TEXT 의 UTF-16 코드 단위
    controls: List[Control] = field(default_factory=list)


def _subtree_end(recs: List[Record], i: int) -> int:
    level = recs[i].level
    j = i + 1
    while j < len(recs) and recs[j].level > level:
        j += 1
    return j


def _read_paragraph(recs: List[Record], i: int) -> Tuple[Paragraph, int]:
    head = recs[i]
    shape_id = struct.unpack_from("<H", head.data, 8)[0] if len(head.data) >= 10 else 0
    para = Paragraph(shape_id, [])
    end = _subtree_end(recs, i)
    j = i + 1
    while j < end:
        r = recs[j]
        if r.level == head.level + 1 and r.tag == PARA_TEXT:
            para.units = list(struct.unpack_from(f"<{len(r.data) // 2}H", r.data))
        if r.level == head.level + 1 and r.tag == CTRL_HEADER:
            sub_end = _subtree_end(recs, j)
            para.controls.append(Control(_ctrl_id(r.data), r.data, recs[j + 1:sub_end]))
            j = sub_end
            continue
        j += 1
    return para, end


def _read_paragraphs(recs: List[Record], i: int, level: int) -> Tuple[List[Paragraph], int]:
    """i 부터 같은 레벨에 이어지는 문단들."""
    out = []
    while i < len(recs) and recs[i].level == level and recs[i].tag == PARA_HEADER:
        para, i = _read_paragraph(recs, i)
        out.append(para)
    return out, i


class _Reader:
    def __init__(self, ole: olefile.OleFileIO, compressed: bool):
        self.ole = ole
        self.compressed = compressed
        self.outline: Dict[int, int] = {}
        self.bin_items: Dict[int, Tuple[int, str, int]] = {}  # 순번 → (저장 id, 확장자, 압축 설정)
        self.blocks: List[Block] = []
        self.warnings: List[str] = []
        self._read_doc_info()

    def _stream(self, name: str) -> bytes:
        return self.ole.openstream(name).read()

    def _read_doc_info(self) -> None:
        if not self.ole.exists("DocInfo"):
            return
        raw = self._stream("DocInfo")
        recs = read_records(_inflate(raw) if self.compressed else raw)
        shape_idx = bin_idx = 0
        for r in recs:
            if r.tag == PARA_SHAPE:
                if len(r.data) >= 4:
                    prop = struct.unpack_from("<I", r.data, 0)[0]
                    if (prop >> 23) & 0x3 == 1:  # 개요 문단
                        self.outline[shape_idx] = ((prop >> 25) & 0x7) + 1
                shape_idx += 1
            elif r.tag == BIN_DATA:
                bin_idx += 1
                self._bin_item(bin_idx, r.data)

    def _bin_item(self, idx: int, data: bytes) -> None:
        if len(data) < 2:
            return
        prop = struct.unpack_from("<H", data, 0)[0]
        kind, compress = prop & 0x0F, (prop >> 4) & 0x03
        if kind not in (1, 2):  # 0 = 외부 파일 연결
            return
        storage_id = struct.unpack_from("<H", data, 2)[0]
        ext = ""
        if kind == 1 and len(data) >= 6:
            n = struct.unpack_from("<H", data, 4)[0]
            ext = data[6:6 + 2 * n].decode("utf-16-le", "replace")
        self.bin_items[idx] = (storage_id, ext, compress)

    def _image(self, bin_ref: int) -> Optional[Image]:
        item = self.bin_items.get(bin_ref)
        if not item:
            return None
        storage_id, ext, compress = item
        name = f"BinData/BIN{storage_id:04X}.{ext}"
        if not self.ole.exists(name):
            return None
        data = self._stream(name)
        if compress == 1 or (compress == 0 and self.compressed):
            data = _inflate(data)
        return Image(id=name, data=data, mime=sniff_mime(data, mime_from_name(ext)))

    # ── 문단 → 블록 ──

    def emit_paragraphs(self, paras: List[Paragraph]) -> None:
        for p in paras:
            self.emit_paragraph(p)

    def emit_paragraph(self, p: Paragraph) -> None:
        level = self.outline.get(p.shape_id)
        buf: List[str] = []
        images: List[Image] = []

        def flush():
            text = "".join(buf)
            if text.strip():
                self.blocks.append(Block("paragraph", text=text, heading_level=level))
            buf.clear()

        for piece in self._pieces(p):
            if isinstance(piece, str):
                buf.append(piece)
                continue
            ctrl = piece
            if ctrl.id == "tbl ":
                flush()
                self._table(ctrl)
            elif ctrl.id in ("head", "foot"):
                continue
            else:  # 글상자·각주 등 하위 목록의 글은 제자리에, 그림은 문단 뒤에
                buf.append(self._inline_text(ctrl.children))
                images.extend(self._pictures(ctrl.children))
        flush()
        for img in images:
            self.blocks.append(Block("image", image=img))

    @staticmethod
    def _pieces(p: Paragraph):
        """문단 글을 글 조각과 확장 컨트롤로 나눈다. 확장 컨트롤은 CTRL_HEADER 와 같은 순서다."""
        controls = iter(p.controls)
        units: List[int] = []
        u, i, n = p.units, 0, len(p.units)

        def text():
            s = struct.pack(f"<{len(units)}H", *units).decode("utf-16-le", "replace")
            units.clear()
            return s

        while i < n:
            c = u[i]
            if c >= 32:
                units.append(c)
                i += 1
            elif c in _CHAR_CTRL:
                if c == 24:
                    units.append(0x2D)
                elif c in (10, 30, 31):  # 줄 바꿈·묶음 빈칸·고정폭 빈칸
                    units.append(0x20)
                i += 1
            elif c in _INLINE_CTRL:
                if c == 9:
                    units.append(0x20)
                i += 8
            else:
                ctrl = next(controls, None)
                if ctrl is not None:
                    yield text()
                    yield ctrl
                i += 8
        yield text()

    def _inline_text(self, recs: List[Record]) -> str:
        """컨트롤 아래 하위 목록(글상자·각주 등)의 글. 안의 표는 칸 글을 잇는다."""
        parts: List[str] = []
        i = 0
        while i < len(recs):
            if recs[i].tag == PARA_HEADER:
                para, i = _read_paragraph(recs, i)
                parts.append(" ")  # 문단 사이를 띄운다(HWPX 파서와 같게)
                for piece in self._pieces(para):
                    if isinstance(piece, str):
                        parts.append(piece)
                    elif piece.id not in ("head", "foot"):
                        parts.append(self._inline_text(piece.children))
            else:
                i += 1
        return "".join(parts)

    def _pictures(self, recs: List[Record]) -> List[Image]:
        out = []
        for r in recs:
            if r.tag == SHAPE_COMPONENT_PICTURE and len(r.data) >= 73:
                img = self._image(struct.unpack_from("<H", r.data, 71)[0])
                if img:
                    out.append(img)
        return out

    def _table(self, ctrl: Control) -> None:
        recs = ctrl.children
        if not recs:
            return
        base = recs[0].level
        start = next((k for k, r in enumerate(recs) if r.tag == TABLE and r.level == base), None)
        if start is None:
            return
        caption = recs[:start]
        cells: List[Cell] = []
        images: List[Image] = []
        i = start + 1
        while i < len(recs):
            r = recs[i]
            if r.level == base and r.tag == LIST_HEADER and len(r.data) >= 16:
                col, row, col_span, row_span = struct.unpack_from("<4H", r.data, 8)
                paras, i = _read_paragraphs(recs, i + 1, base)
                texts = []
                for p in paras:
                    texts.append("".join(s if isinstance(s, str) else self._inline_text(s.children)
                                         for s in self._pieces(p)
                                         if isinstance(s, str) or s.id not in ("head", "foot")))
                    for ctrl_ in p.controls:
                        images.extend(self._pictures(ctrl_.children))
                text = " ".join(" ".join(texts).split())
                cells.append(Cell(text, row, col, max(row_span, 1), max(col_span, 1)))
            else:
                i += 1
        if cells:
            self.blocks.append(Block("table", table=Table(cells)))
        cap = self._inline_text(caption).strip()
        if cap:
            self.blocks.append(Block("paragraph", text=cap))
        for img in images:
            self.blocks.append(Block("image", image=img))


def parse(data: bytes) -> Document:
    if data[:32].startswith(b"HWP Document File V3"):
        from . import hwp3

        return hwp3.parse(data)
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if not ole.exists("FileHeader"):
            raise ParseError("HWP: FileHeader 가 없다")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise ParseError("HWP: 서명이 맞지 않다")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & _FH_PASSWORD:
            raise EncryptedDocumentError("암호가 걸린 HWP")
        compressed = bool(flags & _FH_COMPRESSED)
        distribution = bool(flags & _FH_DISTRIBUTION)

        reader = _Reader(ole, compressed)
        folder = "ViewText" if distribution else "BodyText"
        k = 0
        while ole.exists(f"{folder}/Section{k}"):
            raw = ole.openstream(f"{folder}/Section{k}").read()
            body = _decrypt_view_text(raw, compressed) if distribution else (_inflate(raw) if compressed else raw)
            recs = read_records(body)
            if recs:
                paras, _ = _read_paragraphs(recs, 0, recs[0].level)
                reader.emit_paragraphs(paras)
            k += 1
        if k == 0:
            raise ParseError(f"HWP: {folder}/Section0 이 없다")
        doc = Document(format="hwp", blocks=reader.blocks, warnings=reader.warnings)
        if distribution:
            doc.metadata["distribution"] = True
        return doc
