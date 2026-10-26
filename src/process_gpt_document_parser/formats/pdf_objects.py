"""그리기 명령이 많은 쪽을 빨리 읽는다: 선·사각형·곡선은 pdfium, 글자는 경로 명령을 걷어 낸 스트림으로 pdfminer.

pdfminer 의 내용 스트림 해석이 파이썬이라 그래프의 경로 명령 수백만 개에서 느리다. 규칙은 pdfplumber 객체를 보고 맞춰
왔으므로 객체 모양(좌표·선/사각형/곡선 구분)은 pdfminer 를 따른다. 글자는 pdfminer 그대로 둔다(pdfium 은 공백 글자를
줄이거나 생성 공백으로 바꿔 원래 글리프를 알 수 없다). 근거: docs/VERIFICATION.md "pdfium 엔진".
"""
from __future__ import annotations

import ctypes
import re
from typing import Any, Dict, List, Optional, Tuple

import pypdfium2.raw as c

Matrix = Tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_LINETO, _BEZIERTO, _MOVETO = c.FPDF_SEGMENT_LINETO, c.FPDF_SEGMENT_BEZIERTO, c.FPDF_SEGMENT_MOVETO


def _mul(m: Matrix, n: Matrix) -> Matrix:
    """m 을 먼저, n 을 나중에 적용하는 행렬."""
    a, b, cc, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, cc * a2 + d * c2, cc * b2 + d * d2, e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def _matrix(obj) -> Matrix:
    fm = c.FS_MATRIX()
    if not c.FPDFPageObj_GetMatrix(obj, fm):
        return _IDENTITY
    # pdfium 은 float32 라 스트림의 10진수(0.12)가 0.1199999… 로 온다. 되돌려야 좌표 끝자리가 pdfminer 와 같다
    return (round(fm.a, 5), round(fm.b, 5), round(fm.c, 5), round(fm.d, 5), round(fm.e, 4), round(fm.f, 4))


# ── 선·사각형·곡선 (pdfium) ─────────────────────────────────────────────────

