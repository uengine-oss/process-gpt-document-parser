"""가로선 없이 한 행에 묶인 하위 행을 글자 줄로 나눈다(find_tables 결과 후처리).

한컴 통계표는 하위 행 사이에 선이 없어 find_tables 가 수십 행을 한 칸에 담는다. 값은 남지만 행 머리와의 짝이
사라져 LLM 이 몇 번째 줄인지 세다 틀린다. 근거·측정은 process-gpt-memento docs/DESIGN_NOTES.md "선 없는 하위 행".
"""
from __future__ import annotations

import re
from typing import List, Optional

from ..model import Cell, Table
from .pdf_layout import Bins

_NUM = re.compile(r"^[-−–+△▲▽▼↑↓]?\(?[-−–+△▲▽▼↑↓]?[$€£¥₩]?[\d][\d.,]*%?\)?[a-zA-Z*]{0,2}$")
_BAND = 0.5  # 줄 높이의 이 비율보다 가까운 글자는 같은 줄
_PROSE = 40  # 칸 한 줄이 이보다 길면 문단


def _is_num(s: str) -> bool:
    # "220 (31.3)" 처럼 비율을 붙인 값도 값이다
    return bool(_NUM.match(s.strip().split(" (")[0]))


def _col_edges(tab) -> List[float]:
    xs = sorted({round(c[0], 1) for row in tab.rows for c in row.cells if c})
    return xs + [tab.bbox[2]]


def _chars(page, clip) -> List[tuple]:
    """(x0, y0, x1, y1, 글자). 이름과 값 사이에 공백 문자가 없는 PDF 가 있어 단어가 아니라 글자로 칸을 나눈다."""
    out = []
    for c in page.chars:
        x, y = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        if c["text"].strip() and clip[0] <= x <= clip[2] and clip[1] <= y <= clip[3]:
            out.append((c["x0"], c["top"], c["x1"], c["bottom"], c["text"]))
    return out


def _bands(chars) -> List[list]:
    """같은 줄(세로 중심이 가까운) 글자 묶음, 위에서 아래로."""
    chars = sorted(chars, key=lambda w: (w[1] + w[3]) / 2)
    bands: List[list] = []  # [중심, 높이, 글자들]
    for w in chars:
        mid, h = (w[1] + w[3]) / 2, w[3] - w[1]
        # 위첨자(2024ᵖ)는 중심이 조금 떠 있어 줄 높이는 그 줄에서 가장 큰 글자로 본다
        if bands and abs(mid - bands[-1][0]) <= _BAND * max(h, bands[-1][1], 1.0):
            bands[-1][2].append(w)
            bands[-1][1] = max(bands[-1][1], h)
        else:
            bands.append([mid, h, [w]])
    return [b[2] for b in bands]


def _tokens(chars, edges) -> List[tuple]:
    """한 줄 글자를 보이는 간격(글자 높이의 1/4 초과)으로 끊은 (x0, x1, 글).
    칸 경계를 넘을 때는 조금만 벌어져도 끊는다(좁은 칸에 오른쪽 정렬한 이웃 숫자). 붙어서 넘친 글은 한 덩어리.
    edges 는 이 줄의 행에 실제로 있는 칸 경계다. 다른 행에만 있는 경계(여기서는 병합 칸 안)로는 끊지 않는다."""
    out: List[list] = []
    for w in sorted(chars, key=lambda w: w[0]):
        gap = w[0] - out[-1][1] if out else 0
        crosses = out and any(out[-1][1] - 0.5 <= e <= w[0] + 0.5 for e in edges[1:-1])
        if out and gap <= 0.25 * (w[3] - w[1]) and not (crosses and gap > 0.5):
            out[-1][1] = max(out[-1][1], w[2])
            out[-1][2] += (" " if gap > 0.12 * (w[3] - w[1]) else "") + w[4]  # 낱말 사이 틈은 띄운다(Head Start)
        else:
            out.append([w[0], w[2], w[4]])
    return [tuple(t) for t in out]


