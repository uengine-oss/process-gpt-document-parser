"""모든 포맷이 같이 내는 출력 구조."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

BBox = Tuple[float, float, float, float]


@dataclass
class Cell:
    text: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1


@dataclass
class Table:
    cells: List[Cell]

    @property
    def n_rows(self) -> int:
        return max((c.row + c.row_span for c in self.cells), default=0)

    @property
    def n_cols(self) -> int:
        return max((c.col + c.col_span for c in self.cells), default=0)

    @property
    def has_merged(self) -> bool:
        return any(c.row_span > 1 or c.col_span > 1 for c in self.cells)

    def grid(self, fill_merged: bool = True) -> List[List[str]]:
        """칸 격자. fill_merged 면 병합 칸 값을 덮인 칸마다 두고, 아니면 첫 칸에만 둔다."""
        rows, cols = self.n_rows, self.n_cols
        grid = [["" for _ in range(cols)] for _ in range(rows)]
        for c in self.cells:
            for r in range(c.row, min(c.row + c.row_span, rows)):
                for k in range(c.col, min(c.col + c.col_span, cols)):
                    if fill_merged or (r == c.row and k == c.col):
                        grid[r][k] = c.text
        return grid

    def to_markdown(self, fill_merged: bool = True) -> str:
        grid = self.grid(fill_merged)
        if not grid:
            return ""
        lines = []
        for i, row in enumerate(grid):
            cells = [(v.replace("\n", " / ").replace("|", "\\|") or " ") for v in row]
            lines.append("| " + " | ".join(cells) + " |")
            if i == 0:
                lines.append("| " + " | ".join(["---"] * len(row)) + " |")
        return "\n".join(lines)

    def to_html(self) -> str:
        """병합을 rowspan·colspan 으로 그대로 적은 HTML 표."""
        from html import escape

        by_row: Dict[int, List[Cell]] = {}
        for c in self.cells:
            by_row.setdefault(c.row, []).append(c)
        out = ["<table>"]
        for r in range(self.n_rows):
            tds = []
            for c in sorted(by_row.get(r, []), key=lambda c: c.col):
                span = (f' rowspan="{c.row_span}"' if c.row_span > 1 else "") + \
                       (f' colspan="{c.col_span}"' if c.col_span > 1 else "")
                # 태그를 지우고 읽는 쪽에서도 줄 사이 낱말이 붙지 않게 공백을 둔다
                tds.append(f"<td{span}>{escape(c.text).replace(chr(10), ' <br>')}</td>")
            out.append("<tr>" + "".join(tds) + "</tr>")
        out.append("</table>")
        return "\n".join(out)


TABLE_FORMATS = ("markdown", "markdown-first", "html")


def render_table(table: Table, table_format: str = "markdown") -> str:
    if table_format == "html":
        return table.to_html()
    if table_format in ("markdown", "markdown-first"):
        return table.to_markdown(fill_merged=table_format == "markdown")
    raise ValueError(f"table_format 은 {TABLE_FORMATS} 중 하나")


@dataclass
class Image:
    id: str
    data: bytes = field(repr=False)
    mime: str
    description: Optional[str] = None


@dataclass
class Comment:
    id: str
    text: str
    author: str = ""
    date: str = ""
    anchor: str = ""


@dataclass
class Block:
    """문서 흐름의 한 덩어리. kind 는 paragraph | table | image."""

    kind: str
    text: str = ""
    heading_level: Optional[int] = None
    table: Optional[Table] = None
    image: Optional[Image] = None
    page: Optional[int] = None  # 0부터. 쪽 정보가 있는 포맷(PDF)만
    bbox: Optional[BBox] = None  # PDF 좌표(pt, 왼쪽 위 원점)
    comments: List[Comment] = field(default_factory=list)
    ocr: bool = False  # 글을 OCR 콜백이 만들었다

    def render(self, table_format: str = "markdown") -> str:
        if self.kind == "table":
            body = render_table(self.table, table_format) if self.table else self.text
        elif self.kind == "image":
            desc = (self.image.description if self.image else None) or self.text
            return f"[그림: {desc.strip()}]" if desc and desc.strip() else ""
        elif self.heading_level:
            body = "#" * min(self.heading_level, 6) + " " + self.text.strip()
        else:
            body = self.text
        notes = [f'[메모 #{c.id} @ "{c.anchor[:60]}": {c.text}]' for c in self.comments]
        return "\n".join([body] + notes) if notes else body


@dataclass
class Document:
    format: str
    blocks: List[Block] = field(default_factory=list)
    page_sizes: Optional[List[Tuple[float, float]]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def images(self) -> List[Image]:
        return [b.image for b in self.blocks if b.kind == "image" and b.image]

    def to_markdown(self, table_format: str = "markdown") -> str:
        """table_format: markdown(병합 칸 값을 덮인 칸마다, 기본) | markdown-first(첫 칸에만) | html(rowspan·colspan).

        기본값 근거는 docs/VERIFICATION.md "표 출력 형식".
        """
        return "\n\n".join(t for t in (b.render(table_format) for b in self.blocks) if t.strip())

    def pages(self) -> List[List[Block]]:
        """쪽별 블록. 쪽 정보가 없는 포맷은 전체가 한 쪽이다."""
        if not self.page_sizes:
            return [list(self.blocks)]
        out: List[List[Block]] = [[] for _ in self.page_sizes]
        for b in self.blocks:
            out[b.page or 0].append(b)
        return out

    def to_dict(self, include_image_data: bool = False) -> Dict[str, Any]:
        def block(b: Block) -> Dict[str, Any]:
            d: Dict[str, Any] = {"kind": b.kind, "text": b.render() if b.kind != "paragraph" else b.text}
            if b.heading_level:
                d["heading_level"] = b.heading_level
            if b.page is not None:
                d["page"] = b.page
            if b.bbox:
                d["bbox"] = list(b.bbox)
            if b.ocr:
                d["ocr"] = True
            if b.table:
                d["cells"] = [c.__dict__ for c in b.table.cells]
            if b.image:
                d["image"] = {"id": b.image.id, "mime": b.image.mime, "description": b.image.description}
                if include_image_data:
                    import base64

                    d["image"]["data"] = base64.b64encode(b.image.data).decode()
            if b.comments:
                d["comments"] = [c.__dict__ for c in b.comments]
            return d

        return {
            "format": self.format,
            "page_sizes": self.page_sizes,
            "metadata": self.metadata,
            "warnings": self.warnings,
            "blocks": [block(b) for b in self.blocks],
        }