class _Paths:
    """한 쪽의 경로 객체. 좌표는 pdfplumber 와 같게(왼쪽 위 원점, mediabox 기준)."""

    def __init__(self, page, plumber_page):
        self.height = float(plumber_page.height)
        self.mb_top = float(plumber_page.mediabox[1])
        self.mb_y0 = float(page.get_mediabox()[1])
        self.page_number, self.doctop = plumber_page.page_number, plumber_page.initial_doctop
        self.objects: Dict[str, List[Dict[str, Any]]] = {"rect": [], "line": [], "curve": []}
        self._buf = (ctypes.c_int(), ctypes.c_int(), ctypes.c_float(), ctypes.c_float(), ctypes.c_float())
        self._rgba = tuple(ctypes.c_uint() for _ in range(4))

    def y(self, y_abs: float) -> float:
        return self.mb_top + self.height - (y_abs - self.mb_y0)

    def walk(self, count, get, parent: Matrix) -> None:
        for i in range(count):
            obj = get(i)
            kind = c.FPDFPageObj_GetType(obj)
            if kind == c.FPDF_PAGEOBJ_FORM:
                m = _mul(_matrix(obj), parent)
                self.walk(c.FPDFFormObj_CountObjects(obj), lambda k, o=obj: c.FPDFFormObj_GetObject(o, k), m)
            elif kind == c.FPDF_PAGEOBJ_PATH:
                self.path(obj, _mul(_matrix(obj), parent))

    def path(self, obj, m: Matrix) -> None:
        fillmode, stroke, x, yv, width = self._buf
        c.FPDFPath_GetDrawMode(obj, fillmode, stroke)
        fill, stroke_on = fillmode.value != 0, bool(stroke.value)
        if not (fill or stroke_on):
            return  # 칠하지 않는 경로(pdfminer 의 n)
        a, b, cc, d, e, f = m
        segs: List[tuple] = []
        bez: List[Tuple[float, float]] = []
        get_seg, get_pt, get_type, get_close = (c.FPDFPath_GetPathSegment, c.FPDFPathSegment_GetPoint,
                                                c.FPDFPathSegment_GetType, c.FPDFPathSegment_GetClose)
        for k in range(c.FPDFPath_CountSegments(obj)):
            seg = get_seg(obj, k)
            get_pt(seg, x, yv)
            t = get_type(seg)
            px, py = round(x.value, 4), round(yv.value, 4)
            pt = (a * px + cc * py + e, b * px + d * py + f)
            if t == _LINETO:
                segs.append(("l", pt))
            elif t == _MOVETO:
                segs.append(("m", pt))
            elif t == _BEZIERTO:
                bez.append(pt)
                if len(bez) == 3:
                    segs.append(("c", *bez))
                    bez = []
            if get_close(seg):
                segs.append(("h",))
        c.FPDFPageObj_GetStrokeWidth(obj, width)
        style = {"linewidth": round(width.value, 4), "stroke": stroke_on, "fill": fill, "evenodd": fillmode.value == 1,
                 "stroking_color": self._color(c.FPDFPageObj_GetStrokeColor, obj),
                 "non_stroking_color": self._color(c.FPDFPageObj_GetFillColor, obj), "dash": None}
        starts = [i for i, sg in enumerate(segs) if sg[0] == "m"] + [len(segs)]
        for i, j in zip(starts, starts[1:]):
            if j - i > 1:  # pdfminer 는 m 하나뿐인 부분 경로를 그리지 않는다(m[^m]+)
                self.paint(segs[i:j], style)

    def _color(self, getter, obj) -> Tuple[float, ...]:
        r, g, b, a = self._rgba
        if not getter(obj, r, g, b, a):
            return (0.0,)
        return (r.value / 255, g.value / 255, b.value / 255)

    def paint(self, path: List[tuple], style: Dict[str, Any]) -> None:
        """pdfminer converter.paint_path 와 같은 구분(선 하나 / 축에 나란한 닫힌 사각형 / 그 밖 곡선)."""
        shape = "".join(s[0] for s in path)
        pts = [s[-1] if s[0] != "h" else path[0][-1] for s in path]
        if len(shape) > 3 and shape[-2:] == "lh" and pts[-2] == pts[0]:
            shape = shape[:-2] + "h"
            pts.pop()
        if shape in ("mlh", "ml"):
            kind = "line"
            pts = pts[:2]
        elif shape in ("mlllh", "mllll"):
            (x0, y0), (x1, y1), (x2, y2), (x3, y3), _ = pts
            square = (x0 == x1 and y1 == y2 and x2 == x3 and y3 == y0) or (y0 == y1 and x1 == x2 and y2 == y3 and x3 == x0)
            kind = "rect" if pts[0] == pts[4] and square else "curve"
        else:
            kind = "curve"
        if kind == "rect":
            xs, ys = (pts[0][0], pts[2][0]), (pts[0][1], pts[2][1])
        else:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        top, bottom = self.y(y1), self.y(y0)
        o = {"object_type": kind, "page_number": self.page_number, "x0": x0, "x1": x1,
             "y0": y0 - self.mb_y0, "y1": y1 - self.mb_y0, "width": x1 - x0, "height": y1 - y0,
             "top": top, "bottom": bottom, "doctop": self.doctop + top, "mcid": None, "tag": None}
        o.update(style)
        o["pts"] = [(px, self.y(py)) for px, py in pts]
        o["path"] = [(s[0], *[(px, self.y(py)) for px, py in s[1:]]) for s in path]
        self.objects[kind].append(o)


def paths(pdf, plumber_page, index: int) -> Dict[str, List[Dict[str, Any]]]:
    """pdfplumber 의 rect·line·curve 목록."""
    page = pdf[index]
    try:
        p = _Paths(page, plumber_page)
        p.walk(c.FPDFPage_CountObjects(page.raw), lambda k: c.FPDFPage_GetObject(page.raw, k), _IDENTITY)
        return p.objects
    finally:
        page.close()


# ── 내용 스트림 ───────────────────────────────────────────────────────────────