def _chars_by_row(rows, chars) -> List[list]:
    """글자마다 그 글자를 감싸는 가장 작은 칸의 행에 배정한다. 병합 칸·자잘한 행이 겹쳐도 한 글자는 한 행에만."""
    cells = [(r, c) for r, row in enumerate(rows) for c in row.cells if c]
    near = Bins(cells, lambda rc: rc[1][1], lambda rc: rc[1][3])
    out: List[list] = [[] for _ in rows]
    for w in chars:
        xc, yc = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        best = None
        for r, c in near.around(yc):
            if c[0] <= xc <= c[2] and c[1] <= yc <= c[3]:
                area = (c[2] - c[0]) * (c[3] - c[1])
                if best is None or area < best[0]:
                    best = (area, r)
        if best:
            out[best[1]].append(w)
    return out


def _runs(idx: List[int]) -> List[List[int]]:
    """연달아 붙은 줄 번호끼리 묶는다."""
    out: List[List[int]] = []
    for i in idx:
        if out and out[-1][-1] == i - 1:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def _nearest(i: int, runs: List[List[int]], mids: List[float]) -> List[int]:
    """줄 i 에 세로로 가장 가까운 묶음. 두 값 줄 가운데 찍힌 이름은 반 줄, 위 행 이름은 한 줄 떨어져 있다.
    거리가 같으면 위 묶음."""
    return min(runs, key=lambda run: (round(min(abs(mids[i] - mids[j]) for j in run)), run[0] > i))


def _row_edges(band, edges, row_cells) -> List[float]:
    """이 줄이 든 행의 칸 경계만 남긴 edges. row_cells 는 행마다 (top, bottom, 칸 x 경계들)."""
    mid = sum((w[1] + w[3]) / 2 for w in band) / len(band)
    xs = [x for top, bottom, cxs in row_cells if top <= mid <= bottom for x in cxs]
    if not xs:
        return edges
    return [edges[0]] + [e for e in edges[1:-1] if any(abs(e - x) <= 1.0 for x in xs)] + [edges[-1]]


