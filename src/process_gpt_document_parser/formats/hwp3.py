"""HWP 3.x (한글 97 이하) — 한글과컴퓨터가 공개한 「한글 문서 파일 형식 3.0」을 참고해 구현했다.

본 제품은 한글과컴퓨터의 한/글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.

파일 인식 정보·문서 정보·문서 요약·정보 블록 뒤에 (압축이면 raw deflate 로) 글꼴 이름·스타일·문단 리스트·
추가 정보 블록이 이어진다. 문단 리스트를 처음부터 한 번 읽으며 문단·표·그림을 문서 순서대로 낸다.
명세에 없는 사실(실측)은 docs/spec.md "HWP 3.x" 에 적었다.
"""
from __future__ import annotations

import struct
import zlib
from collections import Counter
from typing import Dict, List, Optional, Tuple

from ..errors import EncryptedDocumentError, ParseError
from ..images import mime_from_name, sniff_mime
from ..model import Block, Cell, Document, Image, Table
from .hwp3_chars import decode_hchar

SIGNATURE = b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05"

_INFO, _SUMMARY = 128, 1008
_PARA_SHAPE, _CHAR_SHAPE = 187, 31
_STYLE = 20 + _CHAR_SHAPE + _PARA_SHAPE

# 고정 길이 특수 문자의 전체 바이트 수(코드 hchar 포함). 명세 10장
_FIXED = {7: 84, 8: 96, 9: 8, 18: 8, 19: 8, 20: 8, 21: 8, 22: 24, 23: 10, 24: 6, 25: 6, 26: 246, 28: 64, 30: 4, 31: 4}
_MAX_DEPTH = 32


class _Buf:
    def __init__(self, data: bytes):
        self.b, self.p = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.b):
            raise ParseError("HWP 3.x: 자료가 중간에 끝났다")
        out = self.b[self.p:self.p + n]
        self.p += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]


class _Para:
    __slots__ = ("pieces", "outline")

    def __init__(self):
        self.pieces: List[object] = []  # str | ("table", Table, caption) | ("image", name) | ...
        self.outline: Optional[int] = None


