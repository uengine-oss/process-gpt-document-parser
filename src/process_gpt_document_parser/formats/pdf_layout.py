"""PDF 글자를 줄·블록으로 묶는다. 글자는 콘텐츠 스트림에 기록된 순서를 따른다(읽기 순서의 근거)."""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

# 같은 줄로 볼 세로 어긋남(글자 크기 대비)
_SAME_LINE = 0.5
# 같은 줄 안에서 이보다 벌어지면 다른 줄(칸)로 끊는다(글자 크기 대비)
_LINE_BREAK_GAP = 1.0
# 공백 문자가 없어도 이보다 벌어지면 띄어 쓴다(글자 크기 대비)
_SPACE_GAP = 0.2
# 다음 줄을 같은 블록으로 볼 줄 사이 빈틈 상한(줄 높이 대비)
_PARA_GAP = 0.8


class Bins:
    """구간을 한 축의 칸에 나눠 담고, 한 점·구간에 걸칠 수 있는 것만 원래 순서로 꺼낸다(후보일 뿐, 조건은 부르는 쪽이 다시 본다)."""

    def __init__(self, items, lo, hi, size: float = 8.0):
        self.items, self.size, self.bins = items, size, {}
        for i, it in enumerate(items):
            for k in range(math.floor(lo(it) / size), math.floor(hi(it) / size) + 1):
                self.bins.setdefault(k, []).append(i)

    def ids(self, a: float, b: Optional[float] = None) -> set:
        b = a if b is None else b
        found = set()
        for k in range(math.floor(a / self.size), math.floor(b / self.size) + 1):
            found.update(self.bins.get(k, ()))
        return found

    def around(self, a: float, b: Optional[float] = None) -> list:
        return [self.items[i] for i in sorted(self.ids(a, b))]


def center_bins(chars) -> Bins:
    """글자 가운데 높이로 담는다. 칸 안 글자 찾기(가운데가 칸 안)에 쓴다."""
    return Bins(chars, lambda c: (c["top"] + c["bottom"]) / 2, lambda c: (c["top"] + c["bottom"]) / 2)


