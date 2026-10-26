"""PDF — pdfplumber(pdfminer.six)로 글자·표 선을, pypdfium2 로 그림 영역을 렌더한다.

읽기 순서는 PDF 에 기록된 글 순서를 따르고, 표·그림은 그 순서 안의 제자리에 끼운다.
쪽은 셋으로 나뉜다: 글만 / 글 레이어 없음(쪽 전체 OCR) / 글 + 그림(그림 영역을 묶어 영역마다 한 장).
"""
from __future__ import annotations

import bisect
import io
import math
import re
import unicodedata
from collections import Counter
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..errors import EncryptedDocumentError, ParseError
from ..images import Ocr, run_parallel
from ..model import Block, Cell, Document, Image, Table
from . import pdf_layout as layout
from .pdf_subrows import split_subrows

# 머리말·꼬리말 후보: 쪽 높이의 위·아래 이 비율 안에 있는 글
_MARGIN_BAND = 0.1
# 그림 영역 렌더 해상도
_RENDER_DPI = 200
# 두 그림이 쪽 짧은 변의 이 비율 이내로 붙어 있으면 한 영역
_MERGE_GAP_FRAC = 0.012
# 쪽의 이 비율보다 작은 영역은 아이콘·장식
_MIN_REGION_AREA_FRAC = 0.01
# 쪽의 이 비율 이상을 덮는 영역은 설명 대신 받아쓴다
_REGION_OCR_COVERAGE = 0.5
# 글이 있는 쪽에서 이 비율 이상을 덮는 그림은 배경·워터마크
_SKIP_BG_COVERAGE = 0.95
# 같은 자리 그림이 문서 쪽의 이 비율 이상에 나오면 로고·레터헤드
_BOILERPLATE_PAGE_RATIO = 0.5
_BOILERPLATE_MIN_PAGES = 3
_BOILERPLATE_MIN_DOC_PAGES = 5

Entry = Dict[str, Any]  # {"key", "text" | "table" | "image", "bbox"}

# 점선 괘선: 이보다 짧은 선분이 이 간격 안으로 이 개수 이상 한 축에 늘어서면 선 하나로 잇는다.
# pdfplumber 는 1pt 미만 선분을 잇기 전에 버려서 점선 칸 경계가 사라진다
_DOT_MAX_LEN, _DOT_MAX_GAP, _DOT_MIN_COUNT = 1.0, 3.0, 5
# 숨은 칸 경계: 공백 글자 없는 간격이 글자 크기의 이 배 이상이고, 같은 열에서 이만큼의 행이 겹치면
_HIDDEN_GAP, _HIDDEN_MIN_ROWS = 1.0, 3
# 칸 글: 공백 글자 없이 이 폭(글자 크기 대비)보다 벌어진 자리는 띄어쓴다.
# 단 양쪽이 한 글자짜리 글자면 균등 배분("투 자 액" → "투자액", "S S M")이라 붙인다
_CELL_WORD_GAP = 0.2
# 가로선만 있는 표: 이 폭(쪽 폭 대비) 이상인 같은 폭 가로선끼리 묶는다(2단 논문의 한 단 표까지)
_RULE_MIN_WIDTH = 0.2
# 덩어리 둘이 모두 표 폭의 이 비율보다 길면 표 줄이 아니라 2단 본문 한 줄
_TWO_COLUMN_PROSE = 0.35
# 공백 글자가 글자 수의 이 비율 이상이면 띄어쓰기를 글자로 담는 PDF
_SPACE_CHAR_RATIO = 0.05


# ── 쪽 가장자리 반복 글 ───────────────────────────────────────────────────────

def _margin_key(bbox: List[float], text: str, page_h: float) -> Optional[str]:
    if bbox[3] > page_h * _MARGIN_BAND and bbox[1] < page_h * (1 - _MARGIN_BAND):
        return None
    return re.sub(r"\d+", "#", " ".join(text.split())) or None