class _Reader:
    def __init__(self, data: bytes):
        self.buf = _Buf(data)
        self.unknown: Counter = Counter()
        self.images: Dict[str, bytes] = {}

    # ── 문단 리스트 ──

    def para_list(self, depth: int = 0, buf: Optional[_Buf] = None) -> List[_Para]:
        if depth > _MAX_DEPTH:
            raise ParseError("HWP 3.x: 문단 리스트가 너무 깊게 중첩됐다")
        buf = buf or self.buf
        out: List[_Para] = []
        while True:
            prev_shape, n_chars, n_lines, has_shapes = buf.u8(), buf.u16(), buf.u16(), buf.u8()
            buf.take(1 + 4 + 1 + _CHAR_SHAPE)  # 기타 플래그·특수 문자 플래그·스타일·대표 글자 모양
            if n_chars == 0:  # 빈 문단 = 리스트 끝. 문단 모양 이후는 저장되지 않는다(실측)
                return out
            if prev_shape == 0:
                buf.take(_PARA_SHAPE)
            buf.take(14 * n_lines)
            if has_shapes:
                for _ in range(n_chars):
                    if buf.u8() != 1:
                        buf.take(_CHAR_SHAPE)
            out.append(self.chars(buf, n_chars, depth))

    def chars(self, buf: _Buf, n_chars: int, depth: int) -> _Para:
        """글자 수는 hchar 단위다. 특수 문자는 식별 정보까지만 세고 딸린 자료·문단 리스트는 세지 않는다(실측)."""
        para = _Para()
        text: List[str] = []

        def push(piece):
            if text:
                para.pieces.append("".join(text))
                text.clear()
            para.pieces.append(piece)

        i = 0
        while i < n_chars:
            c = buf.u16()
            if c >= 32:
                s = decode_hchar(c)
                if s is None:
                    self.unknown[c] += 1
                    s = "�"
                text.append(s)
            elif c == 13:
                pass
            elif c in _FIXED:
                body = buf.take(_FIXED[c] - 2)
                i += len(body) // 2
                if c in (9, 30, 31):
                    text.append(" ")
                elif c == 24:
                    text.append("-")
                elif c == 23:  # 글자겹침: 겹친 글자들
                    for k in range(3):
                        h = struct.unpack_from("<H", body, 2 * k)[0]
                        if h >= 32:
                            text.append(decode_hchar(h) or "�")
                elif c == 28 and body[0] == 1:  # 개요 번호 문단
                    para.outline = body[3] + 1
            elif c in (10, 11, 15, 16, 17):
                buf.take(6)
                i += 3
                push(self.boxed(buf, c, depth))
            else:  # 필드·책갈피·상호참조와 예약 코드: 코드, 길이, 코드, 자료
                n = buf.u32()
                buf.take(2 + n)
                i += 3
            i += 1
        if text:
            para.pieces.append("".join(text))
        return para

    def boxed(self, buf: _Buf, code: int, depth: int):
        if code == 10:
            info = buf.take(84)
            kind, n_cells = struct.unpack_from("<HH", info, 78)
            geom = []
            for _ in range(n_cells):
                cell = buf.take(27)
                geom.append(struct.unpack_from("<4H", cell, 4))  # x, y, 너비, 높이
            bodies = [self.para_list(depth + 1, buf) for _ in range(n_cells)]
            caption = self.para_list(depth + 1, buf)
            if kind == 0:
                return ("table", _table(geom, [self.inline(b) for b in bodies]), self.inline(caption))
            if kind == 2:  # 수식 스크립트는 HWP5·HWPX 파서처럼 내지 않는다. 캡션만
                bodies = []
            return ("text", " ".join(self.inline(b) for b in bodies + [caption]).strip())
        if code == 11:
            extra = buf.u32()
            info = buf.take(344 + extra)
            kind = info[70]
            name = info[79:79 + 256].split(b"\0", 1)[0].decode("latin-1")
            texts = []
            if kind == 3:
                texts = self.drawing(info[344:], depth)
            caption = self.para_list(depth + 1, buf)
            texts.append(self.inline(caption))
            return ("image", name if kind == 2 else None, " ".join(t for t in texts if t).strip())
        if code == 15:  # 숨은 설명
            buf.take(8)
            return ("text", self.inline(self.para_list(depth + 1, buf)))
        if code == 16:  # 머리말/꼬리말: 본문 흐름에 넣지 않는다
            buf.take(10)
            self.para_list(depth + 1, buf)
            return ("skip",)
        buf.take(14)  # 17 각주/미주: 제자리에
        return ("text", self.inline(self.para_list(depth + 1, buf)))

    def drawing(self, data: bytes, depth: int) -> List[str]:
        """그리기 개체(그림 종류 3)의 글상자 글."""
        d = _Buf(data)
        head_len = d.u32()
        head = d.take(head_len)
        n_objects = struct.unpack_from("<I", head, 4)[0] if len(head) >= 8 else 0
        out = []
        for _ in range(n_objects):
            common_len = d.u32()
            common = d.take(common_len)
            kind = struct.unpack_from("<H", common, 0)[0]
            if kind == 6:  # 글상자: 공통 헤더 뒤 글상자 정보, 세부 정보 2 가 문단 리스트
                pass
            n1 = d.u32()
            d.take(n1)
            n2 = d.u32()
            detail = d.take(n2)
            if kind == 6 and detail:
                out.append(self.inline(self.para_list(depth + 1, _Buf(detail))))
        return out

    def inline(self, paras: List[_Para]) -> str:
        """하위 리스트(셀·글상자·각주)의 글. 문단 사이와 안의 표는 띄어서 잇는다."""
        parts = []
        for p in paras:
            for piece in p.pieces:
                if isinstance(piece, str):
                    parts.append(piece)
                elif piece[0] == "text":
                    parts.append(piece[1])
                elif piece[0] == "table":
                    parts.append(" ".join(c.text for c in piece[1].cells))
                    parts.append(piece[2])
                elif piece[0] == "image":
                    parts.append(piece[2])
            parts.append(" ")
        return " ".join("".join(parts).split())

    # ── 추가 정보 블록 ──

    def extra_blocks(self) -> None:
        buf = self.buf
        while buf.p + 8 <= len(buf.b):
            block_id, size = buf.u32(), buf.u32()
            if block_id == 0 and size == 0:
                return
            body = buf.take(size)
            if block_id == 1 and len(body) >= 32:
                name = body[:16].split(b"\0", 1)[0].decode("latin-1")
                self.images[name] = body[32:]


