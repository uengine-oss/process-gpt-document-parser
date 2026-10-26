"""DOCX(OOXML WordprocessingML) — word/document.xml 을 본문 순서로 읽는다. 메모·헤딩·그림 위치를 지킨다."""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from ..errors import ParseError
from ..images import mime_from_name, sniff_mime
from ..model import Block, Cell, Comment, Document, Image, Table

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
V = "{urn:schemas-microsoft-com:vml}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _local(el) -> str:
    return el.tag.split("}", 1)[1] if "}" in el.tag else el.tag


def _paragraph_text(p) -> str:
    parts = []
    for el in p.iter():
        tag = _local(el)
        if tag == "t":
            parts.append(el.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _comment_anchors(p) -> List[Dict[str, object]]:
    """문단 안 메모의 (id, 글 범위)."""
    offset = 0
    starts: Dict[str, int] = {}
    out: List[Dict[str, object]] = []
    seen = set()
    for el in p.iter():
        tag = _local(el)
        if tag == "t":
            offset += len(el.text or "")
        elif tag in ("tab", "br", "cr"):
            offset += 1
        elif tag == "commentRangeStart":
            starts[el.get(W + "id")] = offset
        elif tag == "commentRangeEnd":
            cid = el.get(W + "id")
            if cid in starts:
                out.append({"id": cid, "span": (starts.pop(cid), offset)})
                seen.add(cid)
        elif tag == "commentReference":
            cid = el.get(W + "id")
            if cid not in starts and cid not in seen:
                out.append({"id": cid, "span": (offset, offset)})
                seen.add(cid)
    return out


_HEADING_NAME = re.compile(r"^(?:heading|제목)\s*(\d)$", re.IGNORECASE)


def _heading_styles(styles_xml: bytes) -> Dict[str, int]:
    """styles.xml → {styleId: 헤딩 수준}. outlineLvl 또는 기본 제목 스타일 이름, basedOn 을 따라 상속."""
    root = ET.fromstring(styles_xml)
    own: Dict[str, Optional[int]] = {}
    based_on: Dict[str, str] = {}
    for style in root.iter(W + "style"):
        sid = style.get(W + "styleId") or ""
        level: Optional[int] = None
        outline = style.find(f"{W}pPr/{W}outlineLvl")
        name = style.find(W + "name")
        if outline is not None and (outline.get(W + "val") or "").isdigit():
            value = int(outline.get(W + "val"))
            level = value + 1 if value < 9 else None  # 9 = 본문 수준
        elif name is not None:
            m = _HEADING_NAME.match((name.get(W + "val") or "").strip())
            if m:
                level = int(m.group(1))
        own[sid] = level
        parent = style.find(W + "basedOn")
        if parent is not None:
            based_on[sid] = parent.get(W + "val") or ""
    levels: Dict[str, int] = {}
    for sid in own:
        cur, hops = sid, 0
        while cur and own.get(cur) is None and hops < 10:
            cur, hops = based_on.get(cur, ""), hops + 1
        if cur and own.get(cur):
            levels[sid] = own[cur]
    return levels


def _body_children(container):
    """본문 순서의 p/tbl — 콘텐츠 컨트롤(sdt, 목차 등) 안쪽까지 편다."""
    for child in container:
        if _local(child) == "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                yield from _body_children(content)
        else:
            yield child


class _Reader:
    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        names = set(z.namelist())
        self.rels = self._rels(z.read("word/_rels/document.xml.rels")) if "word/_rels/document.xml.rels" in names else {}
        self.comments = self._comments(z.read("word/comments.xml")) if "word/comments.xml" in names else {}
        self.levels = _heading_styles(z.read("word/styles.xml")) if "word/styles.xml" in names else {}
        self.blocks: List[Block] = []

    @staticmethod
    def _rels(xml: bytes) -> Dict[str, str]:
        out = {}
        for rel in ET.fromstring(xml).iter(PR + "Relationship"):
            target = rel.get("Target") or ""
            if rel.get("TargetMode") == "External":
                continue
            out[rel.get("Id")] = target.lstrip("/") if target.startswith("/") else "word/" + target
        return out

    @staticmethod
    def _comments(xml: bytes) -> Dict[str, Comment]:
        out = {}
        for c in ET.fromstring(xml).iter(W + "comment"):
            cid = c.get(W + "id")
            text = "\n".join(_paragraph_text(p) for p in c.iter(W + "p")).strip()
            out[cid] = Comment(id=cid, text=text, author=c.get(W + "author") or "", date=c.get(W + "date") or "")
        return out

    def _anchored_comments(self, p) -> List[Comment]:
        text = _paragraph_text(p)
        out = []
        for a in _comment_anchors(p):
            meta = self.comments.get(a["id"])
            if meta:
                s, e = a["span"]
                out.append(Comment(id=meta.id, text=meta.text, author=meta.author, date=meta.date, anchor=text[s:e]))
        return out

    def _images(self, el) -> List[Image]:
        out = []
        rids = [b.get(R + "embed") for b in el.iter(A + "blip")] + [i.get(R + "id") for i in el.iter(V + "imagedata")]
        for rid in rids:
            path = self.rels.get(rid or "")
            if not path:
                continue
            try:
                data = self.z.read(path)
            except KeyError:
                continue
            out.append(Image(id=path, data=data, mime=sniff_mime(data, mime_from_name(path))))
        return out

    def _heading_level(self, p) -> Optional[int]:
        outline = p.find(f"{W}pPr/{W}outlineLvl")
        if outline is not None and (outline.get(W + "val") or "").isdigit():
            value = int(outline.get(W + "val"))
            return value + 1 if value < 9 else None
        style = p.find(f"{W}pPr/{W}pStyle")
        return self.levels.get(style.get(W + "val") or "") if style is not None else None

    def paragraph(self, p) -> None:
        text = _paragraph_text(p)
        comments = self._anchored_comments(p)
        if text.strip() or comments:
            self.blocks.append(Block("paragraph", text=text, heading_level=self._heading_level(p), comments=comments))
        for img in self._images(p):
            self.blocks.append(Block("image", image=img))

    def table(self, tbl) -> None:
        cells: List[Cell] = []
        open_cells: Dict[int, Cell] = {}  # 세로 병합 중인 열 → 시작 칸
        comments: List[Comment] = []
        images: List[Image] = []
        for r, tr in enumerate(tbl.findall(W + "tr")):
            before = tr.find(f"{W}trPr/{W}gridBefore")
            col = int(before.get(W + "val", "0")) if before is not None else 0
            for tc in tr.findall(W + "tc"):
                pr = tc.find(W + "tcPr")
                gs = pr.find(W + "gridSpan") if pr is not None else None
                vm = pr.find(W + "vMerge") if pr is not None else None
                span = max(int(gs.get(W + "val", "1")), 1) if gs is not None else 1
                merge = (vm.get(W + "val") or "continue") if vm is not None else None
                if merge == "continue" and col in open_cells:
                    open_cells[col].row_span += 1
                else:
                    cell = Cell(_cell_text(tc), r, col, 1, span)
                    cells.append(cell)
                    if merge == "restart":
                        open_cells[col] = cell
                    else:
                        open_cells.pop(col, None)
                for p in tc.iter(W + "p"):
                    comments.extend(self._anchored_comments(p))
                images.extend(self._images(tc))
                col += span
        if cells:
            self.blocks.append(Block("table", table=Table(cells), comments=comments))
        for img in images:
            self.blocks.append(Block("image", image=img))


def _cell_text(tc) -> str:
    """칸 글. 칸 안 중첩 표는 행을 ' / ', 칸을 ' | ' 로 잇는다."""
    parts = []
    for child in tc:
        if child.tag == W + "p":
            parts.append(_paragraph_text(child))
        elif child.tag == W + "tbl":
            rows = [" | ".join(_cell_text(c).replace("\n", " ") for c in tr.findall(W + "tc"))
                    for tr in child.findall(W + "tr")]
            parts.append(" / ".join(rows))
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                parts.append(_cell_text(content))
    return "\n".join(parts).strip()


def parse(data: bytes) -> Document:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "word/document.xml" not in z.namelist():
            raise ParseError("DOCX: word/document.xml 이 없다")
        reader = _Reader(z)
        body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
        for child in _body_children(body if body is not None else []):
            tag = _local(child)
            if tag == "p":
                reader.paragraph(child)
            elif tag == "tbl":
                reader.table(child)
        return Document(format="docx", blocks=reader.blocks)