def _split_row(inside, edges, n_cols, row_cells=None) -> Optional[List[List[str]]]:
    bands = _bands(inside)
    if len(bands) < 2:
        return None
    col_of = lambda x: max(0, min(n_cols - 1, sum(1 for e in edges[1:-1] if x >= e)))
    grid = []
    for band in bands:
        cells = [[] for _ in range(n_cols)]
        own = _row_edges(band, edges, row_cells) if row_cells else edges
        tokens = _tokens(band, own)
        for t in tokens:
            xc = (t[0] + t[1]) / 2
            lo = max([e for e in own if e <= xc], default=edges[0])
            hi = min([e for e in own if e > xc], default=edges[-1])
            span = range(col_of(lo + 0.5), col_of(hi - 0.5) + 1)
            # 병합 이름 칸에 글 덩어리가 하나뿐이면 걸친 열마다 채운다(병합 칸 값을 채우는 표 출력과 같게). 값은 제 열에만
            alone = (len(span) > 1 and not _is_num(t[2])
                     and sum(1 for u in tokens if lo <= (u[0] + u[1]) / 2 < hi) == 1)
            for c in (span if alone else [col_of(xc)]):
                cells[c].append(t[2])
        grid.append([" ".join(c) for c in cells])
    # 각주 표시만 든 칸(위첨자 *)은 따로 행이 아니다. 위첨자는 제 줄보다 떠 있어 아래 줄 것이다. "-"(해당 없음)는 값이다
    for r in range(len(grid) - 1):
        for c, text in enumerate(grid[r]):
            if text and set(text) <= set("*†‡") and any(grid[r + 1]):
                grid[r + 1][c] += text
                grid[r][c] = ""
    # 값 아래 (비율)·(0.8배)를 쌓은 칸: 괄호는 위 줄 값 밑에만, 다른 글(두 줄 가운데 찍힌 4세)은 위 줄 빈칸에만 있으면
    # 위 줄의 부속이다 — "값 (비율)" 로 붙인다. 위 줄 이름 밑에 제 이름이 있으면(총투자/증감률) 제 행
    # 다만 어느 열에서든 두 줄 사이에 칸 경계(선·클립)가 있어 다른 행에 들면 붙이지 않는다(값 행 | (구성비) 행)
    tops = sorted({round(top, 1) for top, _, _ in row_cells or []})
    row_of = lambda band: sum(1 for t in tops if t <= sum((w[1] + w[3]) / 2 for w in band) / len(band) + 0.5)
    merged: List[List[str]] = []
    mids: List[float] = []  # 줄의 세로 위치 — 이름을 가까운 값 줄에 붙일 때 쓴다
    last_row = None
    for g, band in zip(grid, bands):
        if not any(g):
            continue
        paren = [bool(c) and c.startswith("(") and c.endswith(")") for c in g]
        fits = (merged and sum(_is_num(c) for c in merged[-1] if c) >= 2 and row_of(band) == last_row
                and all((bool(a) if p else not a) for a, b, p in zip(merged[-1], g, paren) if b))
        last_row = row_of(band)
        # 두 줄 가운데 찍힌 값(주민등록 유아 수 70,251)도 위 줄 빈칸에만 있으면 같은 행이다
        if fits and (any(paren) or any(_is_num(c) for c in g if c)):
            merged[-1] = [f"{a} {b}".strip() for a, b in zip(merged[-1], g)]
        else:
            merged.append(g)
            mids.append(sum((w[1] + w[3]) / 2 for w in band) / len(band))
    grid = merged
    # 값 줄이 둘 이상이어야 하위 행이다. 머리 칸의 줄바꿈(확정치/(A))은 그대로 둔다
    is_val = [sum(_is_num(c) for c in g if c) >= 2 for g in grid]
    vrows = [g for g, v in zip(grid, is_val) if v]
    if len(vrows) < 2:
        return None
    # 값 열은 값 줄마다 숫자가 차 있는 열. 그 앞(연도처럼 묶음 첫 줄에만 적힌 이름)은 위 값을 잇는다
    # 이름 열: 값 줄 절반 이상에 숫자 아닌 이름(1/4, 전월비)이 있는 열과, 숫자가 드문 앞 열(연도).
    # 순위 표처럼 이름 | 값 | 이름 | 값 이 번갈아 와도 된다
    named = {c for c in range(n_cols) if sum(bool(g[c]) and not _is_num(g[c]) for g in vrows) >= 0.5 * len(vrows)}
    first_num = next((c for c in range(n_cols) if c not in named
                      and sum(_is_num(g[c]) for g in vrows) >= 0.5 * len(vrows)), n_cols)
    labels = sorted(named | set(range(first_num)))
    values = [c for c in range(n_cols) if c not in labels]
    for c in labels:
        parts = [g[c] for g in grid if g[c]]
        # 세로쓰기 이름(생/산)은 한 글자씩 줄에 걸쳐 있다 — 합쳐서 모든 하위 행에
        if len(parts) >= 2 and all(len(p) == 1 and "가" <= p <= "힣" for p in parts):  # 한 자리 값(9, 8)은 이름이 아니다
            for g in grid:
                g[c] = "".join(parts)
    # 값 없이 이름만 있는 줄: 여러 줄 이름(담배제품/현재/사용률), 두 값 줄 사이 가운데 이름(백화점),
    # 묶음 이름(서비스업). 이어진 이름 줄을 한 이름으로 묶고, 값 줄마다 가장 가까운 이름을 붙인다
    lab_idx = [i for i, (g, v) in enumerate(zip(grid, is_val)) if not v and not any(g[c] for c in values)]
    val_idx = [i for i, v in enumerate(is_val) if v]
    # 이름 줄이 값 줄만큼 있으면 하위 행이 아니라 줄바꿈된 이름 칸 옆에 값/(비중)을 쌓은 칸이다.
    # 첫 값 줄 위에 몰린 이름 줄(묶음 소제목)은 세지 않는다
    lead = sum(1 for i in lab_idx if val_idx and i < val_idx[0])
    # 다만 값 줄마다 제 이름이 같은 줄에 있거나, 위아래 이름 줄 사이 가운데 찍혔으면(두 줄 이름 옆 가운데 값) 하위 행이다
    labset = set(lab_idx)
    mid_of = lambda i: abs(mids[i] - (mids[i - 1] + mids[i + 1]) / 2) <= 0.25 * (mids[i + 1] - mids[i - 1])
    centered = len(lab_idx) - lead >= len(val_idx) and \
        all(any(grid[i][c] for c in labels) or (i - 1 in labset and i + 1 in labset and mid_of(i)) for i in val_idx)
    if len(lab_idx) - lead >= len(val_idx) and not centered:
        return None
    standalone = set()  # 제 행으로 남길 이름 줄
    if lab_idx:
        for c in labels:
            texts = [g[c] for g in grid]
            if not any(texts[i] for i in lab_idx):
                continue
            if centered and not all(texts[i] for i in val_idx):  # 가운데 값: 바로 위아래 이름 줄이 그 값의 이름
                for i in val_idx:
                    if not texts[i] and i - 1 in labset and i + 1 in labset:
                        grid[i][c] = f"{texts[i - 1]} {texts[i + 1]}".strip()
                continue
            if all(texts[i] for i in val_idx):  # 값 줄마다 제 이름이 있으면 바로 옆 묶음 이름(항공 → 국내·국제)만 앞에
                # 첫 값 줄 위에 몰린 이름 줄(묶음 소제목 All Randomly Assigned / 3-Year-Old Cohort)은 붙이지 않고 제 행으로
                lead = [i for i in lab_idx if texts[i] and i < val_idx[0]]
                # 값 줄과 한 줄 간격만큼 떨어진 이름 줄은 묶음 제목 행(Sex / Race / Ethnicity)이다. 값 줄 사이 가운데 찍힌 것만 묶음 이름
                pitch = sorted(b - a for a, b in zip(mids, mids[1:]))[len(mids) // 2 - 1] if len(mids) >= 3 else 0
                lead += [i for i in lab_idx if texts[i] and i not in lead and pitch
                         and min(abs(mids[i] - mids[j]) for j in val_idx) >= 0.75 * pitch]
                standalone.update(lead)
                runs = _runs([i for i in lab_idx if texts[i] and i not in lead])
                for i in (val_idx if runs else []):
                    run = _nearest(i, runs, mids)
                    if min(abs(i - j) for j in run) == 1:
                        grid[i][c] = f"{' '.join(texts[j] for j in run)} {texts[i]}"
            elif sum(bool(texts[i]) for i in val_idx) >= 0.5 * len(val_idx):
                # 값 줄 대부분에 제 이름이 있다(행마다 이름): 빈 값 줄은 줄바꿈된 이름(만성 하기도/질환)이 위아래에 걸친 꼴.
                # 드물면 여러 행에 걸친 이름(65세/이상)이라 아래처럼 묶는다.
                # 이름만 있는 줄 묶음을 가장 가까운 값 줄 하나에 붙인다(거리가 같으면 위 값 줄, 아래 빈 줄은 위를 잇는다)
                own = {i: [i] if texts[i] else [] for i in val_idx}
                for run in _runs([i for i in lab_idx if texts[i]]):
                    i = min(val_idx, key=lambda i: (round(min(abs(mids[i] - mids[j]) for j in run)), i > run[0]))
                    own[i] += run
                for i in val_idx:
                    grid[i][c] = " ".join(texts[j] for j in sorted(own[i]))
            else:
                runs = _runs([i for i, t in enumerate(texts) if t])
                for i in val_idx:
                    grid[i][c] = " ".join(texts[j] for j in _nearest(i, runs, mids))
        grid = [g for i, g in enumerate(grid) if i not in lab_idx or i in standalone or any(g[c] for c in values)]
    for r in range(1, len(grid)):
        for c in labels:
            if not grid[r][c] and any(grid[r][v] for v in values):
                grid[r][c] = grid[r - 1][c]
    return grid if _regular(grid, values) else None


def _aligned_rows(inside, edges, n_cols) -> Optional[List[List[str]]]:
    """값이 숫자가 아닌 하위 행($9,594 or less | Greater than $25,771): 줄마다 같은 열들(둘 이상, 첫 열 포함)에 글이 있으면
    줄 하나가 행 하나다. 한 열만 줄바꿈된 칸(이름 두 줄)이 있으면 줄마다 채운 열이 달라 나누지 않는다."""
    bands = _bands(inside)
    if len(bands) < 2:
        return None
    col_of = lambda x: max(0, min(n_cols - 1, sum(1 for e in edges[1:-1] if x >= e)))
    grid, shapes = [], set()
    for band in bands:
        cells = [[] for _ in range(n_cols)]
        for t in _tokens(band, edges):
            cells[col_of((t[0] + t[1]) / 2)].append(t[2])
        shapes.add(tuple(bool(c) for c in cells))
        grid.append([" ".join(c) for c in cells])
    shape = shapes.pop() if len(shapes) == 1 else None
    return grid if shape and shape[0] and sum(shape) >= 2 else None


def _text_rows(inside, edges, n_cols) -> Optional[List[List[str]]]:
    """글 값 표(이름 | 설명 문장): 줄마다 행이고, 모든 칸이 소문자로 시작하는 줄은 위 행의 이어진 줄이다(접힌 이름·설명).
    이름 칸과 다른 칸이 함께 찬 행이 셋 이상일 때만. 줄마다 왼쪽 칸만 찬 행(묶음 제목)은 제 행으로 둔다."""
    bands = _bands(inside)
    if len(bands) < 3:
        return None
    col_of = lambda x: max(0, min(n_cols - 1, sum(1 for e in edges[1:-1] if x >= e)))
    grid: List[List[str]] = []
    for band in bands:
        cells = [[] for _ in range(n_cols)]
        for t in _tokens(band, edges):
            cells[col_of((t[0] + t[1]) / 2)].append(t[2])
        row = [" ".join(c) for c in cells]
        filled = [c for c in row if c]
        if grid and filled and all(c[0].islower() for c in filled):
            grid[-1] = [f"{a} {b}".strip() for a, b in zip(grid[-1], row)]
        else:
            grid.append(row)
    full = sum(1 for g in grid if g[0] and any(g[1:]))
    return grid if full >= 3 and all(g[0] or not any(g[1:]) for g in grid) else None


_GROUPED = re.compile(r"^[-−–+△▲▽▼↑↓(]*[$€£¥₩]?\d{1,3}(,\d{3})+(\.\d+)?[)%*a-zA-Z]*$")


def _regular(grid: List[List[str]], values: List[int]) -> bool:
    """나눈 결과가 반듯한 격자일 때만 쓴다. 아니면 원래 칸(<br> 묶음)이 낫다.
    값 줄마다 값 칸 수가 비슷하고, 한 칸에 숫자 하나("값 (비율)" 은 허용), 쉼표 묶음이 세 자리여야 한다."""
    vals = [[g[c] for c in values] for g in grid if sum(_is_num(c) for c in g if c) >= 2]
    filled = [sum(bool(c) for c in v) for v in vals]
    # 값을 비운 행(‡ 표시) 몇 개는 괜찮다. 값 줄 대부분이 비슷하게 차 있어야 격자다
    if not vals or sum(f < 0.6 * max(filled) for f in filled) > 0.25 * len(vals):
        return False
    for v in vals:
        for cell in v:
            head = cell.split(" (")[0]
            # "2024. 8"(연. 월)은 값 하나다
            if sum(_is_num(t) and not t.endswith(".") for t in head.split()) > 1:
                return False
            if "," in head and _is_num(head) and not _GROUPED.match(head):
                return False
    return True


def _row_groups(table: Table, n_rows: int) -> List[List[int]]:
    """머리행(0) 아래 행들을, 여러 행에 걸친 칸으로 이어진 묶음으로. 한 묶음이 하위 행을 나눌 단위다.

    한 열만 행마다 선이 있으면(값 칸) find_tables 는 그 선마다 행을 만들고, 다른 열의 큰 칸은 그 행들에 걸친다.
    """
    spans = sorted((max(c.row, 1), c.row + c.row_span - 1) for c in table.cells if c.row + c.row_span - 1 >= 1)
    groups: List[List[int]] = []
    end = 0
    for start, stop in spans:
        if groups and start <= end:
            end = max(end, stop)
        else:
            groups.append([start])
            end = stop
        groups[-1] = list(range(groups[-1][0], min(end, n_rows - 1) + 1))
    return groups


def _respan(table: Table, grid: List[List[str]], src: List[Optional[int]]) -> Table:
    """나눈 표에 원래 병합 칸을 되살린다. 병합이 걸친 원래 행이 하위 행으로 나뉘지 않았으면 그 병합 그대로
    (글자 없는 복사본 행이 빠진 만큼 줄여), 나뉜 행에 걸친 병합은 칸마다 채운 글로 둔다."""
    new = {r: i for i, r in enumerate(src) if r is not None}
    cells, covered = [], set()
    for c in table.cells:
        if c.row not in new or (c.row_span == 1 and c.col_span == 1):
            continue
        first = new[c.row]
        last = max((new[r] for r in range(c.row, c.row + c.row_span) if r in new), default=first)
        if any(src[i] is None for i in range(first, last + 1)):
            continue
        cells.append(Cell(c.text, first, c.col, last - first + 1, c.col_span))
        covered |= {(i, c.col + j) for i in range(first, last + 1) for j in range(c.col_span)}
    cells += [Cell(t.replace("<br>", "\n"), r, c) for r, row in enumerate(grid) for c, t in enumerate(row)
              if (r, c) not in covered]
    return Table(cells)


def split_subrows(page, tab, table: Table) -> Table:
    """tab 은 pdfplumber 표, table 은 그 표를 옮긴 것. 하위 행을 나눈 표(나눌 것이 없으면 그대로)."""
    try:
        md_rows = [[c.replace("\n", "<br>") for c in row] for row in table.grid()]
        rows = list(tab.rows)
        if not md_rows or len(md_rows) != len(rows):
            return table
        edges = _col_edges(tab)
        n_cols = len(md_rows[0])
        if len(edges) - 1 != n_cols:
            return table
        by_row = _chars_by_row(rows, _chars(page, tab.bbox))
        out, changed, kept = [md_rows[0]], False, []
        src: List[Optional[int]] = [0]  # out 의 각 행이 온 원래 행(하위 행은 None)
        for group in _row_groups(table, len(md_rows)):
            cells = md_rows[group[0]]
            # 숫자가 여러 줄로 쌓인 칸이 없으면 나눌 하위 행이 없다. 행 이름 글이 두 줄로 접힌 것만으로 나누면
            # 칸 클립으로 이미 갈린 행(계/남/여)이 줄 단위로 다시 묶인다
            if not any(sum(any(ch.isdigit() for ch in seg) for seg in md_rows[r][c].split("<br>")) >= 2
                       for r in group for c in range(n_cols)):
                for r in group:
                    if any(w[4].isalnum() for w in by_row[r]):
                        kept.append(len(out))
                    out.append(md_rows[r])
                    src.append(r)
                continue
            # 문단이 든 행(쪽 테두리를 표로 잡은 경우)은 나누면 낱말이 칸으로 흩어진다. 숫자가 여러 줄 쌓인 열이 둘 이상이면 긴 이름이 있어도 값 행이다
            stacked = sum(any(sum(any(ch.isdigit() for ch in seg) for seg in md_rows[r][c].split("<br>")) >= 2 for r in group)
                          for c in range(n_cols))
            prose = stacked < 2 and any(len(seg) > _PROSE for r in group for c in md_rows[r] for seg in c.split("<br>"))
            inside = [w for r in group for w in by_row[r]]
            row_cells = [(min(c[1] for c in cs), max(c[3] for c in cs), [x for c in cs for x in (c[0], c[2])])
                         for cs in ([c for c in rows[r].cells if c] for r in group) if cs]
            sub = _text_rows(inside, edges, n_cols) if prose else \
                (_split_row(inside, edges, n_cols, row_cells) or _aligned_rows(inside, edges, n_cols)
                 or _text_rows(inside, edges, n_cols))
            if not sub:
                for r in group:
                    # 제 글자가 없는 행은 병합 칸 복사본뿐이다(find_tables 가 만든 자잘한 행)
                    if any(w[4].isalnum() for w in by_row[r]):
                        kept.append(len(out))
                    out.append(md_rows[r])
                    src.append(r)
                continue
            changed = True
            # 위 행에서 내려온 병합 칸(행 안에 글자가 없는 칸)은 원래 채운 값을 쓴다
            for c in [c for c in range(n_cols) if cells[c] and not any(s[c] for s in sub)]:
                for s in sub:
                    # 가로 병합으로 옆 칸 값이 복사된 칸은 하위 행에서도 옆 칸을 따른다
                    if c + 1 < n_cols and cells[c] == cells[c + 1]:
                        s[c] = s[c + 1]
                    elif c > 0 and cells[c] == cells[c - 1]:
                        s[c] = s[c - 1]
                    else:
                        parts = cells[c].split("<br>")
                        s[c] = ("" if all(len(p) == 1 for p in parts) else " ").join(parts)
            kept.extend(range(len(out), len(out) + len(sub)))
            out.extend(sub)
            src.extend([None] * len(sub))
        if not changed:
            return table
        order = [0] + [k for k in kept if k > 0]
        return _respan(table, [out[k] for k in order], [src[k] for k in order])
    except Exception:
        return table