_TOKEN = re.compile(rb"""
    (?P<num>[+-]?(?:\d+\.?\d*|\.\d+))
  | (?P<op>[A-Za-z'"][A-Za-z0-9'"*]*)
  | (?P<str>\()
  | (?P<other><<|>>|<[0-9A-Fa-f\s]*>|[\[\]{}]|/[^\s/\[\]()<>{}%]*|%[^\r\n]*)
  | (?P<sp>\s+)
""", re.X)
_PAREN = re.compile(rb"[()\\]")
_PAINT = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
_HAS_CLIP = re.compile(rb"(?<![A-Za-z])W\*?(?![A-Za-z0-9'\"*])")
# 그래픽 상태·클립 연산자(앞뒤가 공백·구분자)와, 그 사이 글 문자열·주석. 칠하기는 _path 가 본다
_COARSE = re.compile(
    rb"(?P<str>\()|(?P<hex><(?!<)[0-9A-Fa-f\s]*>)|(?P<cmt>%[^\r\n]*)"
    rb"|(?<![^\s\[\]()<>{}])(?P<op>q|Q|cm|n|W\*?|ID)(?![^\s\[\]()<>{}/%])")
_NUM = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_EI = re.compile(rb"\sEI(?=\s|$)")
# 경로 만들기·칠하기·클립, 선 모양·색 연산자와 그 피연산자(글자 색은 규칙이 안 본다).
# 글 문자열은 먼저 걸려 그대로 남는다(\1·\2)
_N = rb"[+-]?(?:\d+\.?\d*|\.\d+)\s+"
_END = rb"(?![A-Za-z0-9'\"*])"
_NAME = rb"/[^\s/\[\]()<>{}%]+\s+"
_PATH_OPS = re.compile(
    rb"(\((?:[^()\\]|\\.)*\))|(<(?!<)[0-9A-Fa-f\s]*>)"
    rb"|(?<![A-Za-z0-9'\"*/.])(?:(?:" + _N + rb")++(?:" + _NAME + rb")?(?:re|RG|rg|SCN|scn|SC|sc|[mlcvywJjMiGgKk])"
    rb"|\[[^\]]*\]\s*" + _N + rb"d|" + _NAME + rb"(?:CS|cs|ri)|h|S|s|f\*?|F|B\*?|b\*?|n|W\*?)" + _END)
_EMPTY_Q = re.compile(rb"(?<![A-Za-z0-9'\"*/])q\s+Q" + _END)


def _streams(page_obj) -> List[Any]:
    from pdfminer.pdftypes import list_value, stream_value

    return [stream_value(s) for s in list_value(page_obj.contents)]


def strip_stream(data: bytes) -> bytes:
    data = _PATH_OPS.sub(rb"\1\2", data)
    n = 1
    while n:  # 그리기를 걷어 내 빈 q … Q 는 안쪽부터 지운다
        data, n = _EMPTY_Q.subn(b"", data)
    return data


def strip_paths(page_obj) -> None:
    """쪽과 그 폼 XObject 의 내용 스트림에서 경로 명령을 지운다. 글자·그림·그래픽 상태는 남는다."""
    from pdfminer.pdftypes import dict_value, resolve1, stream_value

    seen = set()

    def strip(stream) -> None:
        if id(stream) in seen:
            return
        seen.add(id(stream))
        stream.data = strip_stream(stream.get_data())

    def forms(resources, depth: int) -> None:
        xobjects = dict_value(resolve1((resources or {}).get("XObject"))) if resources else {}
        for ref in xobjects.values():
            try:
                x = stream_value(ref)
            except Exception:
                continue
            if x.get("Subtype") is not None and getattr(x.get("Subtype"), "name", None) == "Form" and depth < 8 \
                    and id(x) not in seen:
                strip(x)
                forms(dict_value(resolve1(x.get("Resources"))) if x.get("Resources") else None, depth + 1)

    for s in _streams(page_obj):
        strip(s)
    forms(page_obj.resources, 0)


# ── 클립 ─────────────────────────────────────────────────────────────────────
# pdfium 은 객체를 다 덮는 클립을 버려 한컴 칸 클립 대부분이 남지 않는다. 쪽 내용 스트림에서 `W n` 경로만 직접 읽는다.
# pdfminer 기록과 같게: 쪽 최상위 스트림만(폼 안은 안 본다), m 이 W 표시를 지우고 n 에서 남긴다.

