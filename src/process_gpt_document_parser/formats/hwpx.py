"""HWPX(OWPML, KS X 6101) — ZIP 안의 Contents/section*.xml 을 문서 순서로 읽는다."""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from ..errors import EncryptedDocumentError, ParseError
from ..images import mime_from_name, sniff_mime
from ..model import Block, Cell, Document, Image, Table


def _local(elem) -> str:
    t = elem.tag
    return t.split("}", 1)[1] if "}" in t else t


_SPACING = {"tab", "lineBreak", "fwSpace", "nbSpace"}
# 머리말·꼬리말은 첫 문단의 조판 부호 안에 들어 있어 본문 글로 섞여 나온다
_NOT_BODY = {"header", "footer"}


def _t_text(node) -> str:
    """<t> 하나의 글 — <tab/> 같은 인라인 요소 뒤 tail 까지."""
    parts = [node.text or ""]
    for child in node:
        if _local(child) in _SPACING:
            parts.append(" ")
        parts.append(child.tail or "")
    return "".join(parts)


def _iter_body(elem, skip=_NOT_BODY):
    for child in elem:
        if _local(child) in skip:
            continue
        yield child
        yield from _iter_body(child, skip)


def _paragraph_text(p) -> str:
    return "".join(_t_text(n) for n in _iter_body(p, _NOT_BODY | {"tbl"}) if _local(n) == "t")


def _int_attr(elem, name: str, default: int) -> int:
    for k, v in elem.attrib.items():
        if k.endswith(name):
            try:
                return int(v)
            except ValueError:
                return default
    return default


def _table(tbl) -> Table:
    cells: List[Cell] = []
    for tr in tbl:
        if _local(tr) != "tr":
            continue
        for tc in tr:
            if _local(tc) != "tc":
                continue
            row = col = 0
            row_span = col_span = 1
            for cc in tc:
                tag = _local(cc)
                if tag == "cellAddr":
                    col, row = _int_attr(cc, "colAddr", 0), _int_attr(cc, "rowAddr", 0)
                elif tag == "cellSpan":
                    col_span, row_span = _int_attr(cc, "colSpan", 1), _int_attr(cc, "rowSpan", 1)
            # 칸 안 문단 사이는 띄운다(붙이면 "422"·"423" 이 "422423" 이 된다)
            parts = [" " if _local(s) == "p" else _t_text(s) for s in tc.iter() if _local(s) in ("p", "t")]
            text = " ".join("".join(parts).split())
            cells.append(Cell(text, row, col, max(row_span, 1), max(col_span, 1)))
    return Table(cells)


class _Reader:
    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        self.names = set(z.namelist())
        self.manifest = self._manifest()
        self.outline = self._outline_levels()
        self.blocks: List[Block] = []

    def _manifest(self) -> Dict[str, Tuple[str, str]]:
        """content.hpf 의 opf:item → {id: (href, media-type)}."""
        try:
            root = ET.fromstring(self.z.read("Contents/content.hpf"))
        except (KeyError, ET.ParseError):
            return {}
        return {i.get("id"): (i.get("href"), i.get("media-type") or "")
                for i in root.iter() if _local(i) == "item" and i.get("id") and i.get("href")}

    def _outline_levels(self) -> Dict[str, int]:
        """header.xml 의 개요 문단 모양 → {paraPr id: 1부터의 헤딩 수준}."""
        try:
            root = ET.fromstring(self.z.read("Contents/header.xml"))
        except (KeyError, ET.ParseError):
            return {}
        levels: Dict[str, int] = {}
        for pr in root.iter():
            if _local(pr) != "paraPr":
                continue
            for h in pr:
                if _local(h) == "heading" and h.get("type") == "OUTLINE":
                    try:
                        levels[pr.get("id", "")] = int(h.get("level", "0")) + 1
                    except ValueError:
                        pass
        return levels

    def _image(self, ref: str) -> Optional[Image]:
        href, media = self.manifest.get(ref, (None, ""))
        candidates = ([href] if href else []) + sorted(
            n for n in self.names if n.startswith("BinData/") and ref in n)
        for name in candidates:
            if name in self.names:
                data = self.z.read(name)
                return Image(id=ref, data=data, mime=media or sniff_mime(data, mime_from_name(name)))
        return None

    def walk(self, elem) -> None:
        lt = _local(elem)
        if lt in _NOT_BODY:
            return
        if lt == "tbl":
            table = _table(elem)
            if table.cells:
                self.blocks.append(Block("table", table=table))
            self._images_in(elem, _NOT_BODY)  # 칸 안 그림은 표 뒤에
            return
        if lt == "p":
            if any(_local(d) == "tbl" for d in _iter_body(elem)):
                self._mixed(elem)
            else:
                text = _paragraph_text(elem)
                if text.strip():
                    self.blocks.append(Block("paragraph", text=text,
                                             heading_level=self.outline.get(elem.get("paraPrIDRef", ""))))
            # 그림은 표와 같은 문단에 묶이는 일이 흔해 문단을 다 읽은 뒤 붙인다
            self._images_in(elem, _NOT_BODY | {"tbl"})
            return
        for child in elem:
            self.walk(child)

    def _images_in(self, elem, skip) -> None:
        for sub in _iter_body(elem, skip):
            if _local(sub) == "img" and sub.get("binaryItemIDRef"):
                img = self._image(sub.get("binaryItemIDRef"))
                if img:
                    self.blocks.append(Block("image", image=img))

    def _mixed(self, p) -> None:
        """표를 품은 문단: 표 앞뒤 글을 각각 문단으로 두고 표는 제자리에."""
        level = self.outline.get(p.get("paraPrIDRef", ""))
        buf: List[str] = []

        def flush():
            text = "".join(buf)
            if text.strip():
                self.blocks.append(Block("paragraph", text=text, heading_level=level))
            buf.clear()

        def visit(elem):
            for child in elem:
                tag = _local(child)
                if tag in _NOT_BODY:
                    continue
                if tag == "t":
                    buf.append(_t_text(child))
                elif tag == "tbl":
                    flush()
                    self.walk(child)
                else:
                    visit(child)

        visit(p)
        flush()


def _encrypted(z: zipfile.ZipFile) -> bool:
    try:
        manifest = z.read("META-INF/manifest.xml")
    except KeyError:
        return False
    return b"encryption-data" in manifest


def parse(data: bytes) -> Document:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if _encrypted(z):
            raise EncryptedDocumentError("암호가 걸린 HWPX")
        sections = sorted((n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                          key=lambda n: int(re.search(r"\d+", n).group()))
        if not sections:
            raise ParseError("HWPX: Contents/section*.xml 이 없다")
        reader = _Reader(z)
        for name in sections:
            reader.walk(ET.fromstring(z.read(name)))
        return Document(format="hwpx", blocks=reader.blocks)
