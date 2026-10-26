"""Word 97-2003 바이너리(.doc) — Microsoft 가 공개한 [MS-DOC] 명세를 참고해 구현했다.

FIB → 피스 테이블(Clx)로 본문 글을 모으고, 문단 속성(PAPX FKP)으로 표 칸·행 끝과 헤딩을,
문자 속성(CHPX FKP)으로 인라인 그림 위치를 찾는다. 그림 데이터는 Data 스트림의 PICF/OfficeArt 레코드다.
"""
from __future__ import annotations

import bisect
import io
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

import olefile

from ..errors import EncryptedDocumentError, ParseError, UnsupportedFormatError
from ..model import Block, Cell, Document, Image, Table

# 문단 속성 sprm
sprmPFInTable = 0x2416
sprmPFTtp = 0x2417
sprmPOutLvl = 0x2640
sprmPItap = 0x6649
sprmPFInnerTtp = 0x244C
sprmPFInnerTableCell = 0x244B
sprmTDefTable = 0xD608
sprmTVertMerge = 0xD62B
sprmPChgTabs = 0xC615
# 문자 속성 sprm
sprmCFSpec = 0x0855
sprmCPicLocation = 0x6A03

_OPERAND_SIZE = {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}


def iter_sprms(grpprl: bytes) -> Iterator[Tuple[int, bytes]]:
    i, n = 0, len(grpprl)
    while i + 2 <= n:
        sprm = struct.unpack_from("<H", grpprl, i)[0]
        i += 2
        spra = sprm >> 13
        if spra == 6:
            if sprm == sprmTDefTable:
                if i + 2 > n:
                    break
                size = struct.unpack_from("<H", grpprl, i)[0] + 1
            elif sprm == sprmPChgTabs and i < n and grpprl[i] == 255:
                j = i + 1
                dels = grpprl[j] if j < n else 0
                j += 1 + 4 * dels
                adds = grpprl[j] if j < n else 0
                size = j + 1 + 3 * adds - i
            else:
                if i >= n:
                    break
                size = grpprl[i] + 1
        else:
            size = _OPERAND_SIZE[spra]
        yield sprm, grpprl[i:i + size]
        i += size


@dataclass
class ParaProps:
    istd: int = 0
    in_table: bool = False
    ttp: bool = False
    itap: int = 0
    inner_ttp: bool = False
    inner_cell: bool = False
    outline: Optional[int] = None
    tdef: Optional[bytes] = None
    vmerge: Dict[int, int] = field(default_factory=dict)


def _para_props(papx: bytes) -> ParaProps:
    pp = ParaProps()
    if len(papx) < 2:
        return pp
    pp.istd = struct.unpack_from("<H", papx, 0)[0]
    for sprm, op in iter_sprms(papx[2:]):
        if sprm == sprmPFInTable:
            pp.in_table = bool(op[0])
        elif sprm == sprmPFTtp:
            pp.ttp = bool(op[0])
        elif sprm == sprmPItap:
            pp.itap = struct.unpack_from("<i", op)[0]
        elif sprm == sprmPFInnerTtp:
            pp.inner_ttp = bool(op[0])
        elif sprm == sprmPFInnerTableCell:
            pp.inner_cell = bool(op[0])
        elif sprm == sprmPOutLvl:
            pp.outline = op[0]
        elif sprm == sprmTDefTable:
            pp.tdef = op
        elif sprm == sprmTVertMerge and len(op) >= 3:
            pp.vmerge[op[1]] = op[2]
    return pp


class _Fkp:
    """512바이트 FKP 하나: [(fc 시작, fc 끝, grpprl)]."""

    def __init__(self, page: bytes, papx: bool):
        crun = page[511]
        self.fcs = list(struct.unpack_from(f"<{crun + 1}I", page, 0))
        self.props: List[bytes] = []
        base = 4 * (crun + 1)
        for k in range(crun):
            off = page[base + k * (13 if papx else 1)] * 2
            if off == 0:
                self.props.append(b"")
            elif papx:
                cb = page[off]
                if cb == 0:
                    size, start = 2 * page[off + 1], off + 2
                else:
                    size, start = 2 * cb - 1, off + 1
                self.props.append(page[start:start + size])
            else:
                cb = page[off]
                self.props.append(page[off + 1:off + 1 + cb])

    def find(self, fc: int) -> Optional[bytes]:
        k = bisect.bisect_right(self.fcs, fc) - 1
        if 0 <= k < len(self.props):
            return self.props[k]
        return None