def _path(segment: bytes) -> Tuple[bool, List[tuple]]:
    """마지막으로 경로를 비운 뒤부터 n 앞까지: (W 표시가 남았나, 경로)."""
    args: List[Optional[float]] = []
    path: List[tuple] = []
    clip = False
    pos, end = 0, len(segment)
    while pos < end:
        m = _TOKEN.match(segment, pos)
        if not m:
            pos += 1
            args.clear()
            continue
        pos = m.end()
        kind = m.lastgroup
        if kind == "num":
            args.append(float(m.group()))
            continue
        if kind == "str":
            pos = _skip_string(segment, pos)
            args.append(None)
            continue
        if kind != "op":
            if kind == "other":
                args.append(None)
            continue
        op = m.group()
        if op == b"m" and len(args) >= 2:
            clip = False
            path.append(("m", args[-2], args[-1]))
        elif op == b"l" and len(args) >= 2:
            path.append(("l", args[-2], args[-1]))
        elif op in (b"c", b"v", b"y"):
            path.append((op.decode(),))
        elif op == b"h":
            path.append(("h",))
        elif op == b"re" and len(args) >= 4 and None not in args[-4:]:
            x, yv, w, hh = args[-4:]
            path += [("m", x, yv), ("l", x + w, yv), ("l", x + w, yv + hh), ("l", x, yv + hh), ("h",)]
        elif op in (b"W", b"W*"):
            clip = True
        elif op in _PAINT:
            path = []
        args.clear()
    return clip, path


def _skip_string(data: bytes, pos: int) -> int:
    depth = 1
    while depth:
        m = _PAREN.search(data, pos)
        if not m:
            return len(data)
        ch, pos = m.group(), m.end()
        if ch == b"\\":
            pos += 1
        else:
            depth += 1 if ch == b"(" else -1
    return pos


def _mult(m1: Matrix, m0: Matrix) -> Matrix:
    """pdfminer mult_matrix(m1, m0)."""
    a1, b1, c1, d1, e1, f1 = m1
    a0, b0, c0, d0, e0, f0 = m0
    return (a0 * a1 + c0 * b1, b0 * a1 + d0 * b1, a0 * c1 + c0 * d1, b0 * c1 + d0 * d1,
            a0 * e1 + c0 * f1 + e0, b0 * e1 + d0 * f1 + f0)


def clip_rects(page_obj, height: float) -> List[Tuple[float, float, float, float]]:
    """축에 나란한 사각형 클립 (x0, top, x1, bottom). strip_paths 앞에 부른다."""
    try:
        data = b"\n".join(s.get_data() for s in _streams(page_obj))
    except Exception:
        return []
    if not _HAS_CLIP.search(data):
        return []
    x0, y0 = page_obj.mediabox[0], page_obj.mediabox[1]
    ctm: Matrix = (1, 0, 0, 1, -x0, -y0)
    stack: List[Matrix] = []
    found: List[Tuple[Matrix, List[tuple]]] = []
    # 상태를 바꾸는 연산자만 건너뛰며 찾고, W 가 낀 경로만 _path 로 자세히 읽는다
    pos, end, since, saw_w = 0, len(data), 0, False
    while pos < end:
        m = _COARSE.search(data, pos)
        if not m:
            break
        pos = m.end()
        kind = m.lastgroup
        if kind == "str":
            pos = _skip_string(data, pos)
            continue
        if kind != "op":
            continue
        op = m.group("op")
        if op == b"ID":  # 인라인 그림 자료는 건너뛴다
            e = _EI.search(data, pos)
            pos = e.end() if e else end
        elif op in (b"W", b"W*"):
            saw_w = True
        elif op == b"n":
            if saw_w:
                clip, path = _path(data[since:m.start()])
                if clip:
                    found.append((ctm, path))
            since, saw_w = pos, False
        else:  # q·Q·cm 은 경로 밖에서만 온다
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                if stack:
                    ctm = stack.pop()
            else:
                nums = _NUM.findall(data, max(0, m.start() - 160), m.start())
                if len(nums) >= 6:
                    ctm = _mult(tuple(float(v) for v in nums[-6:]), ctm)
            since = pos
    out = []
    for (a, b, cc, d, e, f), segs in found:
        pts = [(s[1], s[2]) for s in segs if s[0] in ("m", "l")]
        if len(pts) not in (4, 5):
            continue
        pts = [(a * x + cc * yv + e, b * x + d * yv + f) for x, yv in pts]
        xs, ys = sorted({round(p[0], 2) for p in pts}), sorted({round(p[1], 2) for p in pts})
        if len(xs) == 2 and len(ys) == 2:
            out.append((xs[0], height - ys[1], xs[1], height - ys[0]))
    return out