def _repeated_margins(pages_blocks: List[Tuple[float, List[layout.TextBlock]]]) -> Set[str]:
    total = len(pages_blocks)
    if total < 3:
        return set()
    seen: Dict[str, Set[int]] = {}
    for pno, (h, blocks) in enumerate(pages_blocks):
        for b in blocks:
            key = _margin_key(b.bbox, b.text, h)
            if key:
                seen.setdefault(key, set()).add(pno)
    need = max(3, total // 2)
    return {k for k, ps in seen.items() if len(ps) >= need}


# ── 깨진 글 레이어 ────────────────────────────────────────────────────────────

_KNOWN_SCRIPTS = ("HANGUL", "LATIN", "CJK UNIFIED", "CJK COMPATIBILITY IDEOGRAPH", "HIRAGANA", "KATAKANA",
                  "GREEK", "CYRILLIC")


def garbled_text(text: str) -> bool:
    """글꼴 대응표가 없는 PDF 처럼 글자가 엉뚱한 문자로 나오는가.

    깨진 글은 여러 문자 체계로 흩어지고 흔한 문자 체계가 적다. 둘 다일 때만 깨진 것으로 본다.
    """
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if len(letters) < 30:
        return False
    names = [unicodedata.name(c, "?") for c in letters]
    known = sum(n.startswith(_KNOWN_SCRIPTS) for n in names) / len(letters)
    scripts = Counter(n.split(" ")[0] for n in names)
    top2 = sum(v for _, v in scripts.most_common(2)) / len(letters)
    return known < 0.5 and top2 < 0.8


# ── 표 ────────────────────────────────────────────────────────────────────────

def _snap(values: List[float], v: float) -> int:
    k = bisect.bisect_left(values, v)
    if k == len(values) or (k > 0 and v - values[k - 1] < values[k] - v):
        k -= 1
    return k


def _edges(vals: List[float], tol: float = 1.0) -> List[float]:
    out: List[float] = []
    for v in sorted(vals):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def encodes_spaces(chars) -> bool:
    """띄어쓰기를 공백 글자로 담는 PDF(한컴·워드)인가. 그런 PDF 에서 공백 없이 벌어진 간격은 균등 배분이다."""
    words = sum(1 for c in chars if c.get("text", "").strip())
    spaces = sum(1 for c in chars if c.get("text") == " ")
    return words > 0 and spaces >= _SPACE_CHAR_RATIO * words


def _cell_text(chars, bbox, size: float) -> str:
    """칸 글. 공백 글자는 그대로, 공백 없이 벌어진 간격은 균등 배분(한 글자씩 떨어진 글자)이 아니면 띄운다."""
    x0, top, x1, bottom = bbox
    inside = sorted((c for c in chars if x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom),
                    key=lambda c: (round(c["top"]), c["x0"]))
    out = []
    for line in _char_lines(inside):
        # 본문보다 작은 글자로 짠 칸은 그 줄 글자 크기로 낱말 간격을 잰다
        sizes = sorted(c["size"] for c in line if c["text"].strip())
        word_gap = _CELL_WORD_GAP * (min(size, sizes[len(sizes) // 2]) if sizes else size)
        tokens: List[List[str]] = []  # [글, 앞 구분: "" | " " | "gap"]
        space, prev = False, None
        for c in line:
            if not c["text"].strip():
                space = True
                continue
            if prev is None:
                tokens.append([c["text"], ""])
            elif space:
                tokens.append([c["text"], " "])
            elif c["x0"] - prev["x1"] > word_gap:
                tokens.append([c["text"], "gap"])
            else:
                tokens[-1][0] += c["text"]
            prev, space = c, False
        text = ""
        for k, (tok, sep) in enumerate(tokens):
            prev_tok = tokens[k - 1][0] if k else ""
            # 균등 배분 글자끼리, 또는 기호 하나(-·) 옆의 간격은 띄어쓰기가 아니다
            spread = sep == "gap" and ((_spread_letter(tok) and _spread_letter(prev_tok))
                                       or _lone_mark(prev_tok) or _lone_mark(tok))
            text += ("" if not sep or spread else " ") + tok
        if text:
            out.append(text)
    return "\n".join(out)


def _plumber_table(t, spaced: bool = False, size: float = 10.0) -> Optional[Table]:
    chars = [c for c in t.page.chars if c.get("text")]
    if not any(t.cells):
        return None
    x0, top, x1, bottom = t.bbox
    inside = [c for c in chars if c["text"].strip() and x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom]
    if inside and sum(not c.get("upright", True) for c in inside) * 2 > len(inside):
        return None  # 글자 대부분이 누운 격자는 그래프 축 눈금(눕혀 쓴 날짜)이다
    near = layout.center_bins(chars)
    rows = [[None if b is None else _cell_text(near.around(b[1], b[3]), b, size) for b in r.cells] for r in t.rows]
    rows = [[_unstack(c) for c in r] for r in rows]
    texts = [c for r in rows for c in r if c and c.strip()]
    n_cols = max((len(r) for r in rows), default=0)
    # 두 칸 이상에 글이 없는 '표'는 선·글자 도형을 표로 오인한 것이다. 한 행짜리는 받는다(행마다 따로 잡는 PDF).
    # 한 열은 칸마다 선으로 막은 세 행 이상이 모두 글을 담을 때만(칸 친 목록)
    one_column = n_cols == 1 and len(rows) >= 3 and len(texts) == len(rows) and _ruled_rows(t)
    if (n_cols < 2 and not one_column) or len(texts) < 2:
        return None
    boxes = [(r, c, bbox) for r, row in enumerate(t.rows) for c, bbox in enumerate(row.cells) if bbox]
    # 글 없는 글자 폭 남짓의 가는 칸(칸 배경과 선이 조금 어긋난 자리)은 같은 행 옆 칸에 붙인다.
    # 다른 행도 쓰는 경계에 붙은 빈 칸(들여쓰기 열)은 진짜 칸이다
    def shared(x, row):
        return any(r != row and (abs(b[0] - x) < 0.5 or abs(b[2] - x) < 0.5) for r, _, b in boxes)

    boxes = [list(x) for x in boxes]
    for item in list(boxes):
        r, c, s = item
        if s[2] - s[0] >= 1.5 * size or (rows[r][c] or "").strip():
            continue
        same = lambda b: abs(b[1] - s[1]) < 0.5 and abs(b[3] - s[3]) < 0.5
        left = next((x for x in boxes if x is not item and same(x[2]) and abs(x[2][2] - s[0]) < 0.5), None)
        right = next((x for x in boxes if x is not item and same(x[2]) and abs(x[2][0] - s[2]) < 0.5), None)
        if left and not shared(s[0], r):
            left[2] = (left[2][0], left[2][1], s[2], left[2][3])
            boxes.remove(item)
        elif right and not shared(s[2], r):
            right[2] = (s[0], right[2][1], right[2][2], right[2][3])
            boxes.remove(item)
    boxes = [tuple(x) for x in boxes]
    xs = _edges([b[0] for _, _, b in boxes] + [b[2] for _, _, b in boxes])
    ys = _edges([b[1] for _, _, b in boxes] + [b[3] for _, _, b in boxes])
    cells = []
    for r, c, (x0, top, x1, bottom) in boxes:
        col, row = _snap(xs, x0), _snap(ys, top)
        text = (rows[r][c] if r < len(rows) and c < len(rows[r]) else "") or ""
        row_span, col_span = max(_snap(ys, bottom) - row, 1), max(_snap(xs, x1) - col, 1)
        parts = _spread(t.page, (x0, top, x1, bottom), xs[col:col + col_span + 1], size) if col_span > 1 else None
        if parts:
            cells += [Cell(p, row, col + k, row_span, 1) for k, p in enumerate(parts)]
        else:
            cells.append(Cell(text.strip(), row, col, row_span, col_span))
    return Table(cells)


def _ruled_rows(t) -> bool:
    """행 사이마다 보이는 가는 선(선, 테두리, 2pt 이하 채움)이 표 폭 대부분을 가로지른다. 같은 색 줄 음영이 맞붙은 글 상자는 아니다."""
    x0, _, x1, _ = t.bbox
    thin = [o for o in t.page.lines + t.page.rects
            if o["bottom"] - o["top"] <= 2 or (o.get("stroke") and o in t.page.rects)]
    for r in t.rows[1:]:
        y = min(c[1] for c in r.cells if c)
        span = sum(max(0.0, min(o["x1"], x1) - max(o["x0"], x0)) for o in thin
                   if abs(o["top"] - y) <= 1.5 or abs(o["bottom"] - y) <= 1.5)
        if span < 0.8 * (x1 - x0):
            return False
    return True


_CAPTION = re.compile(r"(figure|fig\.|diagram|chart|graph|picture|photo|image|exhibit|그림|사진|도표|차트)\s*[\dIVX]", re.I)


_PAGE_NO = re.compile(r"\d{1,3}|[ivxlcdm]{1,7}", re.I)


def _not_a_table(table: Table) -> bool:
    """틀 안의 그림 제목 한 줄(Figure 6 | 제목), 막대그래프 위 숫자처럼 이름 칸 없이 숫자만 띄엄띄엄 든 격자, 선 친 차례."""
    grid = table.grid(fill_merged=False)
    texts = [c.strip() for r in grid for c in r if c and c.strip()]
    if table.n_rows == 1 and texts and _CAPTION.match(texts[0]):
        return True
    cells = sum(len(r) for r in grid)
    if not any(ch.isalpha() for t in texts for ch in t) and len(texts) * 2 <= cells:
        return True
    # 차례: 끝 열이 모두 쪽 번호(세 자리까지)이고 줄지 않으며, 앞 열은 글. 칸 하나에 줄로 쌓였어도 줄마다 본다
    last = [l.strip() for r in grid if r and r[-1] for l in r[-1].split("\n") if l.strip()]
    heads = [r[0] for r in grid if r and r[0].strip()]
    return len(grid[0]) <= 3 and len(last) >= 3 and all(_PAGE_NO.fullmatch(v) for v in last) \
        and all(any(ch.isalpha() for ch in h) for h in heads) and layout._contents([[v] for v in last])


def _spread(page, bbox, bounds: List[float], size: float) -> Optional[List[str]]:
    """여러 열에 걸친 칸의 글이 공백 글자 없는 간격으로 열 수만큼 갈리고 덩어리마다 제 열 안에 들면 열마다 나눈다.

    선이 한 행에서만 빠진 표(그 행만 값 셋이 한 칸)를 다른 행의 열에 맞춘다.
    """
    x0, top, x1, bottom = bbox
    inside = sorted((c for c in page.chars if c.get("text") and x0 <= (c["x0"] + c["x1"]) / 2 <= x1
                     and top <= (c["top"] + c["bottom"]) / 2 <= bottom), key=lambda c: (round(c["top"]), c["x0"]))
    n = len(bounds) - 1
    out = [[] for _ in range(n)]
    lines = _char_lines(inside)
    if not lines:
        return None
    edges = [[] for _ in range(n)]
    wordy = False
    for line in lines:
        frags = _fragments(line, _HIDDEN_GAP * size)
        if len(frags) != n:
            return None
        for k, (a, b, text) in enumerate(frags):
            inside = bounds[k] - 1 <= a and b <= bounds[k + 1] + 1
            wordy = wordy or not inside  # 선 없는 머리칸 글은 열 경계를 조금 넘기도 한다
            if not bounds[k] <= (a + b) / 2 <= bounds[k + 1]:
                return None
            wordy = wordy or " " in text.strip() or len(text.strip()) > 15
            edges[k].append((a, (a + b) / 2, b))
            out[k].append(text.strip())
    # 띄어쓴 덩어리는 여러 줄이 열마다 같은 자리에 맞춰졌을 때만(머리칸). 양쪽 정렬로 벌어진 산문 줄은 나누지 않는다
    if wordy and (len(lines) < 2 or not all(any(max(e[i] for e in es) - min(e[i] for e in es) <= 2 for i in range(3))
                                            for es in edges)):
        return None
    return ["\n".join(p) for p in out]


def _unstack(text: Optional[str]) -> Optional[str]:
    """좁은 칸에 한글을 한 글자씩 세로로 쓴 글은 붙인다(숫자 하위 행은 그대로)."""
    if not text or "\n" not in text:
        return text
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return "".join(lines) if len(lines) > 1 and all(len(l) == 1 and "가" <= l <= "힣" for l in lines) else text


_MONO_NUM = re.compile(r"[-−–+$(]*\d[\d.,]*[%)]*")


def _monospace_tables(page) -> List[Dict[str, Any]]:
    """고정폭 글자로 친 표(공백 글자로 칸을 맞춘 타자기식). [{"bbox", "rows"}].

    고정폭 줄에 숫자 덩어리가 셋 이상이고 그 오른쪽 끝이 세 줄 넘게 같은 자리(글자 반 칸 안)에 오면 숫자 열이다.
    숫자 열 왼쪽 글은 행 이름. "-----" 같은 구분 줄은 행 사이에 끼어도 된다. 숫자 열에 안 맞는 숫자가 많으면 표가 아니다.
    """
    chars = sorted((c for c in page.chars if c.get("text") and c.get("upright", True)), key=lambda c: (round(c["top"]), c["x0"]))
    lines: List[List[dict]] = []
    for c in chars:
        if lines and abs(c["top"] - lines[-1][0]["top"]) <= 0.5 * c["size"]:
            lines[-1].append(c)
        else:
            lines.append([c])
    parsed = []  # (줄 글자들, 덩어리[(x0, x1, 글)], 고정폭 숫자 줄인가, 구분 줄인가)
    for ln in lines:
        ln.sort(key=lambda c: c["x0"])
        ink = [c for c in ln if not c["text"].isspace()]
        widths = [c["x1"] - c["x0"] for c in ink]
        mono = len(ink) >= 6 and min(widths) > 0 and max(widths) <= 1.15 * min(widths)
        toks: List[list] = []
        for c in ln:
            if c["text"].isspace():
                if toks and toks[-1][2]:
                    toks.append([0, 0, ""])
                continue
            if toks and toks[-1][2] and c["x0"] - toks[-1][1] <= 0.3 * (c["x1"] - c["x0"]):
                toks[-1][1], toks[-1][2] = c["x1"], toks[-1][2] + c["text"]
            else:
                if toks and not toks[-1][2]:
                    toks.pop()
                toks.append([c["x0"], c["x1"], c["text"]])
        toks = [t for t in toks if t[2]]
        k = len(toks)
        while k and _MONO_NUM.fullmatch(toks[k - 1][2]):
            k -= 1
        if k == 0 and toks:  # 숫자만 있는 줄도 첫 덩어리는 행 이름(0.85)
            k = 1
        nums = toks[k:]  # 줄 끝에 이어진 숫자 덩어리가 값, 그 앞(이름·점 채움)은 행 이름
        rule = bool(toks) and all(set(t[2]) <= set("-=_") for t in toks)
        parsed.append((ink, (toks[:k], nums), mono and len(nums) >= 3, rule))
    out = []
    i = 0
    while i < len(parsed):
        if not parsed[i][2]:
            i += 1
            continue
        j, run = i, []
        while j < len(parsed) and (parsed[j][2] or (parsed[j][3] and run)):
            if parsed[j][2]:
                run.append(parsed[j])
            j += 1
        i = j
        if len(run) < 3:
            continue
        cw = median(c["x1"] - c["x0"] for r in run for c in r[0])
        ends = sorted(t[1] for r in run for t in r[1][1])
        cols: List[List[float]] = []
        for e in ends:
            if cols and e - cols[-1][-1] <= 0.5 * cw:
                cols[-1].append(e)
            else:
                cols.append([e])
        anchors = [sum(c) / len(c) for c in cols
                   if sum(any(abs(t[1] - x) <= 0.5 * cw for t in r[1][1] for x in c) for r in run) >= 0.6 * len(run)]
        if len(anchors) < 3:
            continue
        rows, missed, total = [], 0, 0
        for ink, (label, nums), _, _ in run:
            row = [" ".join(t[2] for t in label)] + [""] * len(anchors)
            for t in nums:
                k = min(range(len(anchors)), key=lambda k: abs(t[1] - anchors[k]))
                total += 1
                missed += abs(t[1] - anchors[k]) > 0.5 * cw  # 머리행 숫자처럼 끝이 어긋나도 가장 가까운 열에 넣는다(글을 잃지 않게)
                row[k + 1] = f"{row[k + 1]} {t[2]}".strip()
            rows.append(row)
        if missed > 0.1 * total:
            continue
        ink = [c for r in run for c in r[0]]
        out.append({"bbox": [min(c["x0"] for c in ink), min(c["top"] for c in ink), max(c["x1"] for c in ink),
                             max(c["bottom"] for c in ink)], "rows": rows})
    return out


def _grid_table(grid: List[List[str]]) -> Table:
    return Table([Cell(text, r, c) for r, row in enumerate(grid) for c, text in enumerate(row)])


def _center_inside(b, box, tol: float = 1.0) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return box[0] - tol <= cx <= box[2] + tol and box[1] - tol <= cy <= box[3] + tol


def _x_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2]


def _spans(items: List[Entry], lo: int, hi: int) -> List[List[Entry]]:
    """bbox 의 [lo, hi] 축에서 겹치는 항목끼리 묶어 앞에서부터. 0.5pt 넘게 겹쳐야 같은 묶음이다."""
    out: List[List[Entry]] = []
    end = None
    for e in sorted(items, key=lambda e: e["bbox"][lo]):
        if out and e["bbox"][lo] < end - 0.5:
            out[-1].append(e)
            end = max(end, e["bbox"][hi])
        else:
            out.append([e])
            end = e["bbox"][hi]
    return out


def _gutters(band: List[Entry]) -> List[Tuple[float, float]]:
    cols = _spans(band, 0, 2)
    return [(max(e["bbox"][2] for e in a), min(e["bbox"][0] for e in b)) for a, b in zip(cols, cols[1:])]


def _narrow(gap: Tuple[float, float], band: List[Entry]) -> Optional[Tuple[float, float]]:
    """단 사이 틈을 띠의 블록으로 좁힌다(틈 가운데 왼쪽 블록은 오른 끝, 오른쪽 블록은 왼 끝까지). 막히면 None."""
    lo, hi = gap
    mid = (lo + hi) / 2
    for e in band:
        if (e["bbox"][0] + e["bbox"][2]) / 2 < mid:
            lo = max(lo, e["bbox"][2])
        else:
            hi = min(hi, e["bbox"][0])
    return (lo, hi) if hi > lo - 0.5 else None


def _reading_order(items: List[Entry]) -> List[Entry]:
    """위치로 읽는 순서(XY-cut). 디자인 도구 PDF 는 그리기 순서가 꼬리말·옆 단부터인 일이 흔하다.

    가로 띠로 자르고, 이어지는 띠들이 아무 블록도 건너지 않는 단 사이 틈을 함께 가지면 한 묶음으로 보아 단(왼쪽부터)으로
    읽는다. 한 줄짜리 칸만 늘어선 띠(이름 | 값 양식)는 행으로 읽는다. 어느 쪽으로도 안 갈리면 그리기 순서 그대로.
    """
    if len(items) <= 1:
        return sorted(items, key=lambda e: e["key"])
    bands = _spans(items, 1, 3)
    if len(bands) == 1:
        cols = _spans(items, 0, 2)
        if len(cols) == 1:
            return sorted(items, key=lambda e: e["key"])
        return [e for c in cols for e in _reading_order(c)]
    # 한 줄짜리 블록 여럿이 늘어선 띠(머리말 줄, 이름 | 값)는 행이다. 위아래 띠와 단으로 묶지 않는다
    # 긴 글 줄만 늘어선 띠는 행이 아니라 두 단 본문(줄 간격이 넓어 줄마다 블록이 된 각주)이다
    row = lambda band: len(band) >= 2 and not any("table" in e or "\n" in (e.get("text") or "") for e in band) \
        and not all(len(e.get("text") or "") > 20 for e in band)
    heights = sorted((e["bbox"][3] - e["bbox"][1]) / ((e.get("text") or "").count("\n") + 1) for e in items if e.get("text"))
    line_h = heights[len(heights) // 2] if heights else 10.0
    groups: List[Tuple[List[Entry], List[Tuple[float, float]], bool]] = []
    for k, band in enumerate(bands):
        if groups and not groups[-1][2] and not row(band):
            group, gaps, _ = groups[-1]
            # 묶음 가로 범위 밖에만 있는 띠(옆 여백 쪽 번호), 묶음 바닥에서 두 줄 넘게 떨어진 마지막 블록(각주·꼬리말)은
            # 단의 일부가 아니다
            gx0, gx1 = min(e["bbox"][0] for e in group), max(e["bbox"][2] for e in group)
            apart = all(e["bbox"][2] <= gx0 or e["bbox"][0] >= gx1 for e in band) or (
                k == len(bands) - 1 and len(band) == 1
                and band[0]["bbox"][1] > max(e["bbox"][3] for e in group) + 2 * line_h)
            if apart:
                groups.append((band, _gutters(band), row(band)))
                continue
            # 한 단뿐인 묶음은 다음 띠의 단 틈을 막지 않으면 그 단의 윗부분이다(단마다 시작 높이가 다르다)
            keep = [g for g in (_narrow(g, band) for g in gaps) if g] if gaps else \
                [g for g in (_narrow(g, group) for g in _gutters(band)) if g]
            if keep:
                groups[-1] = (group + band, keep, False)
                continue
        groups.append((band, _gutters(band), row(band)))
    if len(groups) == 1:
        cols = _spans(items, 0, 2)
        if len(cols) == 1:
            return sorted(items, key=lambda e: e["key"])
        return [e for c in cols for e in _reading_order(c)]
    return [e for g, _, _ in groups for e in _reading_order(g)]


def place_key(entries: List[Entry], bbox) -> float:
    """글 순서 목록 안에서 표·그림이 들어갈 자리(정렬 키).

    같은 단(가로로 겹치는)에서 바로 위 블록 뒤, 없으면 바로 아래 블록 앞, 둘 다 없으면 세로 위치로.
    """
    above = [e for e in entries if e["bbox"][3] <= bbox[1] + 2 and _x_overlap(e["bbox"], bbox)]
    if above:
        return max(above, key=lambda e: e["bbox"][3])["key"] + 0.5 + bbox[1] / 1e6
    below = [e for e in entries if e["bbox"][1] >= bbox[3] - 2 and _x_overlap(e["bbox"], bbox)]
    if below:
        return min(below, key=lambda e: e["bbox"][1])["key"] - 0.5 + bbox[1] / 1e6
    later = [e["key"] for e in entries if e["bbox"][1] > bbox[1]]
    return (min(later) if later else len(entries)) - 0.5 + bbox[1] / 1e6


def carry_table_header(entries: List[Entry], prev: Optional[Table]) -> Tuple[List[Entry], Optional[Table]]:
    """쪽을 넘긴 표에 앞 쪽 표의 머리행을 다시 붙인다. (entries, 이 쪽이 표로 끝나면 그 표)를 돌려준다.

    앞 쪽이 표로 끝나고 이 쪽이 열 수가 같은 표로 시작하면 이어지는 표로 본다. 머리행을 반복하지 않은
    표는 둘째 쪽 첫 행이 머리 자리에 들어가 열 이름을 잃는다.
    """
    ordered = sorted(entries, key=lambda e: e["key"])
    first = ordered[0].get("table") if ordered else None
    if first is not None and prev is not None and first.n_cols == prev.n_cols:
        head, cur = prev.grid()[:1], first.grid()[:1]
        if head and cur and head != cur:
            cells = [Cell(c.text, 0, c.col, 1, c.col_span) for c in prev.cells if c.row == 0]
            cells += [Cell(c.text, c.row + 1, c.col, c.row_span, c.col_span) for c in first.cells]
            ordered[0] = dict(ordered[0], table=Table(cells))
    last = ordered[-1].get("table") if ordered else None
    return ordered, last


# ── 그림 영역 ─────────────────────────────────────────────────────────────────

def _signature(bbox) -> Tuple[int, ...]:
    """같은 자리 판정용: 2pt 격자로 양자화한 위치·크기."""
    return tuple(int(round(c / 2.0)) for c in bbox)


def _image_rects(page) -> List[List[float]]:
    out = []
    for im in page.images:
        x0, top, x1, bottom = float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"])
        if x1 - x0 > 1 and bottom - top > 1:
            out.append([x0, top, x1, bottom])
    return out


def _merge_boxes(boxes: List[List[float]], gap: float) -> List[List[float]]:
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(boxes):
            j = i + 1
            while j < len(boxes):
                a, b = boxes[i], boxes[j]
                if not (a[2] + gap < b[0] or a[0] - gap > b[2] or a[3] + gap < b[1] or a[1] - gap > b[3]):
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    boxes.pop(j)
                    changed = True
                else:
                    j += 1
            i += 1
    return boxes


def _figure_regions(rects: List[List[float]], page_w: float, page_h: float) -> List[Dict[str, Any]]:
    if not rects:
        return []
    area = (page_w * page_h) or 1.0
    out = []
    for b in _merge_boxes(rects, _MERGE_GAP_FRAC * min(page_w, page_h)):
        frac = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1]) / area
        if _MIN_REGION_AREA_FRAC <= frac < _SKIP_BG_COVERAGE:
            out.append({"bbox": b, "coverage": frac})
    return sorted(out, key=lambda r: r["bbox"][1])


class _Renderer:
    def __init__(self, data: bytes):
        import pypdfium2 as pdfium

        self.doc = pdfium.PdfDocument(data)

    def png(self, page_no: int, bbox: Optional[List[float]] = None, dpi: int = _RENDER_DPI) -> Optional[bytes]:
        try:
            page = self.doc[page_no]
            w, h = page.get_size()
            crop = (0, 0, 0, 0)
            if bbox:
                x0, top, x1, bottom = (max(0.0, bbox[0]), max(0.0, bbox[1]), min(w, bbox[2]), min(h, bbox[3]))
                crop = (x0, h - bottom, w - x1, top)
            img = page.render(scale=dpi / 72.0, crop=crop).to_pil()
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            return None

    def close(self) -> None:
        self.doc.close()


# ── 쪽 조립 ───────────────────────────────────────────────────────────────────

def _floating_shading(page) -> Set[Tuple[float, float, float, float]]:
    """칸 경계가 아닌 채움 사각형(글자 뒤 음영). 표 선이 아니다.

    - 다른 선·사각형과 맞닿지 않고 칸 안에 떠 있는 것.
    - 더 큰 채움 사각형 안에 좌우 또는 위아래로 들여 그린 것, 같은 색으로 안에 그린 것(Word 가 칸 배경 안에 줄마다 그리는 문단 음영).
      줄 음영끼리는 위아래로 맞붙어 칸 배경처럼 보인다.
    배경색 칸끼리 맞붙어 칸 경계를 이루는 표도 있어, 맞닿은 채움 사각형은 남긴다. 바깥 테두리 없는 줄무늬 행처럼
    한쪽만 선에 닿아도 같은 높이의 채움 칸과 변을 나눠 붙어 있으면 칸 배경이다.
    """
    def key(o):
        return (o["x0"], o["top"], o["x1"], o["bottom"])

    fills = [o for o in page.rects if o.get("fill") and not o.get("stroke")
             and min(o["x1"] - o["x0"], o["bottom"] - o["top"]) > 2.0]
    if not fills:
        return set()
    others = page.rects + page.lines
    tol = 1.0
    others_by_y = layout.Bins(others, lambda b: b["top"], lambda b: b["bottom"])

    opposite = {"x0": "x1", "x1": "x0", "top": "bottom", "bottom": "top"}

    def side(a, b, which: str) -> bool:
        """b 가 a 의 그 변 바깥에 맞붙은 이웃이거나 그 변 위의 선인가."""
        thin = min(b["x1"] - b["x0"], b["bottom"] - b["top"]) <= 0.5
        if which in ("x0", "x1"):
            if not (a["top"] < b["bottom"] - tol and b["top"] < a["bottom"] - tol):
                return False
        elif not (a["x0"] < b["x1"] - tol and b["x0"] < a["x1"] - tol):
            return False
        if thin:
            targets = (b["x0"], b["x1"]) if which in ("x0", "x1") else (b["top"], b["bottom"])
        else:
            targets = (b[opposite[which]],)
        return any(abs(a[which] - q) <= tol for q in targets)

    def inset(a, b) -> bool:  # a 가 더 큰 b 안에 좌우 또는 위아래로 들여 그려졌다
        if not (b["x0"] - tol <= a["x0"] and a["x1"] <= b["x1"] + tol and b["top"] - tol <= a["top"]
                and a["bottom"] <= b["bottom"] + tol):
            return False
        # 같은 색으로 안에 덧칠한 것은 어느 변에 붙었든 경계를 만들지 않는다
        smaller = (a["x1"] - a["x0"]) * (a["bottom"] - a["top"]) < (b["x1"] - b["x0"]) * (b["bottom"] - b["top"]) - tol
        if smaller and a.get("non_stroking_color") == b.get("non_stroking_color"):
            return True
        return (a["x0"] - b["x0"] > tol and b["x1"] - a["x1"] > tol) or (a["top"] - b["top"] > tol and b["bottom"] - a["bottom"] > tol)

    decor = {key(a) for a in fills if any(inset(a, b) for b in fills if key(b) != key(a))}
    cells = [a for a in fills if key(a) not in decor]

    def paired(a) -> bool:  # 세로 변 하나를 채움 칸들과 통째로 나눠 붙었다(줄무늬 행의 가장자리 칸, 두 행에 걸친 머리칸)
        for near in (lambda b: abs(a["x1"] - b["x0"]) <= tol, lambda b: abs(b["x1"] - a["x0"]) <= tol):
            spans = sorted((max(a["top"], b["top"]), min(a["bottom"], b["bottom"])) for b in cells
                           if key(b) != key(a) and near(b) and b["top"] < a["bottom"] - tol and a["top"] < b["bottom"] - tol)
            y = a["top"]
            for lo, hi in spans:
                if lo > y + tol:
                    break
                y = max(y, hi)
            if spans and y >= a["bottom"] - tol:
                return True
        return False

    def framed(a) -> bool:  # 좌우 또는 위아래가 모두 다른 선·사각형에 닿아야 칸 배경이다(같은 자리에 겹쳐 그린 사본은 빼고)
        near = [b for b in others_by_y.around(a["top"] - tol, a["bottom"] + tol) if b["x0"] <= a["x1"] + tol and b["x1"] >= a["x0"] - tol
                and b["top"] <= a["bottom"] + tol and b["bottom"] >= a["top"] - tol and key(b) != key(a)
                and key(b) not in decor]
        hit = {w: any(side(a, b, w) for b in near) for w in ("x0", "x1", "top", "bottom")}
        return (hit["x0"] and hit["x1"]) or (hit["top"] and hit["bottom"]) or paired(a)

    return decor | {key(a) for a in cells if not framed(a)}


def _dotted_lines(page) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """짧은 선분이 한 축에 촘촘히 늘어선 점선을 선 하나로."""
    out: Dict[str, List[Dict[str, Any]]] = {"v": [], "h": []}
    groups: Dict[Tuple[str, float], List[Tuple[float, float]]] = {}
    for o in page.lines:
        w, h = o["x1"] - o["x0"], o["bottom"] - o["top"]
        if w <= 0.5 and h <= 0.5:  # 길이 0인 점(둥근 끝으로 찍은 점선). 어느 축인지는 늘어선 모양이 정한다
            groups.setdefault(("v", round(o["x0"], 1)), []).append((o["top"], o["bottom"]))
            groups.setdefault(("h", round(o["top"], 1)), []).append((o["x0"], o["x1"]))
        elif w <= 0.5 and h < _DOT_MAX_LEN:
            groups.setdefault(("v", round(o["x0"], 1)), []).append((o["top"], o["bottom"]))
        elif h <= 0.5 and w < _DOT_MAX_LEN:
            groups.setdefault(("h", round(o["top"], 1)), []).append((o["x0"], o["x1"]))
    for (axis, pos), segs in groups.items():
        segs.sort()
        run = [segs[0]]
        for a, b in segs[1:] + [(float("inf"), float("inf"))]:
            if a - run[-1][1] <= _DOT_MAX_GAP:
                run.append((a, b))
                continue
            if len(run) >= _DOT_MIN_COUNT:
                lo, hi = run[0][0], max(r[1] for r in run)
                if axis == "v":
                    out["v"].append({"x0": pos, "x1": pos, "top": lo, "bottom": hi, "width": 0,
                                     "height": hi - lo, "object_type": "line"})
                else:
                    out["h"].append({"x0": lo, "x1": hi, "top": pos, "bottom": pos, "width": hi - lo,
                                     "height": 0, "object_type": "line"})
            run = [(a, b)]
    return out["v"], out["h"]


def _record_clips() -> None:
    """pdfminer 해석기가 `W n` 을 지날 때 클립 경로와 그때의 변환 행렬을 쪽(PDFPage)에 남기게 한다.
    pdfplumber 가 글자·선을 읽는 그 한 번의 해석에서 얻는다(그리기 명령을 다시 읽으면 파싱 시간이 40% 늘었다)."""
    from pdfminer.pdfinterp import PDFPageInterpreter as I

    if getattr(I, "_pgdp_clips", False):
        return
    do_m, do_W, do_W_a, do_n, process_page = I.do_m, I.do_W, I.do_W_a, I.do_n, I.process_page

    def m(self, x, y):
        self._pgdp_clip = False
        return do_m(self, x, y)

    def W(self):
        self._pgdp_clip = True
        return do_W(self)

    def W_a(self):
        self._pgdp_clip = True
        return do_W_a(self)

    def n(self):
        out = getattr(self, "_pgdp_out", None)
        if getattr(self, "_pgdp_clip", False) and out is not None:
            out.append((self.ctm, list(self.curpath)))
        self._pgdp_clip = False
        return do_n(self)

    def page(self, pg):
        self._pgdp_out = pg._pgdp_clips = []
        return process_page(self, pg)

    I.do_m, I.do_W, I.do_W_a, I.do_n, I.process_page = m, W, W_a, n, page
    I._pgdp_clips = True


_record_clips()


def _clip_rects(page) -> List[Tuple[float, float, float, float]]:
    """쪽 그리기 명령의 축에 나란한 사각형 클립 (x0, top, x1, bottom).

    한컴·Acrobat 으로 만든 PDF 는 표 칸마다 그 칸 사각형으로 클립을 걸고 글을 그린다. 선이 없는 칸 경계도 여기 남는다.
    """
    if hasattr(page.page_obj, "_pgdp_rects"):
        return list(page.page_obj._pgdp_rects)
    page.chars  # 해석을 끝내야 기록이 있다
    out = []
    for ctm, path in getattr(page.page_obj, "_pgdp_clips", None) or []:
        pts = [seg[-2:] for seg in path if seg[0] in ("m", "l")]
        if len(pts) not in (4, 5):
            continue
        pts = [(ctm[0] * x + ctm[2] * y + ctm[4], ctm[1] * x + ctm[3] * y + ctm[5]) for x, y in pts]
        xs, ys = sorted({round(p[0], 2) for p in pts}), sorted({round(p[1], 2) for p in pts})
        if len(xs) == 2 and len(ys) == 2:
            out.append((xs[0], page.height - ys[1], xs[1], page.height - ys[0]))
    return out


def _clip_groups(clips, page_area: float) -> List[Tuple[Tuple[float, float, float, float], List[tuple]]]:
    """변을 맞대고 타일처럼 붙은 클립끼리 묶은 (묶음 bbox, 칸들). 칸 클립은 이웃 칸과 변을 맞댄다.
    글 둘레에 조금 안쪽으로 건 클립(Acrobat)은 서로 떨어져 있어 묶이지 않는다. 쪽 절반 넘는 클립(쪽 틀)은 뺀다."""
    rects = sorted({tuple(round(v, 2) for v in r) for r in clips
                    if r[2] - r[0] >= 2 and r[3] - r[1] >= 2 and (r[2] - r[0]) * (r[3] - r[1]) <= 0.5 * page_area})
    key = lambda v: round(v * 2)
    by_left: Dict[int, List[int]] = {}
    by_top: Dict[int, List[int]] = {}
    for i, r in enumerate(rects):
        by_left.setdefault(key(r[0]), []).append(i)
        by_top.setdefault(key(r[1]), []).append(i)
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    linked = set()
    for i, (a, t, b, u) in enumerate(rects):
        for k in (key(b) - 1, key(b), key(b) + 1):  # 오른쪽 이웃(왼변 = 내 오른변)
            for j in by_left.get(k, []):
                if abs(rects[j][0] - b) <= 0.5 and min(u, rects[j][3]) - max(t, rects[j][1]) > 1:
                    parent[find(i)] = find(j)
                    linked.update((i, j))
        for k in (key(u) - 1, key(u), key(u) + 1):  # 아래 이웃(윗변 = 내 아랫변)
            for j in by_top.get(k, []):
                if abs(rects[j][1] - u) <= 0.5 and min(b, rects[j][2]) - max(a, rects[j][0]) > 1:
                    parent[find(i)] = find(j)
                    linked.update((i, j))
    groups: Dict[int, List[tuple]] = {}
    for i in linked:
        groups.setdefault(find(i), []).append(rects[i])
    return [((min(r[0] for r in g), min(r[1] for r in g), max(r[2] for r in g), max(r[3] for r in g)), g)
            for g in groups.values()]


def _clip_cell_lines(rects, bbox, cells=(), pad: float = 0.0) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """표 bbox 안 클립 칸의 변을 가상 세로선·가로선으로. 표 전체를 덮는 클립(표 틀)은 뺀다.
    이미 찾은 칸의 같은 방향 변에서 1pt 넘게 pad 안으로 들어온 클립 변(Word 가 칸 안 글 영역에 거는 여백)은 칸 경계가 아니다.
    칸을 나누는 클립 변(한컴 하위 행)은 칸 변과 겹치거나 글 한 줄 넘게 떨어진다."""
    x0, top, x1, bottom = bbox
    area = max((x1 - x0) * (bottom - top), 1.0)
    vs, hs = {}, {}
    cells = list(cells)
    near = {lo: layout.Bins(cells, lambda c, lo=lo: c[lo] - 1, lambda c, hi=hi: c[hi] + 1) for lo, hi in ((0, 2), (1, 3))}

    def padding(v, lo, hi, axis):  # 그 클립 변이 담긴 칸의 같은 방향 변에서 여백만큼 들어왔다
        both = near[lo].ids(v) & near[axis[0]].ids(axis[1])
        return any(c[lo] - 1 <= v <= c[hi] + 1 and min(abs(v - c[lo]), abs(v - c[hi])) > 1
                   and min(abs(v - c[lo]), abs(v - c[hi])) < pad and c[axis[0]] - 1 <= axis[1] <= c[axis[2]] + 1
                   for c in (cells[i] for i in both))

    for a, t, b, u in rects:
        if a < x0 - 1 or b > x1 + 1 or t < top - 1 or u > bottom + 1 or (b - a) * (u - t) > 0.9 * area:
            continue
        for x in (a, b):
            if padding(x, 0, 2, (1, (t + u) / 2, 3)):
                continue
            vs[(round(x, 1), round(t, 1), round(u, 1))] = {"x0": x, "x1": x, "top": t, "bottom": u, "width": 0,
                                                         "height": u - t, "object_type": "line"}
        for y in (t, u):
            if padding(y, 1, 3, (0, (a + b) / 2, 2)):
                continue
            hs[(round(y, 1), round(a, 1), round(b, 1))] = {"x0": a, "x1": b, "top": y, "bottom": y, "width": b - a,
                                                         "height": 0, "object_type": "line"}
    return list(vs.values()), list(hs.values())


def _open_table_floor(page, t, bbox, horiz, dotted_v) -> Optional[float]:
    """표 아래로 선 없는 본문이 이어지는 표의 바닥(가로선 y). 표 안에서 시작한 세로선이 표 폭 가로선까지 내려가야 한다."""
    x0, top, x1, bottom = bbox
    width = x1 - x0
    verts = [e for e in page.edges if e["orientation"] == "v"] + dotted_v
    for e in sorted((e for e in horiz if e["top"] > bottom + 2 and e["x0"] <= x0 + 0.1 * width
                     and e["x1"] >= x1 - 0.1 * width), key=lambda e: e["top"]):
        if any(x0 - 1 <= v["x0"] <= x1 + 1 and v["top"] < bottom - 1 and v["bottom"] >= e["top"] - 1 for v in verts):
            return e["top"]
        return None  # 바로 아래 가로선까지 이어지는 세로선이 없으면 표는 여기서 끝난다
    return None


def _word_spans(lines) -> List[Tuple[float, float]]:
    """줄들의 낱말 구간(양끝 0.3 씩 줄임). 글자끼리 맞붙은 틈(A|lpaca)은 낱말 안이라 선이 지나면 안 된다."""
    out = []
    for l in lines:
        sizes = sorted(c["size"] for c in l)
        for f in _fragments(l, _CELL_WORD_GAP * sizes[len(sizes) // 2]):
            out.append((f[0] + 0.3, f[1] - 0.3))
    return out


def _line_gap(line, gap_min: float) -> float:
    """열 간격 기준. 본문보다 작은 글자로 짠 표 줄은 그 줄의 글자 크기로 잰다(큰 글자 줄은 쪽 기준 그대로)."""
    sizes = sorted(c["size"] for c in line)
    return min(gap_min, _HIDDEN_GAP * sizes[len(sizes) // 2]) if sizes else gap_min


def _wide_fragments(line) -> list:
    """한 줄을 낱말 간격보다 훨씬 넓은 틈(글자 크기의 0.75배 이상이고 낱말 간격의 2.5배 이상)에서 끊은 덩어리."""
    cs = sorted((c for c in line if c.get("text", "").strip()), key=lambda c: c["x0"])
    if len(cs) < 2:
        return []
    size = median(c["size"] for c in cs)
    gaps = [b["x0"] - a["x1"] for a, b in zip(cs, cs[1:])]
    words = [g for g in gaps if g > 0.15 * size]
    word = median(words) if words else 0.25 * size
    cut = max(0.75 * size, 2.5 * word)
    return _fragments(cs, cut)


def _rule_band_lines(page, found, horiz) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """가로선만 있는 표(논문식 세 줄 표)의 가상 세로선. 가로선이 3개 이상 위아래로 겹쳐 늘어서고 그 사이 띠의 글이
    열로 갈리면 표다(캡션·주석처럼 열로 안 갈리는 띠에서 끊는다). 같은 높이의 선 조각은 먼저 잇고, 머리 일부 열에만 그은
    짧은 선(cmidrule)도 그 표의 선이다. 표 폭은 가장 넓은 선. 열 경계는 줄이 가장 많은 띠(본문) 줄들이 함께 갈리는 자리이고,
    각 띠에서 그 자리를 가로지르는 글자가 없을 때만 긋는다(여러 열에 걸친 머리는 병합 칸이 된다). 이미 찾은 표 자리는 뺀다."""
    chars = [c for c in page.chars if c.get("text", "").strip()]
    if not chars:
        return [], []
    sizes = sorted(c["size"] for c in chars)
    gap_min = _HIDDEN_GAP * sizes[len(sizes) // 2]
    v_edges = None  # 세로 선(처음 쓸 때 만든다)
    rules: List[List[float]] = []  # [x0, x1, y] 같은 높이에서 이어지는 조각을 이은 선
    rules_at: Dict[int, List[int]] = {}  # 높이(정수) → rules 번호. 높이는 만든 뒤 바뀌지 않는다

    def rules_near(y: float):
        found = sorted(i for k in range(math.floor(y) - 2, math.floor(y) + 3) for i in rules_at.get(k, ()))
        return (rules[i] for i in found)

    for e in sorted(horiz, key=lambda e: (round(e["top"]), e["x0"])):
        r = next((r for r in rules_near(e["top"]) if abs(r[2] - e["top"]) <= 1.5 and e["x0"] <= r[1] + 2 and r[0] <= e["x1"] + 2), None)
        if r:
            r[0], r[1] = min(r[0], e["x0"]), max(r[1], e["x1"])
        else:
            rules_at.setdefault(math.floor(e["top"]), []).append(len(rules))
            rules.append([e["x0"], e["x1"], e["top"]])
    groups: List[List[List[float]]] = []  # 겹쳐 늘어선 선 묶음
    for r in sorted((r for r in rules if r[1] - r[0] >= _RULE_MIN_WIDTH * page.width), key=lambda r: r[2]):
        def overlap(g):
            gx0, gx1 = min(q[0] for q in g), max(q[1] for q in g)
            return max(0.0, min(r[1], gx1) - max(r[0], gx0)) / max(1.0, min(r[1] - r[0], gx1 - gx0))
        g = max(groups, key=overlap, default=None)
        if g is not None and overlap(g) >= 0.8:
            g.append(r)
        else:
            groups.append([r])
    groups = [[{"x0": r[0], "x1": r[1], "top": r[2]} for r in g] for g in groups]

    by_center = layout.center_bins(chars)

    def band_lines(x0, x1, a, b):
        inside = sorted((c for c in by_center.around(a, b) if x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and a < (c["top"] + c["bottom"]) / 2 < b),
                        key=lambda c: (round(c["top"]), c["x0"]))
        return _char_lines(inside)

    vs: List[Dict[str, Any]] = []
    hs: List[Dict[str, Any]] = []
    for g in groups:
        ys: List[float] = []
        for y in (e["top"] for e in g):
            if not ys or y - ys[-1] > 1.5:  # 굵은 선(위·아래 두 변)은 하나로
                ys.append(y)
        if len(ys) < 2:
            continue
        x0, x1 = min(e["x0"] for e in g), max(e["x1"] for e in g)

        def columnar(line):  # 열로 갈린 줄. 긴 덩어리 둘(2단 본문 한 줄: 덩어리마다 20자 넘는 글)은 아니다
            f = _fragments(line, _line_gap(line, gap_min))
            return len(f) >= 2 and not (len(f) == 2 and all(q[1] - q[0] > _TWO_COLUMN_PROSE * (x1 - x0)
                                                            and len(q[2].replace(" ", "")) > 20 for q in f))

        runs, run = [], [ys[0]]
        bands_all = list(zip(ys, ys[1:]))
        title_bands, form_bands = set(), set()
        for i, (a, b) in enumerate(bands_all):
            lines = band_lines(x0, x1, a, b)
            # 맨 위 띠의 한 줄은 다음 띠가 열로 갈리면 표 제목칸(여러 열에 걸친 머리)이다
            title = i == 0 and len(lines) == 1 and len(bands_all) > 1 and \
                sum(columnar(l) for l in band_lines(x0, x1, *bands_all[1])) * 2 >= max(1, len(band_lines(x0, x1, *bands_all[1])))
            if title and not columnar(lines[0]):
                title_bands.add((a, b))
            # 두 줄 이름 칸 옆 값이 가운데 줄에 찍히면 이름 줄이 열로 안 갈린 줄로 남는다. 열로 갈린 줄이 셋 이상이면 표 띠다
            n_col = sum(columnar(l) for l in lines)
            left = min((min(c["x0"] for c in l) for l in lines), default=x0)
            at_left = lambda l: min(c["x0"] for c in l) <= left + gap_min
            names = n_col >= _HIDDEN_MIN_ROWS and all(columnar(l) or at_left(l) for l in lines)
            # 빈 양식: 머리 한 줄만 열로 갈리고 그 아래 줄은 모두 첫 열 이름뿐(나머지 칸은 비움).
            # 머리 칸 사이는 열 간격보다 좁을 수 있어 그 줄 낱말 간격의 여러 배로 잰다
            if len(lines) > _HIDDEN_MIN_ROWS and len(_wide_fragments(lines[0])) >= 2 \
                    and all(not columnar(l) and at_left(l) for l in lines[1:]):
                names = True
                form_bands.add((a, b))
            if lines and n_col * 2 < len(lines) and not names and not title:
                runs.append(run)  # 열로 안 갈리는 띠(캡션·주석)에서 표가 끊긴다
                run = [b]
            else:
                run.append(b)
        runs.append(run)
        for run in runs:
            # 표 폭은 이 표의 선으로(같은 묶음에 든 위아래 다른 표가 더 넓을 수 있다)
            mine = [e for e in g if run[0] - 1.5 <= e["top"] <= run[-1] + 1.5]
            x0, x1 = min(e["x0"] for e in mine), max(e["x1"] for e in mine)
            # 위·아래 선 둘뿐인 표(머리 밑 선 없음)는 그 사이 열로 갈린 줄이 셋 이상일 때만
            # 이미 찾은 표와 겹치면 뺀다. 위아래 선 안쪽에 통째로 든 것(배경 칠한 강조 행)은 이 표의 행이다
            # 두 선이 세로 변으로 이어진 상자(그래프 틀)는 아니다
            boxed = len(run) == 2 and any(e["orientation"] == "v" and x0 - 2 <= e["x0"] <= x1 + 2
                                          and e["top"] <= run[0] + 1.5 and e["bottom"] >= run[1] - 1.5 for e in page.edges)
            if len(run) < 2 or boxed or (len(run) == 2 and len(band_lines(x0, x1, run[0], run[1])) < _HIDDEN_MIN_ROWS) \
                    or any(f[0] < x1 and x0 < f[2] and f[1] < run[-1] and run[0] < f[3]
                           and not (x0 - 2 <= f[0] and f[2] <= x1 + 2 and run[0] + 1.5 < f[1] and f[3] < run[-1] - 1.5)
                           for f in found):
                continue
            bands = list(zip(run, run[1:]))
            body = max((band_lines(x0, x1, a, b) for a, b in bands), key=len)
            if len(body) < _HIDDEN_MIN_ROWS:  # 머리 한 줄·값 한 줄짜리 표: 모든 띠의 줄이 함께 갈리는 자리
                body = [l for a, b in bands for l in band_lines(x0, x1, a, b)]
            row_ys: List[float] = []
            if len(run) == 2:  # 안쪽 선이 없으면 행은 줄 사이가 벌어진 자리(칸 안 줄바꿈은 줄 사이가 좁다)
                rows: List[List[float]] = []
                for top, bottom in sorted((min(c["top"] for c in l), max(c["bottom"] for c in l)) for l in body):
                    if rows and top < rows[-1][1] - 0.5:
                        rows[-1][1] = max(rows[-1][1], bottom)
                    else:
                        rows.append([top, bottom])
                height = median(b - a for a, b in rows)
                row_ys = [(b0 + a1) / 2 for (_, b0), (a1, _) in zip(rows, rows[1:]) if a1 - b0 >= 0.5 * height]
                bands = list(zip([run[0]] + row_ys, row_ys + [run[1]]))  # 열 선은 행마다 따로 긋는다(제목 행은 병합)
                form = any(fb[0] >= run[0] - 1 and fb[1] <= run[-1] + 1 for fb in form_bands)
                if row_ys and not form and all(not columnar(l) for l in body if max(c["bottom"] for c in l) < row_ys[0]):
                    body = [l for l in body if max(c["bottom"] for c in l) > row_ys[0]]  # 맨 위 제목 행은 열 자리에서 뺀다
            # 빈 양식은 머리 한 줄만 갈린다
            form = any(fb[0] >= run[0] - 1 and fb[1] <= run[-1] + 1 for fb in form_bands)
            need = 1 if form else min(_HIDDEN_MIN_ROWS, len(body))
            gaps =[(p[1], q[0]) for l in body for p, q in (lambda f: zip(f, f[1:]))(_wide_fragments(l) if form else _fragments(l, _line_gap(l, gap_min)))]
            # 두 줄 이름 칸 옆 값만 찍힌 줄(이름 줄과 따로 잡힌다)도 첫 열 자리가 빈 간격이다. 이름과 값 사이 경계가 그 겹침으로 선다
            starts = [min(c["x0"] for c in l) for l in body]
            text_left = min(starts, default=x0)
            lead = [(text_left, f[0][0]) for l in body for f in [_fragments(l, _line_gap(l, gap_min))]
                    if len(f) >= 2 and f[0][0] > text_left + gap_min]
            # 이름 줄의 이름|값 간격과 겹칠 때만(첫 열 안에서 오른쪽 정렬된 값 앞 빈자리는 아니다)
            gaps += [g for g in lead if any(max(g[0], a) < min(g[1], b) for a, b in gaps)]
            clusters: List[list] = []
            for a, b in sorted(gaps):
                k = next((k for k in clusters if max(k[0], a) < min(k[1], b)), None)
                if k:
                    k[:] = [max(k[0], a), min(k[1], b), k[2] + 1, min(k[3], a), max(k[4], b)]
                else:
                    clusters.append([a, b, 1, a, b])
            glyphs = _word_spans(body)
            # 다른 띠의 글 덩어리(열 간격으로 끊은 것). 낱말로 끊으면 여러 열에 걸친 머리(Mean (log DNA …))의 낱말 사이로 선이 간다
            frags_of = lambda ab: [(f[0] + 0.3, f[1] - 0.3) for l in band_lines(x0, x1, *ab)
                                   for f in _fragments(l, _line_gap(l, gap_min))]
            everywhere = [f for ab in bands for f in frags_of(ab)]
            # 본문 바로 위 띠(열 이름 줄)를 먼저 피한다. 그 위 띠는 여러 열에 걸친 묶음 머리라 어차피 병합 칸이다
            k_body = max(range(len(bands)), key=lambda k: len(band_lines(x0, x1, *bands[k])))
            heads = frags_of(bands[k_body - 1]) if k_body > 0 else []
            # 낱말 간격보다 훨씬 넓게 떨어진 머리 낱말 덩어리(따로 선 열 이름)는 병합 머리 낱말보다 먼저 피한다
            wide = [(f[0] + 0.3, f[1] - 0.3) for ab in bands for l in band_lines(x0, x1, *ab) for f in _wide_fragments(l)]
            cols = []
            for lo, hi, n, wide_lo, wide_hi in clusters:
                free = _free_spans(wide_lo - 1.0, wide_hi + 1.0, glyphs) if n >= need else []
                if free:
                    mid = (lo + hi) / 2
                    a, b = min(free, key=lambda s: 0 if s[0] <= mid <= s[1] else min(abs(s[0] - mid), abs(s[1] - mid)))
                    x = mid if a <= mid <= b else (a + b) / 2
                    # 그 빈 구간 안에서 다른 띠(머리)의 낱말을 덜 가로지르는 자리로 옮긴다. 다 걸치면(병합 머리) 그대로
                    steps = [a + (b - a) * i / 40 for i in range(41)]
                    cross = lambda p, fs: sum(w0 < p < w1 for w0, w1 in fs)
                    x = min(steps + [x], key=lambda p: (cross(p, heads), cross(p, wide), cross(p, everywhere), abs(p - x)))
                    # 표 안 강조 행이 그린 세로 변이 그 빈자리에 있으면 그 자리(두 선이 가는 가짜 열을 만들지 않게)
                    if v_edges is None:
                        v_edges = layout.Bins([e for e in page.edges if e["orientation"] == "v"], lambda e: e["x0"], lambda e: e["x0"])
                    drawn = [e["x0"] for e in v_edges.around(a, b) if a <= e["x0"] <= b
                             and run[0] - 1 <= e["top"] and e["bottom"] <= run[-1] + 1]
                    if drawn:
                        x = min(drawn, key=lambda p: abs(p - x))
                    cols.append(x)
            if not cols:
                continue
            # 본문 위 머리 띠에 겹치지 않는 줄이 여럿이면(칸 안 줄바꿈이 아니라 층층이 쌓인 묶음 머리) 줄마다 행으로 가르고,
            # 열보다 덩어리가 적은 줄은 제목 띠처럼 선 끝 자리에서만 나눈다
            split, stub = [], min(cols)
            split_titles = set()
            for k, (a, b) in enumerate(bands):
                ls = band_lines(x0, x1, a, b)
                fr = [_fragments(l, _line_gap(l, gap_min)) for l in ls]
                # 줄끼리 위아래로 겹치면(칸마다 세로 가운데 맞춤·위 첨자로 높이가 어긋난 한 행) 쌓인 머리가 아니다
                stacked = k < k_body and len(ls) >= 2 and all(
                    max(c["bottom"] for c in u) <= min(c["top"] for c in v) + 0.5 for u, v in zip(ls, ls[1:])) and not any(
                    p[0] < q[1] and q[0] < p[1] for i in range(len(fr)) for j in range(i + 1, len(fr))
                    for p in fr[i] for q in fr[j])
                if not stacked:
                    split.append((a, b))
                    continue
                ys = [(max(c["bottom"] for c in u) + min(c["top"] for c in v)) / 2 for u, v in zip(ls, ls[1:])]
                for (sa, sb), f in zip(zip([a] + ys, ys + [b]), fr):
                    split.append((sa, sb))
                    if len(f) <= len(cols):
                        title_bands.add((sa, sb))
                        split_titles.add((sa, sb))
                # 첫 열(행 머리 칸)에 이름이 하나뿐이면 그 칸은 머리 층 전체에 걸친다
                lead = x0 if sum(q[1] <= stub for f in fr for q in f) > 1 else stub
                hs += [{"x0": lead, "x1": x1, "top": y, "bottom": y, "width": x1 - lead, "height": 0, "object_type": "line"}
                       for y in ys]
            bands = split
            for a, b in bands:
                band_glyphs = _word_spans(band_lines(x0, x1, a, b))
                # 제목 띠(여러 열에 걸친 한 줄, Year)는 한 칸. 그 띠를 덮는 선이 끝나는 열 경계(첫 열 위가 빈 꼴)만 긋는다
                xs = cols + [x0, x1]
                if (a, b) in title_bands:  # 선 끝 자리(가까운 열 경계가 있으면 그 경계에 맞춘다)
                    ends = [p for e in g if abs(e["top"] - a) <= 1.5 or abs(e["top"] - b) <= 1.5
                            for p in (e["x0"], e["x1"]) if x0 + gap_min < p < x1 - gap_min]
                    near = lambda p: min(cols, key=lambda x: abs(x - p))
                    xs = [x0, x1] + [near(p) if abs(near(p) - p) <= 2 * gap_min else p for p in ends]
                    if (a, b) in split_titles and not any(g0 < stub for g0, _ in band_glyphs):
                        xs.append(stub)
                for x in xs:
                    if x in (x0, x1) or not any(g0 < x < g1 for g0, g1 in band_glyphs):
                        vs.append({"x0": x, "x1": x, "top": a, "bottom": b, "width": 0, "height": b - a, "object_type": "line"})
            # 위·아래만 표 폭으로 닫는다. 안쪽 짧은 선(cmidrule)은 그려진 그대로 둬야 그 위 머리가 병합 칸으로 남는다
            hs += [{"x0": x0, "x1": x1, "top": y, "bottom": y, "width": x1 - x0, "height": 0, "object_type": "line"}
                   for y in (run[0], run[-1])]
            # 안쪽 선은 양 끝을 조금 줄여 그린다(cmidrule trim). 가까운 열 경계까지 늘려야 그 칸이 닫힌다
            bounds = sorted(cols + [x0, x1])
            for e in horiz:  # 짧은 선(첫 열 아래만 그은 것)까지
                if run[0] + 1.5 < e["top"] < run[-1] - 1.5 and x0 - 1 <= e["x0"] and e["x1"] <= x1 + 1:
                    # 끝이 든 열의 절반 넘게 덮으면 그 열 경계까지(열 간격보다 많이 줄여 그린 cmidrule)
                    lo = max((x for x in bounds if x <= e["x0"] + 0.5), default=e["x0"])
                    lo_next = min((x for x in bounds if x > e["x0"] + 0.5), default=e["x1"])
                    hi = min((x for x in bounds if x >= e["x1"] - 0.5), default=e["x1"])
                    hi_prev = max((x for x in bounds if x < e["x1"] - 0.5), default=e["x0"])
                    # 덮는 길이는 조각을 이은 선으로 잰다(칸마다 끊어 그린 선)
                    whole = next((r for r in rules_near(e["top"]) if abs(r[2] - e["top"]) <= 1.5 and r[0] - 0.5 <= e["x0"] and e["x1"] <= r[1] + 0.5),
                                 [e["x0"], e["x1"]])
                    a = lo if e["x0"] - lo <= gap_min or min(whole[1], lo_next) - e["x0"] > (lo_next - lo) / 2 else e["x0"]
                    b = hi if hi - e["x1"] <= gap_min or e["x1"] - max(whole[0], hi_prev) > (hi - hi_prev) / 2 else e["x1"]
                    hs.append({"x0": a, "x1": b, "top": e["top"], "bottom": e["top"], "width": b - a, "height": 0,
                               "object_type": "line"})
            hs += [{"x0": x0, "x1": x1, "top": y, "bottom": y, "width": x1 - x0, "height": 0, "object_type": "line"}
                   for y in row_ys]
    return vs, hs


def _header_above(page, t, x0: float, top: float, x1: float) -> Optional[Tuple[float, List[Dict[str, Any]]]]:
    """선 친 표 바로 위, 위아래 선 없이 놓인 머리 한 줄. 열 간격으로 갈린 덩어리가 둘 이상이고 덩어리마다 표의 한 열 안에
    들면 머리행이다(표 위 제목 한 줄은 한 덩어리). 그 줄까지 표를 올리고 덩어리를 가르지 않는 열 경계에 세로선을 긋는다."""
    chars = [c for c in page.chars if c.get("text", "").strip() and x0 - 1 <= (c["x0"] + c["x1"]) / 2 <= x1 + 1
             and c["bottom"] <= top + 1]
    if not chars:
        return None
    size = median(c["size"] for c in chars)
    near = [c for c in chars if c["bottom"] >= top - 1.5 * size]
    lines = _char_lines(sorted(near, key=lambda c: (round(c["top"]), c["x0"])))
    if len(lines) != 1 or any(c["bottom"] > top + 1 for c in lines[0]):
        return None
    frags = _fragments(lines[0], _HIDDEN_GAP * size)
    xs = sorted({round(x, 1) for c in t.cells if c for x in (c[0], c[2])} | {round(x0, 1), round(x1, 1)})
    cols = list(zip(xs, xs[1:]))
    if len(frags) < 2 or not all(any(a - 1 <= f0 and f1 <= b + 1 for a, b in cols) for f0, f1, _ in frags):
        return None
    new_top = min(c["top"] for c in lines[0]) - 1
    # 위에 다른 글이 바로 붙어 있으면(문단 끝 줄) 머리가 아니다
    if any(new_top - 1.5 * size <= c["bottom"] < new_top for c in chars):
        return None
    out = [{"x0": x, "x1": x, "top": new_top, "bottom": top, "width": 0, "height": top - new_top, "object_type": "line"}
           for x in xs if not any(f0 < x < f1 for f0, f1, _ in frags)]
    return new_top, out


def _frame_grid_lines(page, found) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """칸마다 글 틀(선 없는 채움 사각형)만 있고 괘선은 그림인 표(슬라이드에서 내보낸 표): 틀이 같은 가로 범위로 3열 이상,
    여러 열이 같은 높이에서 시작하는 행이 3개 이상이면 열 사이·행 사이에 가상 선을 긋는다. 찾은 표 안의 틀은 보지 않는다.
    행 경계는 열마다 따로 그어, 여러 행에 걸친 틀(병합 칸)은 자르지 않는다."""
    chars = [c for c in page.chars if c.get("text", "").strip()]
    frames = []
    for r in page.rects:
        if not r.get("fill") or r.get("stroke") or min(r["x1"] - r["x0"], r["bottom"] - r["top"]) <= 2 \
                or r["x1"] - r["x0"] > 0.9 * page.width:
            continue
        box = (r["x0"], r["top"], r["x1"], r["bottom"])
        if any(_center_inside(box, t) for t in found):
            continue
        if any(_center_inside((c["x0"], c["top"], c["x1"], c["bottom"]), box, 0.0) for c in chars):
            frames.append(box)
    cols: List[List[Tuple[float, ...]]] = []
    for f in sorted(frames):
        col = next((c for c in cols if abs(c[0][0] - f[0]) <= 2 and abs(c[0][2] - f[2]) <= 2), None)
        if col:
            col.append(f)
        else:
            cols.append([f])
    cols = sorted((c for c in cols if len(c) >= 3), key=lambda c: c[0][0])
    if len(cols) < 3 or any(a[0][2] > b[0][0] for a, b in zip(cols, cols[1:])):
        return [], []
    starts: List[List[float]] = []
    for t in sorted(f[1] for c in cols for f in c):
        if starts and t - starts[-1][0] <= 3:
            starts[-1].append(t)
        else:
            starts.append([t])
    rows = [min(s) for s in starts if sum(any(abs(f[1] - min(s)) <= 3 for f in c) for c in cols) >= 2]
    if len(rows) < 3:
        return [], []
    top, bottom = rows[0] - 1, max(f[3] for c in cols for f in c) + 1
    edges = [cols[0][0][0] - 1] + [(a[0][2] + b[0][0]) / 2 for a, b in zip(cols, cols[1:])] + [cols[-1][0][2] + 1]
    line = lambda x0, t, x1, b: {"x0": x0, "x1": x1, "top": t, "bottom": b, "width": x1 - x0, "height": b - t,
                                 "object_type": "line"}
    vs = [line(x, top, x, bottom) for x in edges]
    hs = [line(edges[0], y, edges[-1], y) for y in (top, bottom)]
    for y in rows[1:]:
        for k, c in enumerate(cols):
            if not any(f[1] < y - 1 - 3 and f[3] > y - 1 + 1 for f in c):
                hs.append(line(edges[k], y - 1, edges[k + 1], y - 1))
    return vs, hs


def _stripe_gap_lines(page, tables) -> List[Dict[str, Any]]:
    """줄무늬 행 표: 배경 칠한 행 묶음 사이에 배경 없는 행이 끼면 조각마다 따로 잡힌다. 폭이 같은 조각이 한 행 높이 안
    간격으로 이어지고 그 틈에 글(배경 없는 행)이 있으면 두 조각이 함께 쓰는 열 경계와 바깥 변을 세로선으로 그어 한 표로 잇는다.
    틈이 비었으면 따로 놓인 두 표다."""
    out: List[Dict[str, Any]] = []
    pieces = sorted(tables, key=lambda t: t.bbox[1])
    for a, b in zip(pieces, pieces[1:]):
        (ax0, _, ax1, abot), (bx0, btop, bx1, _) = a.bbox, b.bbox
        heights = [c[3] - c[1] for t in (a, b) for c in t.cells if c]
        if abs(ax0 - bx0) > 2 or abs(ax1 - bx1) > 2 or not heights or not 0 < btop - abot <= 1.5 * median(heights):
            continue
        if not any(c.get("text", "").strip() and ax0 <= (c["x0"] + c["x1"]) / 2 <= ax1 and abot < (c["top"] + c["bottom"]) / 2 < btop
                   for c in page.chars):
            continue
        edges = lambda t, y: {round(x) for c in t.cells if c and abs(c[3 if y == "bottom" else 1] - t.bbox[3 if y == "bottom" else 1]) <= 1
                              for x in (c[0], c[2])}
        for x in sorted(edges(a, "bottom") & edges(b, "top")):
            out.append({"x0": x, "x1": x, "top": abot, "bottom": btop, "width": 0, "height": btop - abot, "object_type": "line"})
    return out


def _find_tables(page, spaced: bool = False) -> list:
    """표를 찾고, 찾은 표의 바깥 틀을 가상 선으로 닫아 한 번 더 찾는다.

    공공 문서 표는 바깥 세로 테두리를 흔히 생략한다. 그러면 가장자리 칸의 모서리가 안 닫혀 칸이 빠진다.
    """
    sizes = sorted(c["size"] for c in page.chars if c.get("text", "").strip())
    pad = 0.8 * sizes[len(sizes) // 2] if sizes else 0.0  # 칸 여백 클립: 글 한 줄 높이보다 칸 변에 가깝다
    framed = page
    floating = _floating_shading(page)
    if floating:
        page = page.filter(lambda o: o.get("object_type") != "rect" or (o["x0"], o["top"], o["x1"], o["bottom"]) not in floating)
    dv, dh = _dotted_lines(page)
    # 위아래 좌표가 소수점 끝만 다른 가로선을 pdfplumber 는 세로선으로 분류해 버린다
    dh += [{"x0": l["x0"], "x1": l["x1"], "top": (l["top"] + l["bottom"]) / 2, "bottom": (l["top"] + l["bottom"]) / 2,
            "width": l["x1"] - l["x0"], "height": 0, "object_type": "line"}
           for l in page.lines if 0 < l["bottom"] - l["top"] < 0.5 and l["x1"] - l["x0"] >= 3]
    first = page.find_tables({"explicit_vertical_lines": dv, "explicit_horizontal_lines": dh})
    horiz = [e for e in page.edges if e["orientation"] == "h" and e["x1"] - e["x0"] >= 3] + dh
    # 칸 하나짜리(제목 띠 배경 사각형)는 표가 아니라 선 사이 제목칸이다
    vs, hs = _rule_band_lines(page, [t.bbox for t in first if sum(1 for c in t.cells if c) > 1], horiz)
    fv, fh = _frame_grid_lines(framed, [t.bbox for t in first])
    vs, hs = vs + fv, hs + fh
    if not first and not vs:
        return first
    vs += _stripe_gap_lines(page, first)
    groups = _clip_groups(_clip_rects(page), page.width * page.height)
    for t in first:
        x0, top, x1, bottom = t.bbox
        # 표에 닿는 클립 칸 묶음까지 표다(바깥 테두리 없는 첫 열, 선 없이 클립만 있는 본문). 표를 감싸는 배치용 칸이 든 묶음은 뺀다
        area = max((x1 - x0) * (bottom - top), 1.0)
        mine = []
        for (ga, gt, gb, gu), rects in groups:
            if ga > x1 + 1 or gb < x0 - 1 or gt > bottom + 1 or gu < top - 1:
                continue
            # 한 열짜리(줄 음영 글 상자)를 쪽을 덮는 묶음으로 넓히지 않는다. 한컴 표는 그런 묶음으로 넓혀야 한다
            if (gb - ga) * (gu - gt) > 0.5 * page.width * page.height and len({round(c[0]) for c in t.cells if c}) == 1:
                continue
            if any(max(0.0, min(r[2], x1) - max(r[0], x0)) * max(0.0, min(r[3], bottom) - max(r[1], top)) > 0.5 * area
                   for r in rects):
                continue
            mine += rects
            x0, top, x1, bottom = min(x0, ga), min(top, gt), max(x1, gb), max(bottom, gu)
        # 안쪽 열 세로선 둘 이상이 표 위아래로 함께 더 뻗으면 거기까지 표다(윗선 없는 머리행, 아랫선 밑 합계 행)
        segs: Dict[int, List[Tuple[float, float]]] = {}
        for e in page.edges:
            if e["orientation"] == "v" and x0 - 1 <= e["x0"] <= x1 + 1 and e["bottom"] - e["top"] < 0.8 * page.height:
                segs.setdefault(round(e["x0"]), []).append((e["top"], e["bottom"]))  # 쪽 높이만 한 변(배경·틀)은 빼고
        span: Dict[int, List[float]] = {}
        for x, ss in segs.items():
            if not any(a < bottom - 1 and b > top + 1 for a, b in ss):
                continue
            lo, hi = top, bottom
            for a, b in sorted(ss):  # 칸마다 끊어 그린 세로선을 이어 본다
                if a < lo and b >= lo - 2:
                    lo = a
            for a, b in sorted(ss, key=lambda s: -s[1]):
                if b > hi and a <= hi + 2:
                    hi = b
            span[x] = [lo, hi]
        # 바깥 변 선만 뻗은 것(표를 감싼 상자·쪽 틀)으로는 늘리지 않는다. 안쪽 열 선이 하나는 함께 뻗어야 한다
        inner = {x: s for x, s in span.items() if x0 + 1 < x < x1 - 1}
        if len(span) >= 2 and inner:
            ups, downs = sorted(s[0] for s in span.values()), sorted((s[1] for s in span.values()), reverse=True)
            up = min(top, ups[1]) if min(s[0] for s in inner.values()) < top - 1 else top
            down = max(bottom, downs[1]) if max(s[1] for s in inner.values()) > bottom + 1 else bottom
            grew = [(up, lo) for lo in [None] if up < top] + [(None, down) for _ in [0] if down > bottom]
            top, bottom = up, down
            # 늘린 쪽에서 짧게 그친 열 선은 그 자리에 글이 없거나 열 간격만큼 벌어진 자리일 때만 늘린 끝까지 잇는다
            # (병합 머리칸 글은 낱말 사이 좁은 틈으로 지나가므로 그대로)
            for x, (lo, hi) in span.items():
                for a, b in [(top, lo) for g in grew if g[0] is not None] + [(hi, bottom) for g in grew if g[0] is None]:
                    seg = [c for c in page.chars if c.get("text", "").strip() and c["bottom"] > a and c["top"] < b]
                    if b - a > 1 and not any(
                            c["x0"] - 0.3 < x < c["x1"] + 0.3 or (c["x1"] <= x and any(
                                d["x0"] >= x and d["x0"] - c["x1"] < _HIDDEN_GAP * c["size"] and abs(d["top"] - c["top"]) < 0.5 * c["size"]
                                for d in seg)) for c in seg):
                        vs.append({"x0": x, "x1": x, "top": a, "bottom": b, "width": 0, "height": b - a,
                                   "object_type": "line"})
        # 선 없는 본문: 표 안에서 시작한 세로선이 아래 가로선(표 폭)까지 이어지면 표는 거기까지다.
        # 머리행 맨 아래 칸들의 열 경계를 그 가로선까지 내린다(행은 하위 행 나누기가 글 줄로 가른다)
        floor = _open_table_floor(page, t, (x0, top, x1, bottom), horiz, dv)
        if floor:
            cols = sorted({round(x, 1) for c in t.cells if c and abs(c[3] - bottom) <= 1 for x in (c[0], c[2])})
            vs += [{"x0": x, "x1": x, "top": bottom, "bottom": floor, "width": 0, "height": floor - bottom,
                    "object_type": "line"} for x in cols]
            bottom = floor
        # 바깥 세로선이 없으면 가로선이 뻗은 데까지가 표다(가장자리 열이 세로선 없이 열린 표)
        across = [e for e in horiz if top - 1 <= e["top"] <= bottom + 1 and e["x0"] < x1 + 2 and e["x1"] > x0 - 2]  # 끝이 맞닿은 조각까지
        if across:
            x0, x1 = min(x0, min(e["x0"] for e in across)), max(x1, max(e["x1"] for e in across))
        head = _header_above(page, t, x0, top, x1)
        if head:
            top, lines = head
            vs += lines
        vs += [{"x0": x, "x1": x, "top": top, "bottom": bottom, "width": 0, "height": bottom - top,
                "object_type": "line"} for x in (x0, x1)]
        hs += [{"x0": x0, "x1": x1, "top": y, "bottom": y, "width": x1 - x0, "height": 0,
                "object_type": "line"} for y in (top, bottom)]
        # 칸마다 건 클립이 있으면 선 없는 칸 경계(값 | (증감), 하위 행)까지 그 칸대로
        cv, ch = _clip_cell_lines(mine, (x0, top, x1, bottom), [c for c in t.cells if c], pad)
        vs += cv
        hs += ch
    try:
        tables = page.find_tables({"explicit_vertical_lines": vs + dv, "explicit_horizontal_lines": hs + dh})
        hidden: List[Dict[str, Any]] = []
        for _ in range(3):  # 경계 하나를 넣으면 그 안에서 다음 경계(이름 | 값)가 보인다
            seen = {(round(h["x0"]), round(h["top"])) for h in hidden}
            new = [h for h in _hidden_column_lines(page, tables) if (round(h["x0"]), round(h["top"])) not in seen]
            if not new:
                break
            hidden += new
            tables = page.find_tables({"explicit_vertical_lines": vs + dv + hidden, "explicit_horizontal_lines": hs + dh})
    except Exception:
        tables = first
    return [_drop_nested(t) for t in tables]


def _hidden_column_lines(page, tables) -> List[Dict[str, Any]]:
    """선 없이 나뉜 칸(값 | (증감), 전체 | 남 | 여) 경계에 가상 세로선.

    한 열의 칸들에서 글이 큰 간격(공백 글자가 있어도 보이는 글자 사이로 잰다)으로 갈린 자리가 여러 줄에서 겹치고,
    그 자리를 가로지르는 글자가 없으면 보이지 않는 칸 경계다. 간격 없이 붙어 찍힌 값(85.4100.3)도 그 자리에서 갈린다.
    균등 배분 한글(구 분)이 든 칸은 간격을 세지 않고, 그런 칸이 선이 지날 범위 안에 있으면 긋지 않는다.
    """
    chars = [c for c in page.chars if c.get("text")]
    sizes = sorted(c["size"] for c in chars if c["text"].strip())
    gap_min = _HIDDEN_GAP * sizes[len(sizes) // 2]
    out = []
    for t in tables:
        tx0, ttop, tx1, tbottom = t.bbox
        tchars = [c for c in chars if c["text"].strip() and tx0 <= (c["x0"] + c["x1"]) / 2 <= tx1
                  and ttop <= (c["top"] + c["bottom"]) / 2 <= tbottom]
        columns: Dict[Tuple[int, int], List[tuple]] = {}
        near = layout.center_bins(tchars)
        for x0, top, x1, bottom in (c for c in t.cells if c):
            inside = sorted((c for c in near.around(top, bottom) if x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom),
                            key=lambda c: (round(c["top"]), c["x0"]))
            gaps, spread = [], False
            for line in _char_lines(inside):
                frags = _fragments(line, gap_min)
                spread = spread or (len(frags) > 1 and any(_spread_syllable(f) for _, _, f in frags))
                gaps += [(a[1], b[0]) for a, b in zip(frags, frags[1:]) if not _lone_mark(a[2])]  # 글머리표 뒤 틈은 칸 경계가 아니다
                gaps += _number_gaps(line)
            columns.setdefault((round(x0), round(x1)), []).append((top, bottom, gaps, spread, inside))
        for cells in columns.values():
            plain = [c for c in cells if not c[3]]
            clusters: List[list] = []  # [lo, hi, 줄 수, 간격들 왼끝, 오른끝] 겹치는 간격끼리
            for a, b in sorted(g for c in plain for g in c[2]):
                k = next((k for k in clusters if max(k[0], a) < min(k[1], b)), None)
                if k:
                    k[:] = [max(k[0], a), min(k[1], b), k[2] + 1, min(k[3], a), max(k[4], b)]
                else:
                    clusters.append([a, b, 1, a, b])
            used = [c for c in plain if c[2]]
            if not used:
                continue
            top, bottom = min(c[0] for c in used), max(c[1] for c in used)
            span = [c for c in cells if c[0] >= top - 0.5 and c[1] <= bottom + 0.5]  # 위 머리칸은 선이 닿지 않는다
            if any(c[3] for c in span):  # 균등 배분 이름 칸이 사이에 있다
                continue
            known = Counter(round(x, 1) for c in t.cells if c for x in (c[0], c[2]))
            glyphs = [(ch["x0"] + 0.3, ch["x1"] - 0.3) for c in span for ch in c[4]]
            for lo, hi, n, wide_lo, wide_hi in clusters:
                if n < _HIDDEN_MIN_ROWS:
                    continue
                # 선은 어느 줄의 글자도 가로지르지 않는 빈자리에. 갈린 줄들의 간격 범위에서 모든 줄의 글자를 뺀다
                free = _free_spans(wide_lo - 1.0, wide_hi + 1.0, glyphs)
                line_top = top
                # 맨 위 칸(두 열에 걸친 머리 Other)만 가로막으면 그 칸은 병합 머리로 두고 그 아래부터 긋는다
                # (그 머리칸 아래 줄이 같은 자리에서 갈릴 때만: Other / Male  Female)
                first, rest = sorted(span, key=lambda c: c[0])[0], sorted(span, key=lambda c: c[0])[1:]
                if not free and rest and any(max(a, lo) < min(b, hi) for a, b in first[2]):
                    free = _free_spans(wide_lo - 1.0, wide_hi + 1.0,
                                       [(ch["x0"] + 0.3, ch["x1"] - 0.3) for c in rest for ch in c[4]])
                    line_top = rest[0][0]
                if not free:
                    continue
                # 빈자리에 다른 행의 열 경계(머리행 세로선)가 있으면 그 자리다. 여럿이면 칸이 가장 많이 쓰는 것.
                # 아무 데나 그으면 가는 가짜 열이 생긴다
                near = [k for k in known if any(a <= k <= b for a, b in free)]
                if near:
                    x = max(near, key=lambda k: known[k])
                else:
                    mid = (lo + hi) / 2
                    a, b = min(free, key=lambda s: 0 if s[0] <= mid <= s[1] else min(abs(s[0] - mid), abs(s[1] - mid)))
                    x = mid if a <= mid <= b else (a + b) / 2
                out.append({"x0": x, "x1": x, "top": line_top, "bottom": bottom, "width": 0,
                            "height": bottom - line_top, "object_type": "line"})
    return out


_NUMBER = re.compile(r"(\()?[-−–+*△]?[$€£]?\d(?:[\d,.]*\d)?%?(?:[-−–]\d(?:[\d,.]*\d)?%?)?(?(1)\))\**")


def _number_gaps(line) -> List[Tuple[float, float]]:
    """낱말 간격만큼 떨어진 두 숫자 사이(221 37.6 (32.6–42.6)). 좁은 글꼴 표는 열 간격이 낱말 간격 정도다."""
    cs = sorted((c for c in line if c.get("text", "").strip()), key=lambda c: c["x0"])
    words: List[list] = []
    for c in cs:
        if words and c["x0"] - words[-1][1] <= 0.15 * c["size"]:
            words[-1][1], words[-1][2] = c["x1"], words[-1][2] + c["text"]
        else:
            words.append([c["x0"], c["x1"], c["text"]])
    def apart(a, b):
        if not (_NUMBER.fullmatch(a) and _NUMBER.fullmatch(b)):
            return False
        if re.fullmatch(r"\d{1,3}", a) and re.fullmatch(r"\d{3}", b):  # 띄어 쓴 천 단위(20 527)
            return False
        return not re.fullmatch(r"\(\D?\d[\d,.]*%?\)", b)  # 값 (비율)은 한 칸
    return [(a[1], b[0]) for a, b in zip(words, words[1:]) if apart(a[2], b[2])]


def _free_spans(lo: float, hi: float, covered: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """[lo, hi] 에서 covered 구간들을 뺀 나머지."""
    out, x = [], lo
    for a, b in sorted(c for c in covered if c[1] > lo and c[0] < hi):
        if a > x:
            out.append((x, a))
        x = max(x, b)
    if x < hi:
        out.append((x, hi))
    return out


def _char_lines(chars) -> List[list]:
    lines: List[list] = []
    for c in chars:
        if lines and abs(c["top"] - lines[-1][0]["top"]) <= 0.5 * c["size"]:
            lines[-1].append(c)
        else:
            lines.append([c])
    return [sorted(l, key=lambda c: c["x0"]) for l in lines]


def _fragments(line, gap_min: float) -> List[Tuple[float, float, str]]:
    """보이는 글자 사이가 gap_min 넘게 벌어진 자리에서 끊은 (x0, x1, 글). 공백 글자는 간격으로만 본다."""
    out: List[list] = []
    space = False
    for c in line:
        if not c["text"].strip():
            space = True
            continue
        if out and c["x0"] - out[-1][1] <= gap_min:
            out[-1][1] = max(out[-1][1], c["x1"])
            out[-1][2] += (" " if space else "") + c["text"]
        else:
            out.append([c["x0"], c["x1"], c["text"]])
        space = False
    return [tuple(f) for f in out]


def _lone_mark(token: str) -> bool:
    """글머리·연결 기호 하나(-, ·, ～)."""
    s = token.strip()
    return len(s) == 1 and not s.isalnum()


def _spread_letter(token: str) -> bool:
    """균등 배분으로 한 글자씩 떨어진 글자(한글·영문)."""
    s = token.strip()
    return len(s) == 1 and s.isalpha()


def _spread_syllable(fragment: str) -> bool:
    """균등 배분으로 한 글자씩 떨어진 한글(구 분, 합 계)의 조각."""
    s = fragment.strip()
    return len(s) == 1 and "가" <= s <= "힣"


def _drop_nested(t):
    """다른 칸 안에 통째로 든 칸(칸 안의 점선 상자)은 뺀다. 바깥 칸의 글은 하위 행 나누기가 줄로 가른다."""
    from pdfplumber.table import Table as PlumberTable

    cells = t.cells
    near = layout.Bins(cells, lambda o: o[0], lambda o: o[2])
    inside = [c for c in cells if any(o is not c and o[0] <= c[0] + 0.5 and o[1] <= c[1] + 0.5 and c[2] <= o[2] + 0.5
                                      and c[3] <= o[3] + 0.5 and o != c for o in near.around(c[0] - 0.5, c[0] + 0.5))]
    return PlumberTable(t.page, [c for c in cells if c not in inside]) if inside else t


def _rejoin_blocks(entries: List[Entry], whole: Dict[int, Tuple[Entry, int]]) -> List[Entry]:
    """행으로 쪼갰는데 한 행도 표가 되지 않은 블록은 원래 블록 하나로 되돌린다."""
    out: List[Entry] = []
    i = 0
    while i < len(entries):
        j = entries[i].get("join")
        if j is None:
            out.append(entries[i])
            i += 1
            continue
        k = i
        while k < len(entries) and entries[k].get("join") == j:
            k += 1
        out.extend([whole[j][0]] if k - i == whole[j][1] else entries[i:k])
        i = k
    return out


def _unruled_header(page, entries: List[Entry]) -> List[Entry]:
    """선 없는 표 바로 위 한 줄이 낱말 단위로 열마다 갈리면 머리행이다.

    머리 글 사이 틈이 띄어쓰기만큼 좁아 칸으로 안 갈린 줄. 열마다 첫 낱말이 앞 열 글이 끝난 뒤, 그 열 시작 근처에서
    시작해야 한다(표 위 제목·설명 문장은 낱말이 열 경계와 상관없이 놓인다).
    """
    out: List[Entry] = []
    for e in entries:
        prev = out[-1] if out else None
        cols = e.get("columns")
        if cols and prev and "grid" not in prev and "table" not in prev and prev.get("text") and "\n" not in prev["text"]:
            head = _header_words(page, prev["bbox"], cols, e)
            if head:
                out.pop()
                pb, eb = prev["bbox"], e["bbox"]
                e = {**e, "key": prev["key"], "grid": [head] + e["grid"],
                     "bbox": [min(pb[0], eb[0]), pb[1], max(pb[2], eb[2]), eb[3]]}
        out.append(e)
    return out


def _header_words(page, bbox, cols, table: Entry) -> Optional[List[str]]:
    h = table["height"]
    x0, top, x1, bottom = bbox
    tb = table["bbox"]
    if not (0 <= tb[1] - bottom <= table["pitch"] and bottom - top <= 1.5 * h and abs(x0 - cols[0][0]) <= h):
        return None
    chars = sorted((c for c in page.chars if c.get("text") and x0 - 0.5 <= (c["x0"] + c["x1"]) / 2 <= x1 + 0.5
                    and top - 0.5 <= (c["top"] + c["bottom"]) / 2 <= bottom + 0.5), key=lambda c: c["x0"])
    words: List[List[Any]] = []  # [x0, 글]
    for c in chars:
        if not c["text"].strip():
            words.append(None)
        elif words and words[-1] is not None and c["x0"] - words[-1][2] <= _SPACE_CHAR_RATIO * 4 * c["size"]:
            words[-1][1] += c["text"]
            words[-1][2] = c["x1"]
        else:
            words.append([c["x0"], c["text"], c["x1"]])
    words = [w for w in words if w]
    head: List[List[str]] = [[] for _ in cols]
    for x, text, _ in words:
        k = max((k for k, col in enumerate(cols) if col[0] <= x + h), default=0)
        if not head[k] and k and not (cols[k - 1][1] <= x and abs(x - cols[k][0]) <= h):
            return None
        head[k].append(text)
    return [" ".join(p) for p in head] if all(head) else None


def _join_header_rows(entries: List[Entry]) -> List[Entry]:
    """선(채움 칸)으로 그린 머리행 하나 바로 아래 같은 칸 수의 선 없는 본문이 이어지면 한 표다."""
    out: List[Entry] = []
    for e in entries:
        prev = out[-1] if out else None
        if prev is not None and e.get("unruled") and "table" in prev and not prev.get("unruled") \
                and prev["table"].n_rows == 1 and prev["table"].n_cols == e["table"].n_cols:
            pb, eb = prev["bbox"], e["bbox"]
            if 0 <= eb[1] - pb[3] <= 2 * (pb[3] - pb[1]) and min(pb[2], eb[2]) > max(pb[0], eb[0]):
                prev["table"] = _grid_table(prev["table"].grid() + e["table"].grid())
                prev["bbox"] = [min(pb[0], eb[0]), pb[1], max(pb[2], eb[2]), eb[3]]
                continue
        out.append(e)
    return out


def _page_entries(page, lines: List[layout.Line], margins: Set[str], page_h: float,
                  spaced: bool = False) -> List[Entry]:
    found = []
    sizes = sorted(c["size"] for c in page.chars if c.get("text", "").strip())
    size = sizes[len(sizes) // 2] if sizes else 10.0
    for t in _find_tables(page, spaced):
        table = _plumber_table(t, spaced, size)
        if table and not _not_a_table(table):
            found.append((list(t.bbox), split_subrows(page, t, table), [c for c in t.cells if c]))
    for x in _monospace_tables(page):  # 타자기식 표: 선도 칸 간격도 없이 고정폭 글자로 줄 맞춘 숫자 열
        if not any(_center_inside(tb, x["bbox"]) or _center_inside(x["bbox"], tb) for tb, _, _ in found):
            found.append((x["bbox"], _grid_table(x["rows"]), [x["bbox"]]))
    # 줄 단위로 표의 몫을 가른다: 가운데가 표 안인 줄만 표의 것(표 밑 각주·제목 줄은 살린다).
    # 표는 처음 걸린 줄 자리에 들어가고, 블록은 표를 건너 묶지 않는다
    entries: List[Entry] = []
    placed: Set[int] = set()
    run: List[layout.Line] = []

    whole: Dict[int, Tuple[Entry, int]] = {}  # 행으로 쪼갠 블록 → (원래 항목, 행 수)

    def flush():
        for block in layout.blocks_from_lines(run):
            bbox, text = block.bbox, block.text
            if text and not (margins and _margin_key(bbox, text, page_h) in margins):
                cells = layout.line_cells(block)
                rows = None if cells else layout.block_rows(block)
                tab = None if cells or rows else layout.tab_rows(block)
                rows = rows or tab
                tail = None if cells or rows else layout.trailing_row(block)
                if tail:  # 표 제목 문단에 붙은 머리행: 떼어 두고, 표가 안 되면 아래에서 되돌린다
                    whole[id(block)] = ({"key": float(len(entries)), "text": text, "bbox": bbox, "cells": None}, 2)
                    head, row = tail
                    entries.append({"key": float(len(entries)), "text": head.text, "bbox": head.bbox, "cells": None,
                                    "join": id(block)})
                    rb = [min(c["bbox"][0] for c in row), min(c["bbox"][1] for c in row),
                          max(c["bbox"][2] for c in row), max(c["bbox"][3] for c in row)]
                    entries.append({"key": float(len(entries)), "text": " ".join(c["text"] for c in row), "bbox": rb,
                                    "cells": row, "join": id(block)})
                    continue
                if not rows:
                    entries.append({"key": float(len(entries)), "text": text, "bbox": bbox, "cells": cells})
                    continue
                # 한 블록에 묶인 표 행들: 행마다 항목으로 내고, 표가 안 되면 아래에서 원래 블록으로 되돌린다
                whole[id(block)] = ({"key": float(len(entries)), "text": text, "bbox": bbox, "cells": None}, len(rows))
                for r in rows:
                    rb = [min(c["bbox"][0] for c in r), min(c["bbox"][1] for c in r),
                          max(c["bbox"][2] for c in r), max(c["bbox"][3] for c in r)]
                    entries.append({"key": float(len(entries)), "text": " ".join(c["text"] for c in r), "bbox": rb,
                                    "cells": r, "join": id(block), "tab": bool(tab)})
        run.clear()

    cell_bins = [layout.Bins(cells, lambda c: c[1] - 1, lambda c: c[3] + 1) for _, _, cells in found]
    for line in lines:
        # 표 테두리 안이어도 어느 칸에도 안 든 줄(표 틀 안 각주 등)은 표가 담지 않으니 글로 둔다
        cy = (line.bbox[1] + line.bbox[3]) / 2
        owner = next((k for k, (tb, _, cells) in enumerate(found)
                      if _center_inside(line.bbox, tb) and any(_center_inside(line.bbox, c) for c in cell_bins[k].around(cy))),
                     None)
        if owner is None:
            run.append(line)
        elif owner not in placed:
            flush()
            placed.add(owner)
            entries.append({"key": float(len(entries)), "table": found[owner][1], "bbox": found[owner][0],
                            "cells": None})
    flush()
    entries = _unruled_header(page, _rejoin_blocks(layout.rebuild_unruled(entries), whole))
    for e in entries:
        if "grid" in e:
            e["table"] = _grid_table(e.pop("grid"))
            e["unruled"] = True
    entries = _join_header_rows(entries)
    for k, (tb, table, _) in enumerate(found):
        if k not in placed:
            entries.append({"key": place_key(entries, tb), "table": table, "bbox": tb})
    entries = _reading_order(entries)
    for k, e in enumerate(entries):
        e["key"] = float(k)
    return entries


def _patch_pdfminer() -> None:
    """ICC 색 공간 스트림에 /N(성분 수)이 빠진 PDF 에서 pdfminer 가 쪽 전체를 포기한다. 없으면 3(RGB)으로 본다."""
    from pdfminer import pdftypes

    if getattr(pdftypes.PDFStream.__getitem__, "_pgdp_patched", False):
        return
    original = pdftypes.PDFStream.__getitem__

    def getitem(self, name):
        if name == "N" and "N" not in self.attrs:
            return 3
        return original(self, name)

    getitem._pgdp_patched = True
    pdftypes.PDFStream.__getitem__ = getitem
    _patch_glyph_names()


def _patch_glyph_names() -> None:
    """ToUnicode 가 U+FFFD 로 적은 글자는 인코딩의 글리프 이름(two.SP → 2)으로 풀고, 작은 대문자 글리프(.smcp·.c2sc)는
    보이는 대로 대문자로 낸다."""
    from pdfminer import pdffont
    from pdfminer.pdftypes import list_value, resolve1
    from pdfminer.psparser import literal_name

    font = pdffont.PDFSimpleFont
    if getattr(font.to_unichr, "_pgdp_patched", False):
        return
    init0 = font.__init__

    def init(self, descriptor, widths, spec):
        init0(self, descriptor, widths, spec)
        self._pgdp_caps = set()
        enc = resolve1(spec.get("Encoding")) if "Encoding" in spec else None
        if isinstance(enc, dict):
            cid = 0
            for x in list_value(enc.get("Differences", [])):
                if isinstance(x, int):
                    cid = x
                    continue
                if literal_name(x).endswith((".smcp", ".c2sc")):
                    self._pgdp_caps.add(cid)
                cid += 1

    def to_unichr(self, cid):
        u = None
        if self.unicode_map:
            try:
                u = self.unicode_map.get_unichr(cid)
            except KeyError:
                pass
        if u is None or u == "\ufffd":
            try:
                u = self.cid2unicode[cid]
            except KeyError as err:
                if u is None:
                    raise pdffont.PDFUnicodeNotDefined(None, cid) from err
        return u.upper() if cid in getattr(self, "_pgdp_caps", ()) else u

    to_unichr._pgdp_patched = True
    font.__init__, font.to_unichr = init, to_unichr


# 글자·선을 읽는 엔진. pdfminer 는 비교·대체용(근거: docs/VERIFICATION.md "pdfium 엔진")
ENGINE = "pdfium"


def _load_pdfium(data: bytes, pages) -> None:
    """선·사각형·곡선은 pdfium 으로, 글자·그림은 경로 명령을 걷어 낸 스트림으로 pdfminer 가 읽는다.
    돌린 쪽이나 pdfium 이 못 읽는 쪽은 pdfminer 가 전부 읽는다."""
    import pypdfium2 as pdfium

    from . import pdf_objects

    try:
        doc = pdfium.PdfDocument(data)
    except Exception:  # 암호·손상은 pdfminer 쪽 오류로 낸다
        return
    try:
        for i, page in enumerate(pages):
            if page.rotation % 360:
                continue
            try:
                drawn = pdf_objects.paths(doc, page, i)
                clips = pdf_objects.clip_rects(page.page_obj, float(page.height))
            except Exception:
                continue
            pdf_objects.strip_paths(page.page_obj)
            objects = page.parse_objects()
            objects.update(drawn)
            page._objects, page.page_obj._pgdp_rects = objects, clips
    finally:
        doc.close()


def _visible(page):
    """쪽 밖에 놓여 보이지 않는 글자를 뺀 쪽(펼친 면에서 잘라 낸 쪽은 옆 쪽 글이 쪽 밖에 남아 있다)."""
    x0, top, x1, bottom = page.bbox

    def inside(c):
        cx, cy = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        return x0 <= cx <= x1 and top <= cy <= bottom

    if all(inside(c) for c in page.chars):
        return page
    return page.filter(lambda o: o.get("object_type") != "char" or inside(o))


def parse(data: bytes, ocr: Optional[Ocr] = None, max_workers: int = 4, images: bool = True) -> Document:
    """images: 그림 영역을 렌더해 그림 블록으로 낸다(그림 설명을 받을 때만 의미가 있다)."""
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    _patch_pdfminer()

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
        if ENGINE == "pdfium":
            _load_pdfium(data, pdf.pages)
        pages = [_visible(p) for p in pdf.pages]
    except PDFPasswordIncorrect as exc:
        raise EncryptedDocumentError("암호가 걸린 PDF") from exc
    except Exception as exc:
        raise ParseError(f"PDF 를 열지 못했다: {exc}") from exc

    renderer = _Renderer(data) if (images or ocr) else None
    doc = Document(format="pdf", page_sizes=[])
    try:
        # 1차: 쪽마다 글 블록, 그림 자리
        page_blocks: List[Tuple[float, List[layout.TextBlock]]] = []
        page_lines: List[List[layout.Line]] = []
        page_rects: List[List[List[float]]] = []
        for page in pages:
            layout.clean_chars(page.chars)
        # 공백을 글자로 담는지는 문서 단위로 본다(숫자표뿐인 쪽은 공백 글자가 거의 없다)
        spaced = encodes_spaces([c for page in pages for c in page.chars])
        for page in pages:
            lines = layout.lines_from_chars(page.chars, infer_spaces=not spaced)
            page_lines.append(lines)
            page_blocks.append((float(page.height), layout.blocks_from_lines(lines)))
            page_rects.append(_image_rects(page))
        margins = _repeated_margins(page_blocks)
        boiler = _boilerplate(page_rects)

        # 2차: 쪽 조립 + 받아쓰기 작업 등록
        tasks: List[Tuple[str, Callable[[], str]]] = []
        page_entries: List[List[Entry]] = []
        for pno, page in enumerate(pages):
            w, h = float(page.width), float(page.height)
            doc.page_sizes.append((w, h))
            blocks = page_blocks[pno][1]
            garbled = garbled_text(" ".join(b.text for b in blocks))
            entries = [] if garbled else _page_entries(page, page_lines[pno], margins, h, spaced)
            if not entries:
                needs_ocr = garbled or page_rects[pno] or page.rects or page.curves or page.lines
                if ocr and needs_ocr:
                    png = renderer.png(pno) if renderer else None
                    if png:
                        key = f"ocr:{pno}"
                        tasks.append((key, (lambda b=png: ocr(b))))
                        entries = [{"key": 0.0, "ocr_key": key, "bbox": [0.0, 0.0, w, h], "page_ocr": True}]
            elif renderer and (images or ocr):
                rects = [r for r in page_rects[pno] if _signature(r) not in boiler]
                for k, reg in enumerate(_figure_regions(rects, w, h)):
                    as_text = ocr is not None and reg["coverage"] >= _REGION_OCR_COVERAGE
                    if not (images or as_text):
                        continue
                    png = renderer.png(pno, reg["bbox"])
                    if not png:
                        continue
                    entry = {"key": place_key(entries, reg["bbox"]), "bbox": reg["bbox"]}
                    if as_text:
                        key = f"reg:{pno}:{k}"
                        tasks.append((key, (lambda b=png: ocr(b))))
                        entry["ocr_key"] = key
                    else:
                        entry["image"] = Image(id=f"p{pno + 1}-r{k + 1}", data=png, mime="image/png")
                    entries.append(entry)
            page.flush_cache()
            page_entries.append(entries)

        results = run_parallel(tasks, max_workers) if tasks else {}

        # 3차: 블록으로
        prev_table: Optional[Table] = None
        for pno, entries in enumerate(page_entries):
            if any(e.get("page_ocr") for e in entries):
                prev_table = None
            else:
                entries, prev_table = carry_table_header(entries, prev_table)
            for e in sorted(entries, key=lambda e: e["key"]):
                bbox = tuple(e["bbox"])
                if "ocr_key" in e:
                    text = results.get(e["ocr_key"], "")
                    if text:
                        doc.blocks.append(Block("paragraph", text=text, page=pno, bbox=bbox, ocr=True))
                elif "table" in e:
                    doc.blocks.append(Block("table", table=e["table"], page=pno, bbox=bbox))
                elif "image" in e:
                    doc.blocks.append(Block("image", image=e["image"], page=pno, bbox=bbox))
                else:
                    doc.blocks.append(Block("paragraph", text=e["text"], page=pno, bbox=bbox))
    finally:
        pdf.close()
        if renderer:
            renderer.close()
    return doc


def _boilerplate(page_rects: List[List[List[float]]]) -> Set[Tuple[int, ...]]:
    """여러 쪽 같은 자리에 되풀이되는 그림(로고·워터마크)의 위치 서명."""
    total = len(page_rects)
    if total < _BOILERPLATE_MIN_DOC_PAGES:
        return set()
    seen: Dict[Tuple[int, ...], Set[int]] = {}
    for pno, rects in enumerate(page_rects):
        for r in rects:
            seen.setdefault(_signature(r), set()).add(pno)
    need = max(_BOILERPLATE_MIN_PAGES, int(total * _BOILERPLATE_PAGE_RATIO))
    return {sig for sig, ps in seen.items() if len(ps) >= need}