@dataclass
class Piece:
    cp: int
    cp_end: int
    fc: int  # 실제 바이트 위치
    compressed: bool


class _Reader:
    def __init__(self, ole: olefile.OleFileIO):
        self.ole = ole
        self.word = ole.openstream("WordDocument").read()
        w = self.word
        if len(w) < 0x200:
            raise ParseError("DOC: WordDocument 스트림이 너무 짧다")
        n_fib = struct.unpack_from("<H", w, 2)[0]
        if n_fib < 0x00C1:
            raise UnsupportedFormatError("Word 6/95 이전 DOC 은 지원하지 않는다")
        if struct.unpack_from("<H", w, 0)[0] != 0xA5EC:
            raise ParseError("DOC: FIB 서명이 맞지 않다")
        flags = struct.unpack_from("<H", w, 0x0A)[0]
        if flags & 0x0100:
            raise EncryptedDocumentError("암호가 걸린 DOC")
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            raise ParseError(f"DOC: {table_name} 스트림이 없다")
        self.table = ole.openstream(table_name).read()
        self.data = ole.openstream("Data").read() if ole.exists("Data") else b""
        cslw = struct.unpack_from("<H", w, 62)[0]
        # ccpText, ccpFtn, ccpHdd, (예약), ccpAtn, ccpEdn
        lw = struct.unpack_from("<6i", w, 64 + 12)
        self.ccp_text = lw[0]
        self.txbx_base = lw[0] + lw[1] + lw[2] + lw[4] + lw[5]
        self._fclcb = 64 + 4 * cslw + 2
        self.pieces = self._pieces()
        self._piece_cps = [p.cp for p in self.pieces]
        self.papx_fkps = self._bte(13, papx=True)
        self.chpx_fkps = self._bte(12, papx=False)
        self.style_outline = self._styles()
        self.warnings: List[str] = []
        self.anchors = self._shape_anchors()
        self.shape_pib, self.bstore = self._drawings()
        self.textboxes = self._textboxes()

    def _fc_lcb(self, index: int) -> Tuple[int, int]:
        return struct.unpack_from("<II", self.word, self._fclcb + 8 * index)

    def _pieces(self) -> List[Piece]:
        fc, lcb = self._fc_lcb(33)  # fcClx
        clx = self.table[fc:fc + lcb]
        i = 0
        while i < len(clx) and clx[i] == 0x01:  # Prc 는 건너뛴다
            i += 3 + struct.unpack_from("<h", clx, i + 1)[0]
        if i >= len(clx) or clx[i] != 0x02:
            raise ParseError("DOC: 피스 테이블이 없다")
        lcb_pcd = struct.unpack_from("<I", clx, i + 1)[0]
        plc = clx[i + 5:i + 5 + lcb_pcd]
        n = (len(plc) - 4) // 12
        cps = struct.unpack_from(f"<{n + 1}i", plc, 0)
        out = []
        for k in range(n):
            fc_raw = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
            compressed = bool(fc_raw & 0x40000000)
            real = (fc_raw & ~0x40000000) // 2 if compressed else fc_raw
            out.append(Piece(cps[k], cps[k + 1], real, compressed))
        return out

    def _bte(self, index: int, papx: bool) -> List[Tuple[int, int, int]]:
        """PlcBte → [(fc 시작, fc 끝, FKP 쪽 번호)]."""
        fc, lcb = self._fc_lcb(index)
        plc = self.table[fc:fc + lcb]
        n = (len(plc) - 4) // 8
        if n <= 0:
            return []
        fcs = struct.unpack_from(f"<{n + 1}I", plc, 0)
        pns = struct.unpack_from(f"<{n}I", plc, 4 * (n + 1))
        return [(fcs[k], fcs[k + 1], pns[k] & 0x3FFFFF) for k in range(n)]

    def _fkp_prop(self, btes, cache: Dict[int, _Fkp], fc: int, papx: bool) -> Optional[bytes]:
        k = bisect.bisect_right([b[0] for b in btes], fc) - 1
        if k < 0 or fc >= btes[k][1]:
            return None
        pn = btes[k][2]
        if pn not in cache:
            cache[pn] = _Fkp(self.word[pn * 512:(pn + 1) * 512], papx)
        return cache[pn].find(fc)

    def _styles(self) -> Dict[int, int]:
        """STSH → {istd: 헤딩 수준}. 기본 제목 스타일(sti 1~9)과 스타일의 개요 수준, istdBase 상속."""
        fc, lcb = self._fc_lcb(1)
        st = self.table[fc:fc + lcb]
        if len(st) < 6:
            return {}
        cb_info = struct.unpack_from("<H", st, 0)[0]
        cstd, cb_base = struct.unpack_from("<HH", st, 2)
        pos = 2 + cb_info
        own: Dict[int, Optional[int]] = {}
        base_of: Dict[int, int] = {}
        for istd in range(cstd):
            if pos + 2 > len(st):
                break
            cb = struct.unpack_from("<H", st, pos)[0]
            std = st[pos + 2:pos + 2 + cb]
            pos += 2 + cb
            if cb < 10:
                continue
            sti = struct.unpack_from("<H", std, 0)[0] & 0x0FFF
            w2 = struct.unpack_from("<H", std, 2)[0]
            stk, base = w2 & 0x0F, w2 >> 4
            level = sti if 1 <= sti <= 9 else None
            if stk == 1 and level is None:
                level = self._style_outline(std, cb_base)
            own[istd] = level
            if base != 0x0FFF:
                base_of[istd] = base
        out: Dict[int, int] = {}
        for istd in own:
            cur, hops = istd, 0
            while cur in own and own[cur] is None and cur in base_of and hops < 10:
                cur, hops = base_of[cur], hops + 1
            if own.get(cur):
                out[istd] = own[cur]
        return out

    @staticmethod
    def _style_outline(std: bytes, cb_base: int) -> Optional[int]:
        try:
            p = cb_base
            cch = struct.unpack_from("<H", std, p)[0]
            p += 2 + 2 * cch + 2
            p += p & 1
            cb_upx = struct.unpack_from("<H", std, p)[0]
            grpprl = std[p + 4:p + 2 + cb_upx]  # istd 2바이트 뒤
            for sprm, op in iter_sprms(grpprl):
                if sprm == sprmPOutLvl and op[0] < 9:
                    return op[0] + 1
        except struct.error:
            return None
        return None

    # ── 도형: 글상자·떠 있는 그림 ──

    def _plc(self, index: int, item_size: int) -> Tuple[List[int], List[bytes]]:
        fc, lcb = self._fc_lcb(index)
        plc = self.table[fc:fc + lcb]
        if lcb < 4:
            return [], []
        n = (lcb - 4) // (4 + item_size)
        cps = list(struct.unpack_from(f"<{n + 1}i", plc, 0))
        items = [plc[4 * (n + 1) + item_size * k:4 * (n + 1) + item_size * (k + 1)] for k in range(n)]
        return cps, items

    def _shape_anchors(self) -> Dict[int, int]:
        """본문 cp → 도형 id (PlcfSpa)."""
        cps, spas = self._plc(40, 26)
        return {cp: struct.unpack_from("<i", spa, 0)[0] for cp, spa in zip(cps, spas)}

    def _textboxes(self) -> Dict[int, str]:
        """도형 id → 글상자 글 (PlcftxbxTxt 의 이야기 하나씩)."""
        cps, items = self._plc(56, 22)
        out = {}
        for k, item in enumerate(items):
            lid = struct.unpack_from("<i", item, 14)[0]
            start, end = self.txbx_base + cps[k], self.txbx_base + cps[k + 1]
            text = "".join(ch for ch, _, _ in self.chars(start, end))
            paras = [_clean(p) for p in text.replace("\x07", "\r").split("\r")]
            joined = "\n".join(p for p in paras if p.strip())
            if joined:
                out[lid] = joined
        return out

    def _drawings(self) -> Tuple[Dict[int, int], List[Tuple[int, int, bytes]]]:
        """OfficeArtContent → ({도형 id: BStore 번호}, BStore 의 [(레코드 위치, 길이, 버퍼)])."""
        fc, lcb = self._fc_lcb(50)  # fcDggInfo
        buf = self.table[fc:fc + lcb]
        pib: Dict[int, int] = {}
        bstore: List[Tuple[int, int, bytes]] = []
        pos = 0
        while pos + 8 <= len(buf):
            _, rec_type, rec_len = struct.unpack_from("<HHI", buf, pos)
            if rec_type == 0xF000:  # DggContainer → BStore
                for t, s, e in _children(buf, pos + 8, pos + 8 + rec_len):
                    if t == 0xF001:
                        bstore = [(s2, e2, buf) for t2, s2, e2 in _children(buf, s, e) if t2 == 0xF007]
                pos += 8 + rec_len
            elif rec_type == 0xF002:  # DgContainer
                _collect_pibs(buf, pos + 8, pos + 8 + rec_len, pib)
                pos += 8 + rec_len
            else:
                pos += 1  # OfficeArtWordDrawing 앞의 dgglbl 1바이트
        return pib, bstore

    def floating_image(self, cp: int) -> Optional[Image]:
        spid = self.anchors.get(cp)
        k = self.shape_pib.get(spid) if spid is not None else None
        if not k or k > len(self.bstore):
            return None
        s, e, buf = self.bstore[k - 1]
        cb_name = buf[s + 33] if e - s > 33 else 0
        blobs = list(_office_art_blips(buf, s + 36 + cb_name, e))
        if not blobs:  # 그림이 WordDocument 스트림에 따로 있다(foDelay)
            fo = struct.unpack_from("<I", buf, s + 28)[0]
            if fo < len(self.word) - 8:
                rec_len = struct.unpack_from("<I", self.word, fo + 4)[0]
                blobs = list(_office_art_blips(self.word, fo, min(fo + 8 + rec_len, len(self.word))))
        if not blobs:
            return None
        return Image(id=f"bstore#{k}", data=blobs[0][0], mime=blobs[0][1])

    # ── 글 ──

    def chars(self, start: int = 0, end: Optional[int] = None) -> Iterator[Tuple[str, int, int]]:
        """cp 구간의 (글자, fc, cp). 기본은 본문(0 ~ ccpText)."""
        end = self.ccp_text if end is None else end
        k = max(bisect.bisect_right(self._piece_cps, start) - 1, 0)
        for pc in self.pieces[k:]:
            if pc.cp >= end:
                break
            lo, hi = max(pc.cp, start), min(pc.cp_end, end)
            if lo >= hi:
                continue
            if pc.compressed:
                raw = self.word[pc.fc + lo - pc.cp:pc.fc + hi - pc.cp]
                for k2, ch in enumerate(raw.decode("cp1252", "replace")):
                    yield ch, pc.fc + lo - pc.cp + k2, lo + k2
            else:
                base = pc.fc + 2 * (lo - pc.cp)
                raw = self.word[base:base + 2 * (hi - lo)]
                for k2 in range(len(raw) // 2):
                    yield chr(struct.unpack_from("<H", raw, 2 * k2)[0]), base + 2 * k2, lo + k2

    def para_props(self, fc: int, cache: Dict[int, _Fkp]) -> ParaProps:
        papx = self._fkp_prop(self.papx_fkps, cache, fc, papx=True)
        return _para_props(papx) if papx else ParaProps()

    def picture(self, fc: int, cache: Dict[int, _Fkp]) -> Optional[Image]:
        chpx = self._fkp_prop(self.chpx_fkps, cache, fc, papx=False)
        if not chpx:
            return None
        loc, special = None, False
        for sprm, op in iter_sprms(chpx):
            if sprm == sprmCFSpec:
                special = bool(op[0])
            elif sprm == sprmCPicLocation:
                loc = struct.unpack_from("<I", op)[0]
        if not special or loc is None:
            return None
        return _picf_image(self.data, loc)


# ── 그림 ──────────────────────────────────────────────────────────────────────

_BLIP_TYPES = {0xF01A: "image/emf", 0xF01B: "image/wmf", 0xF01C: "image/pict", 0xF01D: "image/jpeg",
               0xF01E: "image/png", 0xF01F: "image/bmp", 0xF029: "image/tiff", 0xF02A: "image/jpeg"}
# 인스턴스 값이 이것이면 UID 가 하나 더 붙는다
_TWO_UID = {0xF01A: 0x3D5, 0xF01B: 0x217, 0xF01C: 0x543, 0xF01D: 0x46B, 0xF01E: 0x6E1,
            0xF01F: 0x7A9, 0xF029: 0x6E5, 0xF02A: 0x6E3}


def _blip(rec_type: int, inst: int, body: bytes) -> Optional[Tuple[bytes, str]]:
    mime = _BLIP_TYPES.get(rec_type)
    if not mime:
        return None
    p = 32 if inst == _TWO_UID[rec_type] else 16
    if rec_type in (0xF01A, 0xF01B, 0xF01C):
        compressed = body[p + 32] == 0x00 if len(body) > p + 32 else False
        data = body[p + 34:]
        if compressed:
            try:
                data = zlib.decompress(data)
            except zlib.error:
                pass
        return data, mime
    data = body[p + 1:]
    if rec_type == 0xF01F:
        data = _dib_to_bmp(data)
    return data, mime


def _dib_to_bmp(dib: bytes) -> bytes:
    if len(dib) < 40:
        return dib
    size, _, _, _, bits, compression, _, _, _, used = struct.unpack_from("<IiiHHIIiiI", dib, 0)
    colors = used or (1 << bits if bits <= 8 else 0)
    offset = 14 + size + 4 * colors + (12 if compression == 3 and size == 40 else 0)
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib


def _office_art_blips(buf: bytes, start: int, end: int) -> Iterator[Tuple[bytes, str]]:
    pos = start
    while pos + 8 <= end:
        ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", buf, pos)
        body_start, body_end = pos + 8, min(pos + 8 + rec_len, end)
        ver, inst = ver_inst & 0x0F, ver_inst >> 4
        if ver == 0x0F:  # 컨테이너
            yield from _office_art_blips(buf, body_start, body_end)
        elif rec_type == 0xF007 and body_end - body_start >= 36:  # FBSE: 고정부 뒤에 blip 이 붙는다
            cb_name = buf[body_start + 33]
            yield from _office_art_blips(buf, body_start + 36 + cb_name, body_end)
        elif rec_type in _BLIP_TYPES:
            got = _blip(rec_type, inst, buf[body_start:body_end])
            if got:
                yield got
        pos = body_start + rec_len


def _children(buf: bytes, start: int, end: int) -> Iterator[Tuple[int, int, int]]:
    """OfficeArt 레코드 구간의 (종류, 본문 시작, 본문 끝)."""
    pos = start
    while pos + 8 <= end:
        _, rec_type, rec_len = struct.unpack_from("<HHI", buf, pos)
        yield rec_type, pos + 8, min(pos + 8 + rec_len, end)
        pos += 8 + rec_len


def _collect_pibs(buf: bytes, start: int, end: int, out: Dict[int, int]) -> None:
    """DgContainer 아래 도형마다 (spid → 그림 BStore 번호)."""
    for rec_type, s, e in _children(buf, start, end):
        if rec_type == 0xF004:  # SpContainer
            spid, pib = None, None
            for t, s2, e2 in _children(buf, s, e):
                if t == 0xF00A and e2 - s2 >= 4:
                    spid = struct.unpack_from("<I", buf, s2)[0]
                elif t == 0xF00B:
                    count = struct.unpack_from("<H", buf, s2 - 8)[0] >> 4
                    for k in range(count):
                        if s2 + 6 * k + 6 > e2:
                            break
                        opid, op = struct.unpack_from("<Hi", buf, s2 + 6 * k)
                        if opid & 0x3FFF == 0x0104:
                            pib = op
            if spid is not None and pib:
                out[spid] = pib
            _collect_pibs(buf, s, e, out)
        elif rec_type in (0xF002, 0xF003):  # Dg·SpgrContainer
            _collect_pibs(buf, s, e, out)


_CHAR_MAP = {0x0B: "\n", 0x09: "\t", 0x1E: "-", 0x1F: "", 0xA0: " "}


def _clean(text: str) -> str:
    """필드 명령을 버리고 특수 글자를 바꾼다(글상자 글용)."""
    out, stack = [], []
    for ch in text:
        c = ord(ch)
        if c == 0x13:
            stack.append(False)
        elif c == 0x14:
            if stack:
                stack[-1] = True
        elif c == 0x15:
            if stack:
                stack.pop()
        elif not (stack and not stack[-1]):
            out.append(_CHAR_MAP.get(c, ch if c >= 0x20 else ""))
    return "".join(out)


def _picf_image(data: bytes, loc: int) -> Optional[Image]:
    if loc + 8 > len(data):
        return None
    lcb, cb_header = struct.unpack_from("<iH", data, loc)
    mm = struct.unpack_from("<h", data, loc + 6)[0]
    start = loc + cb_header
    if mm == 0x66:  # MM_SHAPEFILE: 그림 이름이 붙는다
        start += 1 + data[start]
    for blob, mime in _office_art_blips(data, start, min(loc + lcb, len(data))):
        return Image(id=f"Data@{loc}", data=blob, mime=mime)
    return None


# ── 표 ────────────────────────────────────────────────────────────────────────

def _row_layout(tdef: Optional[bytes], vmerge: Dict[int, int]) -> Optional[Tuple[List[int], List[int], List[int]]]:
    """TDefTable → (칸 경계 x 들, 칸별 가로 병합, 칸별 세로 병합)."""
    if not tdef or len(tdef) < 3:
        return None
    itc = tdef[2]
    need = 3 + 2 * (itc + 1)
    if itc == 0 or len(tdef) < need:
        return None
    centers = list(struct.unpack_from(f"<{itc + 1}h", tdef, 3))
    horz, vert = [], []
    for k in range(itc):
        off = need + 20 * k
        grf = struct.unpack_from("<H", tdef, off)[0] if off + 2 <= len(tdef) else 0
        horz.append(grf & 0x3)
        vert.append(vmerge.get(k, (grf >> 5) & 0x3))
    return centers, horz, vert


def _build_table(rows: List[Tuple[List[str], Optional[Tuple[List[int], List[int], List[int]]]]]) -> Table:
    xs = sorted({x for _, lay in rows if lay for x in lay[0]})
    cells: List[Cell] = []
    above: Dict[int, Cell] = {}  # 열 시작 → 바로 위 행의 칸
    for r, (texts, lay) in enumerate(rows):
        current: Dict[int, Cell] = {}
        prev: Optional[Cell] = None
        for k, text in enumerate(texts):
            if lay and k < len(lay[0]) - 1 and xs:
                c0 = bisect.bisect_left(xs, lay[0][k])
                c1 = bisect.bisect_left(xs, lay[0][k + 1])
                horz, vert = lay[1][k], lay[2][k]
            else:
                c0 = (prev.col + prev.col_span) if prev else 0
                c1, horz, vert = c0 + 1, 0, 0
            span = max(c1 - c0, 1)
            if horz in (2, 3) and prev is not None:  # 앞 칸에 가로로 합쳐진 칸
                prev.col_span += span
                continue
            if vert == 1 and c0 in above:  # 위 칸에 세로로 합쳐진 칸
                above[c0].row_span += 1
                current[c0] = above[c0]
                prev = above[c0]
                continue
            cell = Cell(text, r, c0, 1, span)
            cells.append(cell)
            current[c0] = cell
            prev = cell
        above = current
    return Table(cells)


# ── 문단 조립 ─────────────────────────────────────────────────────────────────


def parse(data: bytes) -> Document:
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if not ole.exists("WordDocument"):
            raise ParseError("DOC: WordDocument 스트림이 없다")
        reader = _Reader(ole)
        papx_cache: Dict[int, _Fkp] = {}
        chpx_cache: Dict[int, _Fkp] = {}
        blocks: List[Block] = []
        rows: List[Tuple[List[str], object]] = []  # 진행 중인 표
        row: List[str] = []
        cell_buf: List[str] = []
        inner_rows: List[str] = []  # 중첩 표: 행마다 칸을 " | " 로 이은 글
        inner_row: List[str] = []
        inner_par: List[str] = []
        para: List[str] = []
        images: List[Image] = []
        field_stack: List[bool] = []  # 필드마다 결과 구간에 들어섰는가

        def flush_images():
            for img in images:
                blocks.append(Block("image", image=img))
            images.clear()

        def flush_table():
            # 칸·행 끝 표시 없이 끝난 표도 쌓인 글을 버리지 않는다
            close_inner()
            if any(t.strip() for t in cell_buf):
                row.append("\n".join(cell_buf).strip())
            cell_buf.clear()
            if row:
                rows.append((row[:], None))
                row.clear()
            if rows:
                blocks.append(Block("table", table=_build_table(rows)))
                rows.clear()
            flush_images()

        def close_inner():
            if inner_rows or inner_row:
                if inner_row:
                    inner_rows.append(" | ".join(inner_row))
                cell_buf.append(" / ".join(inner_rows))
                inner_rows.clear()
                inner_row.clear()

        for ch, fc, cp in reader.chars():
            code = ord(ch)
            if code == 0x13:
                field_stack.append(False)
                continue
            if code == 0x14:
                if field_stack:
                    field_stack[-1] = True
                continue
            if code == 0x15:
                if field_stack:
                    field_stack.pop()
                continue
            if field_stack and not field_stack[-1]:
                continue  # 필드 명령 글은 버린다
            if code in (0x0D, 0x07, 0x0C):
                props = reader.para_props(fc, papx_cache)
                text = "".join(para)
                para.clear()
                if props.in_table or props.itap >= 1:
                    if props.itap > 1:  # 중첩 표는 바깥 칸 글로 잇는다
                        if props.inner_ttp:
                            inner_rows.append(" | ".join(inner_row))
                            inner_row.clear()
                        else:
                            inner_par.append(text)
                            if props.inner_cell or code == 0x07:
                                inner_row.append(" ".join(" ".join(inner_par).split()))
                                inner_par.clear()
                        continue
                    close_inner()
                    if props.ttp:
                        rows.append((row[:], _row_layout(props.tdef, props.vmerge)))
                        row.clear()
                        cell_buf.clear()
                        continue
                    cell_buf.append(text)
                    if code == 0x07:  # 칸 끝
                        row.append("\n".join(cell_buf).strip())
                        cell_buf.clear()
                    continue
                flush_table()
                if text.strip():
                    level = props.outline + 1 if props.outline is not None and props.outline < 9 else None
                    blocks.append(Block("paragraph", text=text,
                                        heading_level=level or reader.style_outline.get(props.istd)))
                flush_images()
                continue
            if code == 0x01:
                img = reader.picture(fc, chpx_cache)
                if img:
                    images.append(img)
                continue
            if code == 0x08:  # 떠 있는 도형: 글상자 글은 제자리에, 그림은 문단 뒤에
                box = reader.textboxes.get(reader.anchors.get(cp, -1))
                if box:
                    para.append(box)
                img = reader.floating_image(cp)
                if img:
                    images.append(img)
                continue
            if code in (0x02, 0x05):
                continue
            para.append(_CHAR_MAP.get(code, ch if code >= 0x20 else ""))
        flush_table()
        tail = "".join(para)
        if tail.strip():
            blocks.append(Block("paragraph", text=tail))
        flush_images()
        return Document(format="doc", blocks=blocks, warnings=reader.warnings)