_SNAP = 10  # hunit(0.14mm). 셀 경계 좌표에 1~3 hunit 반올림 오차가 있다(실측)


def _grid_lines(values) -> Dict[int, int]:
    """경계 좌표 → 격자선 번호. _SNAP 안으로 붙은 좌표는 같은 선이다."""
    out: Dict[int, int] = {}
    line, last = -1, None
    for v in sorted(set(values)):
        if last is None or v - last > _SNAP:
            line += 1
        out[v] = line
        last = v
    return out


def _table(geom: List[Tuple[int, int, int, int]], texts: List[str]) -> Table:
    """셀 위치·크기(hunit)로 줄·칸 번호와 병합을 정한다. 경계 좌표의 순번이 곧 격자선이다."""
    xi = _grid_lines([g[0] for g in geom] + [g[0] + g[2] for g in geom])
    yi = _grid_lines([g[1] for g in geom] + [g[1] + g[3] for g in geom])
    cells = []
    for (x, y, w, h), text in zip(geom, texts):
        col, row = xi[x], yi[y]
        cells.append(Cell(text, row, col, max(1, yi[y + h] - row), max(1, xi[x + w] - col)))
    return Table(cells)


def parse(data: bytes) -> Document:
    if not data.startswith(SIGNATURE[:23]):
        raise ParseError("HWP 3.x: 서명이 맞지 않다")
    if len(data) < 30 + _INFO + _SUMMARY:
        raise ParseError("HWP 3.x: 파일이 너무 짧다")
    info = data[30:30 + _INFO]
    if struct.unpack_from("<H", info, 96)[0]:
        raise EncryptedDocumentError("암호가 걸린 HWP 3.x")
    compressed = info[124] != 0
    info_block_len = struct.unpack_from("<H", info, 126)[0]
    body = data[30 + _INFO + _SUMMARY + info_block_len:]
    if compressed:
        try:
            body = zlib.decompressobj(-15).decompress(body)
        except zlib.error as exc:
            raise ParseError(f"HWP 3.x: 압축을 풀지 못했다 ({exc})") from exc

    r = _Reader(body)
    for _ in range(7):  # 언어별 글꼴 이름
        r.buf.take(40 * r.buf.u16())
    r.buf.take(_STYLE * r.buf.u16())
    paras = r.para_list()
    try:
        r.extra_blocks()
    except ParseError:
        pass  # 본문은 다 읽었다. 그림만 못 붙는다

    blocks: List[Block] = []
    for p in paras:
        buf: List[str] = []
        images: List[Block] = []

        def flush():
            text = "".join(buf)
            if text.strip():
                blocks.append(Block("paragraph", text=text, heading_level=p.outline))
            buf.clear()

        for piece in p.pieces:
            if isinstance(piece, str):
                buf.append(piece)
            elif piece[0] == "table":
                flush()
                blocks.append(Block("table", table=piece[1]))
                if piece[2]:
                    blocks.append(Block("paragraph", text=piece[2]))
            elif piece[0] == "text":
                buf.append(piece[1])
            elif piece[0] == "image":
                buf.append(piece[2])
                img = r.images.get(piece[1]) if piece[1] else None
                if img:
                    images.append(Block("image", image=Image(
                        id=piece[1], data=img, mime=sniff_mime(img, mime_from_name(piece[1])))))
        flush()
        blocks.extend(images)

    doc = Document(format="hwp", blocks=blocks)
    doc.metadata["hwp_version"] = "3.x"
    if r.unknown:
        codes = ", ".join(f"0x{c:04X}×{n}" for c, n in r.unknown.most_common(8))
        doc.warnings.append(f"HWP 3.x: 읽지 못한 글자 {sum(r.unknown.values())}개 (U+FFFD 로 표시) — {codes}")
    return doc