@dataclass
class Line:
    x0: float
    top: float
    x1: float
    bottom: float
    size: float
    parts: List[str] = field(default_factory=list)
    last_x1: float = 0.0
    chars: List[Dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def text(self) -> str:
        return "".join(self.parts).strip()

    @property
    def bbox(self) -> List[float]:
        return [self.x0, self.top, self.x1, self.bottom]

    @property
    def center(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class TextBlock:
    lines: List[Line]

    @property
    def bbox(self) -> List[float]:
        return [min(l.x0 for l in self.lines), min(l.top for l in self.lines),
                max(l.x1 for l in self.lines), max(l.bottom for l in self.lines)]

    @property
    def text(self) -> str:
        return _join_lines([t for t in (l.text for l in self.lines) if t])


# 합자 글리프는 풀어 쓴다(검색·인용에서 "flow" 가 "ﬂow" 로 남지 않게)
_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
              "ﬅ": "st", "ﬆ": "st"}
# 따로 찍힌 발음 기호 → 결합 문자
_ACCENTS = {"˜": "̃", "ˆ": "̂", "´": "́", "¨": "̈",
            "¸": "̧", "ˇ": "̌", "˚": "̊", "˝": "̋",
            "˘": "̆", "¯": "̄", "˙": "̇"}


def clean_chars(chars: List[Dict[str, Any]]) -> None:
    """글자 dict 의 text 를 제자리에서 고친다(pdfplumber 표 추출도 같은 dict 를 본다).

    합자를 풀고, 기본 글자 위에 겹쳐 따로 찍힌 발음 기호는 그 글자와 합친다. 겹치지 않는 ´ 는 따옴표로 둔다.
    """
    for i, ch in enumerate(chars):
        t = ch.get("text", "")
        if t in _LIGATURES:
            ch["text"] = _LIGATURES[t]
        elif t in _ACCENTS:
            mid = (ch["x0"] + ch["x1"]) / 2
            for j in (i - 1, i + 1):
                if 0 <= j < len(chars):
                    base = chars[j]
                    bt = base.get("text", "")
                    if len(bt) == 1 and bt.isalpha() and base["x0"] <= mid <= base["x1"]:
                        base["text"] = unicodedata.normalize("NFC", bt + _ACCENTS[t])
                        ch["text"] = ""
                        break


def _join_lines(texts: List[str]) -> str:
    """블록의 줄을 잇는다. 줄 끝 하이픈 뒤 소문자로 이어지면 낱말을 붙인다."""
    out = ""
    for t in texts:
        if len(out) >= 2 and out[-1] in "-­" and out[-2].isalpha() and t[:1].islower():
            out = out[:-1] + t
        else:
            out = f"{out}\n{t}" if out else t
    return out


def _continues(line: Line, ch: Dict[str, Any]) -> bool:
    size = max(line.size, ch["size"], 1.0)
    mid = (ch["top"] + ch["bottom"]) / 2
    if abs(mid - line.center) > _SAME_LINE * size:
        return False
    gap = ch["x0"] - line.last_x1
    return -0.5 * size <= gap <= _LINE_BREAK_GAP * size


def lines_from_chars(chars: List[Dict[str, Any]], infer_spaces: bool = True) -> List[Line]:
    """스트림 순서의 글자 → 줄. 앞 줄을 잇지 못하는 글자에서 새 줄을 시작한다.

    infer_spaces=False 면 간격만 보고 띄어 쓰지 않는다(공백을 글자로 담는 PDF 의 균등 배분 글자 간격).
    """
    lines: List[Line] = []
    members: List[List[Dict[str, Any]]] = []
    cur: Optional[Line] = None
    for ch in chars:
        text = ch.get("text", "")
        if not text or not ch.get("upright", True):
            continue
        if cur is not None and _continues(cur, ch):
            members[-1].append(ch)
            gap = ch["x0"] - cur.last_x1
            if infer_spaces and gap > _SPACE_GAP * max(cur.size, ch["size"]) and cur.parts and not cur.parts[-1].endswith(" ") \
                    and not text.startswith(" "):
                cur.parts.append(" ")
            cur.parts.append(text)
            cur.x1 = max(cur.x1, ch["x1"])
            cur.top, cur.bottom = min(cur.top, ch["top"]), max(cur.bottom, ch["bottom"])
            cur.last_x1 = max(cur.last_x1, ch["x1"]) if not text.isspace() else ch["x1"]
            continue
        if text.isspace():
            continue
        cur = Line(ch["x0"], ch["top"], ch["x1"], ch["bottom"], ch["size"], [text], ch["x1"])
        lines.append(cur)
        members.append([ch])
    for line, cs in zip(lines, members):
        line.chars = cs
        if not infer_spaces:
            _space_wide_gaps(line, cs)
    return [l for l in lines if l.text]


def _space_wide_gaps(line: Line, cs: List[Dict[str, Any]]) -> None:
    """공백을 글자로 담는 PDF 에도 공백 글자 없이 찍은 줄(양쪽 정렬 본문)이 있다. 그 줄의 보통 글자 간격보다
    확 벌어진 틈은 띄운다. 균등 배분 줄은 모든 간격이 함께 벌어져 튀는 틈이 없다."""
    ink = [(a, b) for a, b in zip(cs, cs[1:]) if a["text"].strip() and b["text"].strip()]
    if not ink:
        return
    gaps = sorted(b["x0"] - a["x1"] for a, b in ink)
    usual = gaps[len(gaps) // 2]
    parts = [cs[0]["text"]]
    for a, b in zip(cs, cs[1:]):
        if a["text"].strip() and b["text"].strip() and b["x0"] - a["x1"] - usual > _SPACE_GAP * max(a["size"], b["size"]):
            parts.append(" ")
        parts.append(b["text"])
    line.parts = parts


def _x_overlap(a: List[float], b: List[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2]


def blocks_from_lines(lines: List[Line]) -> List[TextBlock]:
    """이어지는 줄을 블록으로. 바로 아래로 이어지거나 같은 높이 옆에 놓인 줄은 같은 블록."""
    blocks: List[TextBlock] = []
    for line in lines:
        if blocks:
            prev = blocks[-1].lines[-1]
            h = max(prev.bottom - prev.top, line.bottom - line.top, 1.0)
            below = 0 <= line.top - prev.bottom + 0.5 <= _PARA_GAP * h and _x_overlap(blocks[-1].bbox, line.bbox)
            beside = abs(line.center - prev.center) <= _SAME_LINE * h and line.x0 >= prev.x1
            if below or beside:
                blocks[-1].lines.append(line)
                continue
        blocks.append(TextBlock([line]))
    return blocks


# ── 괘선 없는 표 ──────────────────────────────────────────────────────────────
# 표의 한 행은 같은 높이에 글이 옆으로 늘어선다(블록 하나 안의 줄들, 또는 이어지는 블록들). 본문 문단은 줄이
# 위아래로 쌓인다. 옆으로 늘어선 행이 세 개 이상 이어지고 칸 수와 칸의 가로 범위가 맞으면 표로 본다.
# 글을 지우거나 순서를 바꾸지 않고 같은 줄을 표로 감싸기만 한다.

MIN_ROWS = 3
MIN_CELLS = 2
_BAND = 0.5
_ROW_GAP = 2.0
# 칸 사이 최소 틈(줄 높이 대비). 한 줄이 조각난 본문·목록·목차는 틈이 이보다 좁았다
_CELL_GAP = 1.0


def line_cells(block: TextBlock) -> Optional[List[Dict[str, Any]]]:
    """블록의 줄들이 한 높이에 옆으로 늘어서 있으면 왼쪽부터 칸 목록, 아니면 None."""
    cells = [{"bbox": l.bbox, "text": l.text} for l in block.lines if l.text]
    if not cells:
        return None
    height = median(c["bbox"][3] - c["bbox"][1] for c in cells) or 1.0
    centers = [(c["bbox"][1] + c["bbox"][3]) / 2 for c in cells]
    if max(centers) - min(centers) > height * _BAND:
        return None
    cells.sort(key=lambda c: c["bbox"][0])
    if any(b["bbox"][0] - a["bbox"][2] < height * _CELL_GAP for a, b in zip(cells, cells[1:])):
        return None
    return cells


def block_rows(block: TextBlock) -> Optional[List[List[Dict[str, Any]]]]:
    """한 블록에 여러 행이 묶인 선 없는 표(줄 간격이 좁은 표): 높이별로 묶은 줄들이 각각 옆으로 늘어선 칸이면 행 목록."""
    rows: List[List[Line]] = []
    for l in sorted((l for l in block.lines if l.text), key=lambda l: (l.center, l.x0)):
        if rows and abs(l.center - rows[-1][0].center) <= (l.bottom - l.top) * _BAND:
            rows[-1].append(l)
        else:
            rows.append([l])
    if len(rows) < 2:
        return None
    out = []
    for r in rows:
        cells = line_cells(TextBlock(r))
        if cells is None:
            return None
        out.append(cells)
    return out if sum(len(r) >= MIN_CELLS for r in out) >= MIN_ROWS else None


_TAB_TOL = 0.5


def tab_rows(block: TextBlock) -> Optional[List[List[Dict[str, Any]]]]:
    """탭 정렬 표: 머리 한 줄은 낱말 간격으로만 띄었지만, 아래 값 행 칸(3칸 이상)이 모두 머리 낱말 첫 글자와 같은 x 에서
    시작한다(탭 위치). 값 행이 하나뿐이어도 그 자리에서 머리를 칸으로 가른다."""
    rows: List[List[Line]] = []
    for l in sorted((l for l in block.lines if l.text), key=lambda l: (l.center, l.x0)):
        if rows and abs(l.center - rows[-1][0].center) <= (l.bottom - l.top) * _BAND:
            rows[-1].append(l)
        else:
            rows.append([l])
    if len(rows) < 2 or len(rows[0]) != 1:
        return None
    body = [line_cells(TextBlock(r)) for r in rows[1:]]
    if any(b is None or len(b) < 3 or len(b) != len(body[0]) for b in body):
        return None
    starts = [c["bbox"][0] for c in body[0]]
    if any(abs(c["bbox"][0] - s) > _TAB_TOL for b in body[1:] for c, s in zip(b, starts)):
        return None
    ink = [c for c in rows[0][0].chars if c["text"].strip()]
    if not ink:
        return None
    # 칸 시작마다 그 자리에서 시작하는 머리 낱말(앞 글자와 낱말 간격 이상 떨어진 글자)
    cuts = []
    for s in starts:
        k = next((i for i, c in enumerate(ink) if abs(c["x0"] - s) <= _TAB_TOL
                  and (i == 0 or c["x0"] - ink[i - 1]["x1"] > _SPACE_GAP * c["size"])), None)
        if k is None:
            return None
        cuts.append(k)
    if cuts[0] != 0:
        return None
    head = []
    for a, b in zip(cuts, cuts[1:] + [len(ink)]):
        part = ink[a:b]
        text = "".join(c["text"] if i == 0 or c["x0"] - part[i - 1]["x1"] <= _SPACE_GAP * c["size"] else " " + c["text"]
                       for i, c in enumerate(part))
        head.append({"bbox": [part[0]["x0"], min(c["top"] for c in part), part[-1]["x1"], max(c["bottom"] for c in part)],
                     "text": text})
    return [head] + body


def trailing_row(block: TextBlock) -> Optional[Tuple[TextBlock, List[Dict[str, Any]]]]:
    """문단 끝에 붙어 묶인 표 머리행(표 제목 바로 아래 머리): 마지막 높이의 줄들이 옆으로 늘어선 칸이면 (앞 줄들, 칸)."""
    lines = sorted((l for l in block.lines if l.text), key=lambda l: (l.center, l.x0))
    last = [l for l in lines if abs(l.center - lines[-1].center) <= (l.bottom - l.top) * _BAND]
    head = [l for l in lines if l not in last]
    if not head or len(last) < MIN_CELLS:
        return None
    cells = line_cells(TextBlock(last))
    return (TextBlock(head), cells) if cells else None


_PAGE_REF = re.compile(r"(\d{1,4}|[ivxlcdm]{1,7})", re.I)
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _contents(grid: List[List[str]]) -> bool:
    """차례: 끝 칸이 모두 쪽 번호이고 위에서 아래로 줄지 않는다(로마 숫자 쪽 다음 아라비아 숫자 쪽)."""
    keys = []
    for row in grid:
        m = _PAGE_REF.fullmatch(row[-1].strip()) if row else None
        if not m:
            return False
        t = m.group(1).lower()
        if t.isdigit():
            keys.append((1, int(t)))
        else:
            v = [_ROMAN[c] for c in t]
            keys.append((0, sum(-a if a < b else a for a, b in zip(v, v[1:] + [0]))))
    return all(a <= b for a, b in zip(keys, keys[1:]))


_LIST_MARK = re.compile(r"[^\w\s]|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)]")


def _list(grid: List[List[str]]) -> bool:
    """글머리 목록: 모든 행의 첫 칸이 글머리 기호(•, -, 1., a))뿐이다."""
    return all(row and _LIST_MARK.fullmatch(row[0].strip()) for row in grid)


def _row_of(cells):
    top = min(c["bbox"][1] for c in cells)
    bottom = max(c["bbox"][3] for c in cells)
    return {"cells": cells, "top": top, "bottom": bottom, "center": (top + bottom) / 2,
            "height": median(c["bbox"][3] - c["bbox"][1] for c in cells) or 1.0}


def _same_row(row, cells) -> bool:
    nxt = _row_of(cells)
    return abs(nxt["center"] - row["center"]) <= row["height"] * _BAND and \
        cells[0]["bbox"][0] - row["cells"][-1]["bbox"][2] >= row["height"] * _CELL_GAP


def _row_slots(run, row, columns) -> Optional[List[int]]:
    """다음 행의 칸마다 들어갈 열. 열 수가 같거나, 빈 칸이 있어 칸이 적으면 칸마다 겹치는 열이 하나씩(첫 칸은 첫 열)."""
    last = run[-1]
    if len(row["cells"]) > len(columns) or len(row["cells"]) < MIN_CELLS:
        return None
    if not (last["bottom"] - 1 <= row["top"] <= last["bottom"] + last["height"] * _ROW_GAP):
        return None
    slack = last["height"] * _BAND
    hits = lambda c, col: c["bbox"][0] <= col[1] + slack and c["bbox"][2] >= col[0] - slack
    if len(row["cells"]) == len(columns):
        return list(range(len(columns))) if all(hits(c, col) for c, col in zip(row["cells"], columns)) else None
    slots = []
    for c in row["cells"]:
        ks = [k for k, col in enumerate(columns) if hits(c, col)]
        if len(ks) != 1 or (slots and ks[0] <= slots[-1]):
            return None
        slots.append(ks[0])
    return slots if slots[0] == 0 else None


def _alnum(text: str) -> List[str]:
    return sorted(c for c in text if c.isalnum())


def rebuild_unruled(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """entries: 글 순서의 [{"key", "text", "bbox", "cells"}]. 표로 볼 행 묶음을 {"grid": ...} 항목 하나로 바꾼다."""
    units: List[Dict[str, Any]] = []
    for i, e in enumerate(entries):
        cells = e.get("cells")
        last = units[-1] if units else None
        if cells and last and last["row"] and _same_row(last["row"], cells):
            last["idx"].append(i)
            last["row"] = _row_of(last["row"]["cells"] + cells)
        else:
            units.append({"idx": [i], "row": _row_of(cells) if cells else None})

    out: List[Dict[str, Any]] = []
    u = 0
    while u < len(units):
        row = units[u]["row"]
        run = [row] if row and len(row["cells"]) >= MIN_CELLS else []
        columns = [[c["bbox"][0], c["bbox"][2]] for c in row["cells"]] if run else []
        slots = [list(range(len(columns)))] if run else []
        v = u + 1
        while run and v < len(units) and units[v]["row"]:
            nxt = units[v]["row"]
            got = _row_slots(run, nxt, columns)
            if got is None:
                break
            for k, c in zip(got, nxt["cells"]):
                columns[k] = [min(columns[k][0], c["bbox"][0]), max(columns[k][1], c["bbox"][2])]
            run.append(nxt)
            slots.append(got)
            v += 1
        # 탭 위치로 머리를 가른 행 묶음은 값 행이 하나여도 표다
        tabbed = all(entries[i].get("tab") for w in units[u:v] for i in w["idx"])
        if len(run) >= (2 if tabbed else MIN_ROWS):
            first = u
            # 바로 위의 같은 칸 수 행은 머리행이다. 머리 글은 가운데·숫자는 오른쪽 정렬이라 칸 위치로는 안 맞는다
            prev = units[u - 1] if u else None
            if prev and prev["row"] and len(prev["row"]["cells"]) == len(run[0]["cells"]) \
                    and out[-len(prev["idx"]):] == [entries[i] for i in prev["idx"]] \
                    and 0 <= run[0]["top"] - prev["row"]["bottom"] + 1 <= prev["row"]["height"] * _ROW_GAP + 1:
                del out[-len(prev["idx"]):]
                run.insert(0, prev["row"])
                slots.insert(0, list(range(len(columns))))
                first = u - 1
            idx = [i for w in units[first:v] for i in w["idx"]]
            grid = []
            for r, ks in zip(run, slots):
                cells = [""] * len(columns)
                for k, c in zip(ks, r["cells"]):
                    cells[k] = c["text"]
                grid.append(cells)
            if _alnum(" ".join(t for r in grid for t in r)) != _alnum(" ".join(entries[i]["text"] for i in idx)) \
                    or _contents(grid) or _list(grid):
                # 표가 원래 블록의 글을 그대로 담지 못하면 바꾸지 않는다(글을 잃는 것보다 표를 놓치는 게 낫다)
                out.extend(entries[i] for i in idx)
                u = v
                continue
            boxes = [entries[i]["bbox"] for i in idx]
            out.append({"key": entries[idx[0]]["key"], "text": "", "grid": grid, "cells": None, "columns": columns,
                        "pitch": run[-1]["top"] - run[-2]["top"], "height": run[-1]["height"],
                        "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                 max(b[2] for b in boxes), max(b[3] for b in boxes)]})
            u = v
        else:
            out.extend(entries[i] for i in units[u]["idx"])
            u += 1
    return out
