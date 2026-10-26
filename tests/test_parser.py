import glob
import os
import re

import pytest

from process_gpt_document_parser import Cell, Table, UnsupportedFormatError, detect_format, parse

BENCH = os.environ.get(
    "PARSER_BENCH",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "test-field", "parser-bench"))
REAL = os.path.join(BENCH, "real")
needs_bench = pytest.mark.skipif(not os.path.isdir(REAL), reason="parser-bench 샘플이 없다")


def test_table_grid_fills_merged_cells():
    t = Table([Cell("머리", 0, 0, 1, 2), Cell("a", 1, 0), Cell("b", 1, 1, 2, 1), Cell("c", 2, 0)])
    assert t.grid() == [["머리", "머리"], ["a", "b"], ["c", "b"]]
    assert t.to_markdown().splitlines()[1] == "| --- | --- |"


def test_table_formats():
    t = Table([Cell("머리", 0, 0, 1, 2), Cell("a", 1, 0), Cell("b", 1, 1)])
    assert t.to_markdown(fill_merged=False).splitlines()[0] == "| 머리 |   |"
    assert '<td colspan="2">머리</td>' in t.to_html()


def test_detect_format_rejects_unknown():
    with pytest.raises(UnsupportedFormatError):
        detect_format(b"hello", "a.txt")


def _words(text):
    return re.findall(r"[0-9A-Za-z가-힣]+", text)


@needs_bench
@pytest.mark.parametrize("hwp", sorted(glob.glob(os.path.join(REAL, "*", "*.hwp"))))
def test_hwp_matches_hwpx_twin(hwp):
    """같은 문서의 HWP 와 HWPX 는 글·표·그림이 같아야 한다."""
    twin = hwp[:-4] + ".hwpx"
    if not os.path.exists(twin):
        pytest.skip("HWPX 짝이 없다")
    try:
        x = parse(twin)
    except Exception as exc:  # 짝 HWPX 가 암호 문서인 경우
        pytest.skip(f"HWPX 를 못 읽는다: {exc}")
    h = parse(hwp)
    assert _words(h.to_markdown()) == _words(x.to_markdown())
    assert [b.kind for b in h.blocks] == [b.kind for b in x.blocks]
    assert [i.data for i in h.images] == [i.data for i in x.images]


EXTERNAL = os.path.join(BENCH, "external")
needs_external = pytest.mark.skipif(not os.path.isdir(EXTERNAL), reason="external 샘플이 없다")


@needs_external
def test_hwp_distribution_documents_decrypt():
    doc = parse(os.path.join(EXTERNAL, "hwp", "viewtext.hwp"))
    assert doc.metadata.get("distribution")
    assert "배포 문서 예제" in doc.to_markdown()
    assert "입찰공고" in parse(os.path.join(EXTERNAL, "hwp", "distribution.hwp")).to_markdown()


@needs_external
def test_hwp_outline_headings():
    doc = parse(os.path.join(EXTERNAL, "hwp", "lists.hwp"))
    heads = [(b.heading_level, b.text) for b in doc.blocks if b.heading_level]
    assert heads[:6] == [(1, "1"), (1, "2"), (2, "2-1"), (1, "3"), (2, "3-1"), (2, "3-2")]
    assert (3, "3-2-1") in heads


@needs_external
def test_doc_table_without_end_marks_keeps_text():
    """칸·행 끝 표시 없이 끝나는 표(o_kurs.doc 표지)의 글을 버리지 않는다."""
    doc = parse(os.path.join(EXTERNAL, "poi_doc", "o_kurs.doc"))
    assert "КОРПОРАТИВНОГО" in doc.to_markdown()


@needs_bench
def test_pdf_keeps_table_structure():
    pdf = glob.glob(os.path.join(REAL, "*", "26년 국세수입*.pdf"))[0]
    doc = parse(pdf)
    rows = next(b.table.grid() for b in doc.blocks if b.table and "실적(A)" in b.table.to_markdown())
    # 원본 HWPX 와 같은 칸: 총국세는 두 열 병합, 그 아래 이름은 둘째 열(첫 열은 빈 칸)
    assert ["ㅇ 총 국 세", "ㅇ 총 국 세", "373.9"] == next(r for r in rows if r[0] == "ㅇ 총 국 세")[:3]
    names = [r[1] for r in rows]
    assert "(농특세)" in names
    assert ["", "-일반회계", "361.5", "378.6", "398.7", "455.3"] == rows[names.index("-일반회계")][:6]


def test_hwp3_hchar_decoding():
    from process_gpt_document_parser.formats.hwp3_chars import decode_hchar

    assert decode_hchar(0x8861) == "가"  # 상용 조합형
    assert decode_hchar(0x3438) == "※"  # KS X 1001 1행
    assert decode_hchar(0x36E7) == "①"  # 8행
    assert decode_hchar(0x4000) == "伽"  # 한자 첫 글자
    assert decode_hchar(0x2022) == "•"  # 유니코드 그대로
    assert decode_hchar(0x301C) is None  # 한컴 고유 영역


def test_hwp3_table_grid_snaps_rounding():
    from process_gpt_document_parser.formats.hwp3 import _table

    # 오른쪽 위 칸 경계가 1 hunit 어긋나도 빈 칸이 생기지 않는다
    t = _table([(0, 0, 100, 50), (100, 0, 101, 50), (0, 50, 201, 50)], ["a", "b", "c"])
    assert t.grid() == [["a", "b"], ["c", "c"]]


def test_hwp3_detected_and_errors_wrapped():
    from process_gpt_document_parser import EncryptedDocumentError, ParseError
    from process_gpt_document_parser.formats.hwp3 import SIGNATURE

    head = SIGNATURE + bytes(128 + 1008)
    assert detect_format(head) == "hwp"
    with pytest.raises(ParseError):
        parse(head)
    locked = bytearray(head)
    locked[30 + 96] = 1
    with pytest.raises(EncryptedDocumentError):
        parse(bytes(locked))


HWP3 = os.path.join(EXTERNAL, "hwp3", "rhwp")
HWP3_TWINS = [("hwp3-sample.hwp", "hwp3-sample-hwpx.hwpx"), ("hwp3-sample4.hwp", "hwp3-sample4-hwp5.hwp"),
              ("hwp3-sample5.hwp", "hwp3-sample5-hwpx.hwpx"), ("hwp3-sample11.hwp", "hwp3-sample11-hwpx.hwpx"),
              ("hwp3-sample13.hwp", "hwp3-sample13-hwp5.hwpx"), ("hwp3-sample14.hwp", "hwp3-sample14-hwp5.hwpx"),
              ("hwp3-sample16.hwp", "hwp3-sample16-hwp5.hwpx"), ("hwp3-sample19.hwp", "hwp3-sample19-hwpx.hwpx")]


@pytest.mark.skipif(not os.path.isdir(HWP3), reason="HWP 3.0 샘플이 없다")
@pytest.mark.parametrize("src,ref", HWP3_TWINS)
def test_hwp3_matches_hancom_conversion(src, ref):
    """한컴이 HWPX·HWP5 로 바꾼 같은 문서와 한글 음절·표 수·그림이 같다."""
    ours, theirs = parse(os.path.join(HWP3, src)), parse(os.path.join(HWP3, ref))
    syllables = lambda d: sorted(re.findall(r"[가-힣]", d.to_markdown("markdown-first")))
    assert syllables(ours) == syllables(theirs)
    assert sum(b.kind == "table" for b in ours.blocks) == sum(b.kind == "table" for b in theirs.blocks)
    assert [i.data for i in ours.images] == [i.data for i in theirs.images]


def _ch(text, x0, x1, top=0.0):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 10, "size": 10, "upright": True}


def test_pdf_clean_chars_ligature_and_overlapping_accent():
    from process_gpt_document_parser.formats.pdf_layout import clean_chars, lines_from_chars

    chars = [_ch("J", 0, 5), _ch("o", 5, 10), _ch("a", 10, 15), _ch("˜", 11, 14), _ch("o", 15, 20),
             _ch(" ", 20, 23), _ch("ﬂ", 23, 30), _ch("o", 30, 35), _ch("w", 35, 40),
             _ch(" ", 40, 43), _ch("n", 43, 48), _ch("´", 48, 50), _ch("t", 50, 55)]
    clean_chars(chars)
    # 겹친 물결은 ã 로, 합자는 fl 로, 글자 사이에 따로 선 ´ 는 그대로
    assert lines_from_chars(chars)[0].text == "João flow n´t"


def test_pdf_joins_hyphenated_line_ends():
    from process_gpt_document_parser.formats.pdf_layout import _join_lines

    assert _join_lines(["ventricular ar-", "rhythmias and", "high-", "Explosives"]) == \
        "ventricular arrhythmias and\nhigh-\nExplosives"


def test_pdf_dotted_segments_become_one_line():
    from process_gpt_document_parser.formats.pdf import _dotted_lines

    class Page:
        lines = [{"x0": 10.0, "x1": 10.0, "top": float(t), "bottom": t + 0.4} for t in range(10)]

    vs, hs = _dotted_lines(Page())
    assert len(vs) == 1 and not hs and vs[0]["top"] == 0 and abs(vs[0]["bottom"] - 9.4) < 1e-6

    class Dots:  # 길이 0인 점(둥근 끝)으로 찍은 세로 점선
        lines = [{"x0": 10.0, "x1": 10.0, "top": t * 1.2, "bottom": t * 1.2} for t in range(10)]

    vs, hs = _dotted_lines(Dots())
    assert len(vs) == 1 and not hs and abs(vs[0]["bottom"] - 10.8) < 1e-6


OLMOCR_TABLES = os.path.join(BENCH, "public", "olmocr-bench", "bench_data", "pdfs", "tables")


@pytest.mark.skipif(not os.path.isdir(OLMOCR_TABLES), reason="olmOCR-Bench 샘플이 없다")
def test_pdf_horizontal_rule_only_tables():
    """가로선만 있는 논문식 표 셋(한 쪽, 캡션·주석으로 갈림). 여러 열 머리는 병합 칸, 머리 아래 짧은 선 위아래는 따로 행."""
    pdf = glob.glob(os.path.join(OLMOCR_TABLES, "4c77cbf4de004721c9da33c7f3843716c9a6*.pdf"))[0]
    grids = [b.table.grid() for b in parse(pdf).blocks if b.table]
    assert len(grids) == 3 and all(len(g[0]) == 8 for g in grids)
    assert grids[0][0][1:4] == ["Mean (log DNA copies/g) ± SD"] * 3
    assert ["Bifidobacterium", "4.79±0.37", "4.81±0.31", "4.77±0.51", "↑0.5%", "↓0.8%", "0.8", "0.84"] in grids[0]


DP_PDFS = os.path.join(BENCH, "public", "dp-bench", "dataset", "pdfs")
needs_dp = pytest.mark.skipif(not os.path.isdir(DP_PDFS), reason="DP-Bench 샘플이 없다")


def _dp_grids(name):
    return [b.table.grid() for b in parse(os.path.join(DP_PDFS, name + ".pdf")).blocks if b.table]


@needs_dp
def test_pdf_line_shading_inside_cells_is_not_grid():
    """Word 가 칸 배경 안에 줄마다 그린 음영 사각형(칸보다 들여 그림)은 칸 경계가 아니다."""
    grids = _dp_grids("01030000000089")
    assert len(grids) == 1 and len(grids[0][0]) == 5
    assert any(r[:3] == ["Canada", "Y", "Y"] for r in grids[0])


@needs_dp
def test_pdf_rule_table_with_small_font_splits_columns():
    """본문보다 작은 글자로 짠 세 줄 표. 열 간격은 표 글자 크기로 잰다."""
    grids = _dp_grids("01030000000197")
    assert ["ARC", "HellaSwag", "MMLU", "TruthfulQA", "Winogrande", "GSM8K"] in [g[0] for g in grids]
    assert any(["0.06", "N/A", "0.15", "0.28", "N/A", "0.70"] in g for g in grids)


@needs_dp
def test_pdf_zebra_row_fills_without_outer_frame():
    """바깥 테두리 없이 줄무늬 행 배경과 안쪽 세로선만 있는 표. 가장자리 열의 배경도 칸이다."""
    rows = [[c.replace("\n", " ") for c in r] for g in _dp_grids("01030000000045") for r in g]
    assert ["1", "Union of Youth Federations of Cambodia (UYFC)", "17,266"] in rows


@needs_dp
def test_pdf_column_rules_extend_table_past_outer_rules():
    """윗선 없는 머리행과 아랫선 밑 합계 행: 안쪽 세로선이 뻗은 데까지 표다."""
    rows = [[c.replace("\n", " ") for c in r] for r in _dp_grids("01030000000045")[0]]
    assert rows[0][:2] == ["No.", "Name of organization"] and rows[-1] == ["", "Total", "27,926"]


@needs_dp
def test_pdf_figure_caption_and_chart_numbers_are_not_tables():
    assert _dp_grids("01030000000073") == []  # 틀 안의 Figure 6 | 제목
    assert _dp_grids("01030000000037") == []  # 막대그래프 위 숫자


@needs_dp
def test_pdf_same_color_line_shading_and_unruled_header_cells():
    """칸 배경 안 같은 색 줄 음영은 행 경계가 아니고, 선 없는 머리칸 글은 아래 열대로 나눈다."""
    rows = [[c.replace("\n", " ") for c in r] for r in _dp_grids("01030000000119")[0]]
    assert rows[0] == ["", "Mitosis (begins with a single cell)", "Meiosis (begins with a single cell)"]
    assert [r[0] for r in rows[1:]] == ["# chromosomes in parent cells", "# DNA replications", "# nuclear divisions",
                                        "# daughter cells produced", "purpose"]


@needs_dp
def test_pdf_unruled_table_header_line_and_sparse_rows():
    assert _dp_grids("01030000000117")[0][0] == ["Saccharometer", "DI Water", "Glucose Solution", "Yeast Suspension"]
    assert _dp_grids("01030000000127")[0][-1] == ["8", "", "", "4.46%"]  # 빈 칸 있는 행도 이어진다


@needs_dp
def test_pdf_rule_tables_stacked_on_page_and_trimmed_midrules():
    """한 쪽에 위아래로 놓인 세 줄 표는 표마다 제 폭. 끝을 줄여 그린 안쪽 선은 열 경계까지 닫는다."""
    last = _dp_grids("01030000000189")[-1]
    assert [r[0] for r in last] == ["Model", "DPO v2", "DPO v3"]
    rows = _dp_grids("01030000000187")[0]
    assert [r[0] for r in rows[-3:]] == ["Total # Samples", "Maximum # Samples Used", "Open Source"]


@needs_dp
def test_pdf_ruled_one_column_list_is_a_table():
    grid = _dp_grids("01030000000149")[0]
    assert len(grid) == 8 and grid[0] == ["Eco-Circle Competence Framework"]


@needs_dp
def test_pdf_toc_and_rotated_axis_labels_are_not_tables():
    assert _dp_grids("01030000000044") == []  # 선 친 차례
    assert _dp_grids("01030000000108") == []  # 링크 밑줄 친 차례
    assert _dp_grids("01030000000076") == []  # 눕혀 쓴 그래프 축 날짜


@needs_dp
def test_pdf_zebra_stripes_with_unshaded_rows_are_one_table():
    """배경 칠한 두 행 묶음 사이에 배경 없는 행이 끼어도 한 표. 두 행에 걸친 머리칸 배경도 칸이다."""
    grids = _dp_grids("01030000000078")
    assert len(grids) == 1 and len(grids[0]) == 10
    assert grids[0][-1][0] == "Viet Nam" and grids[0][2][0] == "Cambodia"


@needs_dp
def test_pdf_two_rule_table_rows_by_line_gaps():
    """위·아래 선 둘뿐인 표: 행은 줄 사이가 벌어진 자리, 맨 위 한 줄 제목은 병합 칸."""
    rows = _dp_grids("01030000000166")[0]
    assert rows[0] == ["Mineral or colloid type", "CEC of pure colloid"] and rows[-1] == ["humus", "200"]
    fish = _dp_grids("01030000000132")[0]
    assert fish[2] == ["La Palma Pupfish", "Cyprinodon longidorsalis"]


@needs_dp
def test_pdf_text_outside_page_is_dropped():
    """쪽 밖(x > 쪽 폭)에 놓여 보이지 않는 글은 읽지 않는다(펼친 면에서 잘라 낸 쪽)."""
    text = "\n".join(b.text or "" for b in parse(os.path.join(DP_PDFS, "01030000000027.pdf")).blocks)
    assert "Figure 7." in text and "Figures 7–9 show" not in text


@needs_dp
def test_pdf_tab_aligned_header_with_one_value_row():
    """탭 정렬 표: 머리는 낱말 간격으로만 띄었고 값 행은 하나뿐이지만, 값 칸이 머리 낱말 첫 글자 자리에서 시작한다."""
    grids = _dp_grids("01030000000116")
    assert grids[1] == [["Saccharometer", "DI Water", "Glucose Solution", "Yeast Suspension"], ["1", "16 ml", "12 ml", "0 ml"]]


@needs_dp
def test_pdf_stacked_header_levels_keep_spans():
    """세 줄 표의 층층 머리: 묶음 머리 줄마다 행, cmidrule 끝까지 병합, 첫 열 머리는 머리 층 전체에 걸친다."""
    t = next(b.table for b in parse(os.path.join(DP_PDFS, "01030000000187.pdf")).blocks if b.table)
    spans = {(c.row, c.col): (c.row_span, c.col_span) for c in t.cells}
    assert spans[(0, 0)] == (3, 1) and spans[(0, 1)] == (1, 6) and spans[(1, 1)] == (1, 3) and spans[(1, 4)] == (1, 3)
    assert t.grid()[2][2:4] == ["OpenOrca", "Synth. Math-Instruct"]


@needs_dp
def test_pdf_slide_table_from_text_frames():
    """슬라이드 표: 괘선은 배경 그림이고 칸마다 글 틀(채움 사각형)만 있다. 틀 격자로 행·열을 잡는다."""
    rows = _dp_grids("01030000000200")[0]
    assert len(rows) == 10 and rows[0] == ["Service Stage", "Function Name", "Explanation", "Expected Benefit"]
    assert rows[-1][1] == "Guide and help"


@needs_dp
def test_pdf_reading_order_by_position():
    """그리기 순서가 꼬리말·옆 단부터여도 위치로 읽는다: 두 단 참고문헌은 왼 단 끝까지 읽고 오른 단으로."""
    blocks = [b.text for b in parse(os.path.join(DP_PDFS, "01030000000194.pdf")).blocks if b.text]
    names = [t.split(",")[0] for t in blocks]
    assert names.index("Zheng Yuan") < names.index("Rowan Zellers") < names.index("Daniel M Ziegler")
    footer = [b.text for b in parse(os.path.join(DP_PDFS, "01030000000147.pdf")).blocks if b.text]
    assert "Project No" in footer[-1]


@needs_dp
def test_pdf_blank_form_between_two_rules():
    """빈 양식: 위·아래 선 사이 머리 한 줄만 열로 갈리고(머리 칸 사이가 좁다) 아래 줄은 첫 열 이름뿐."""
    rows = _dp_grids("01030000000165")[0]
    assert rows[0] == ["Added cation", "Relative Size & Settling Rates of Floccules"] and rows[-1] == ["Check", ""]


@needs_dp
def test_pdf_highlighted_rows_inside_rule_table():
    """세 줄 표 안에 배경 칠한 강조 행이 있어도 한 표이고, 강조 행 세로 변이 열 경계다."""
    grids = _dp_grids("01030000000188")
    assert len(grids) == 1 and len(grids[0][0]) == 10 and len(grids[0]) == 14


@needs_dp
def test_pdf_hairline_rules_and_header_above_open_table():
    """위아래 좌표가 소수점 끝만 다른 가로선도 가로선이다. 선 친 표 위 선 없는 머리 줄은 머리행."""
    rows = [[c.replace("\n", " ") for c in r] for r in _dp_grids("01030000000182")[0]]
    assert rows[0] == ["", "OCR", "Recommendation", "Product semantic search"]
    assert [r[0] for r in rows[1:]] == ["Pack", "Application", "Highlight"]


@needs_dp
def test_pdf_bullet_gap_and_sliver_cell_are_not_columns():
    rows = _dp_grids("01030000000150")[0]
    assert len(rows[0]) == 2 and rows[3][1].startswith("●")


@needs_dp
def test_pdf_header_line_stuck_to_caption_paragraph():
    """표 제목 문단에 붙어 묶인 머리 줄도 머리행."""
    assert _dp_grids("01030000000116")[0][0] == ["Saccharometer", "DI Water", "Glucose Solution", "Yeast Suspension"]


@needs_dp
def test_pdf_filled_header_row_joins_unruled_body():
    """머리행만 채움 칸이고 본문은 선 없이 이어지는 표는 한 표다."""
    grids = _dp_grids("01030000000127")
    assert any(g[0] == ["Year", "3-Year", "5-Year", "7-Year"] and ["1", "33.0%", "20.00%", "14.29%"] in g for g in grids)


@needs_dp
def test_pdf_unruled_rows_packed_in_one_block():
    """줄 간격이 좁아 한 블록에 묶인 선 없는 표의 행들."""
    assert any(["2", "24 ml", "0 ml", "4 ml"] in g for g in _dp_grids("01030000000117"))


@needs_dp
def test_pdf_table_of_contents_is_not_a_table():
    """제목과 쪽 번호가 늘어선 차례는 표가 아니다."""
    assert _dp_grids("01030000000171") == [] and _dp_grids("01030000000155") == []


@needs_dp
def test_pdf_cell_padding_clip_is_not_a_row_rule():
    """Word 가 칸 안 글 영역에 위아래로 들여 건 클립은 행 경계가 아니다(한컴 칸 클립과 다르다)."""
    grids = _dp_grids("01030000000147")
    assert len(grids) == 1 and grids[0][0][:2] == ["Source\n(doc, report,\netc.)", "Year"]
    assert grids[0][1][1] == "2005"


@needs_dp
def test_pdf_bullet_list_is_not_a_table():
    """글머리 기호와 문장이 옆으로 늘어선 목록은 표가 아니다."""
    assert _dp_grids("01030000000069") == []


@needs_bench
def test_pdf_unruled_body_under_boxed_header():
    """머리행만 점선 칸이고 본문은 선 없이 굵은 틀선까지 이어지는 표(고령자 통계 65쪽)."""
    pdf = glob.glob(os.path.join(BENCH, "real2", "156783937", "*.pdf"))[0]
    rows = [r for b in parse(pdf).blocks if b.table and b.page == 64 for r in b.table.grid()]
    assert ["2005", "1,690", "3.5", "1,215", "2.5", "1,416", "2.9"] in rows


@needs_bench
def test_pdf_clip_rows_not_regrouped_by_wrapped_label():
    """칸 클립으로 갈린 계/남/여 행이 두 줄로 접힌 행 이름(세 행 병합) 때문에 하위 행 나누기로 다시 묶이지 않는다(사망원인통계 37쪽)."""
    pdf = glob.glob(os.path.join(BENCH, "real3", "156351825", "*.pdf"))[0]
    rows = [r for b in parse(pdf).blocks if b.table and b.page == 36 for r in b.table.grid()]
    sub = [r[1:3] for r in rows if r[0].startswith("결장")]
    assert sub == [["계", "17.1"], ["남", "19.2"], ["여", "15.0"]]


ICDAR = os.path.join(BENCH, "public", "icdar2013")


@pytest.mark.skipif(not os.path.isdir(ICDAR), reason="ICDAR 2013 샘플이 없다")
@pytest.mark.parametrize("pdf,row", [
    # 값 위에 묶음 소제목 두 줄(All Randomly Assigned / 3-Year-Old Cohort)이 있어도 하위 행을 나눈다
    ("competition-dataset-us/us-008.pdf", ["Control Group", "17.3%", "82.7%", "100%"]),
    # 값을 비운 행(‡) 하나 때문에 격자 전체를 버리지 않는다
    ("competition-dataset-us/us-002.pdf", ["Doctorate-granting", "31.7", "21.7", "21.3", "25.2", "8,200"]),
    # 40자 넘는 이름(Gross financial wealth-end of 2007)이 있어도 숫자가 쌓인 열이 여럿이면 문단이 아니다
    ("competition-dataset-eu/eu-027.pdf", ["Household income (Euro/month)", "2,127", "1,389", "22", "22,500"]),
    # 두 줄 이름 옆 값이 가운데 줄에 찍히고 머리 Year 가 여러 열에 걸친 세 줄 표
    ("competition-dataset-us/us-023.pdf", ["Between-state income inequality (Gini index)", "0.0628", "0.0636"]),
    # 값이 숫자 아닌 하위 행($9,594 or less)
    ("competition-dataset-us/us-003.pdf", ["Lower middle", "$9,595–$17,992", "$22,401–$29,992"]),
    # 이름 | 설명 문장 표: 접힌 이름·설명은 소문자로 이어지고 묶음 제목은 제 행
    ("competition-dataset-us/us-019.pdf", ["Ages 18 and 19", "Remains between 17.7% and 26.8%"]),
    # 낱말 간격으로만 떨어진 숫자 열(221 37.6 (32.6–42.6))
    ("competition-dataset-us/us-025.pdf", ["District of Columbia", "1,144", "193.5", "(182.2–204.8)", "221", "37.6"]),
    # 두 열에 걸친 머리(Other) 아래 숫자 열, 오른쪽 세로 테두리 없는 끝 열(Total population)
    ("competition-dataset-us/us-033.pdf", ["2-11months", "1,087,948", "1,022,490", "292,652", "255,744", "188,980",
                                           "150,760", "165,949", "185,667", "3,350,188"]),
])
def test_pdf_unruled_subrows_split(pdf, row):
    rows = [r[:len(row)] for b in parse(os.path.join(ICDAR, pdf)).blocks if b.table for r in b.table.grid()]
    assert row in rows


@pytest.mark.skipif(not os.path.isdir(ICDAR), reason="ICDAR 2013 샘플이 없다")
def test_pdf_group_heading_lines_stay_rows():
    """값 위 묶음 소제목 줄은 첫 행 이름에 붙지 않고 제 행으로 남는다(us-008)."""
    rows = [r for b in parse(os.path.join(ICDAR, "competition-dataset-us/us-008.pdf")).blocks
            if b.table and b.page == 2 for r in b.table.grid()]
    assert ["All Randomly Assigned (N=4,667):", "", "", ""] in rows
    assert ["Head Start Group", "85.1%", "14.9%", "100%"] in rows


@pytest.mark.skipif(not os.path.isdir(ICDAR), reason="ICDAR 2013 샘플이 없다")
def test_pdf_monospace_text_table():
    """공백 글자로 칸을 맞춘 고정폭 표(us-034): 줄 끝 숫자 열의 오른쪽 끝이 맞는 자리로 칸을 나눈다."""
    grids = [b.table.grid() for b in parse(os.path.join(ICDAR, "competition-dataset-us/us-034.pdf")).blocks if b.table]
    assert ["0.56-0.74 .............", "30", "33", "36", "39", "42", "45", "48"] in grids[0]
    assert grids[1][0] == ["Proportion", "1.7", "1.8", "1.9", "2.0", "2.5", "3.0", "3.5"]


def test_pdf_unstack_joins_vertical_hangul_only():
    from process_gpt_document_parser.formats.pdf import _unstack

    assert _unstack("연\n간") == "연간"
    assert _unstack("1\n2") == "1\n2"  # 숫자 하위 행은 그대로


@needs_bench
def test_pdf_dotted_rule_table_keeps_columns():
    """안쪽 세로선이 점선인 표(GRDP 권역별 성장률)가 칸마다 갈린다."""
    pdf = glob.glob(os.path.join(REAL, "grdp_2026q2", "*.pdf"))[0]
    rows = next(b.table.grid() for b in parse(pdf).blocks
                if b.table and b.page == 7 and "GRDP" in b.table.to_markdown())
    row = next(r for r in rows if r[0] == "전국")
    assert row == ["전국", "1.5", "2.1", "-7.6", "2.5", "3.8", "7.1", "-3.9", "3.2", "3.7", "6.5", "-3.5", "3.4"]


def test_pdf_fragments_measure_visible_gaps():
    from process_gpt_document_parser.formats.pdf import _fragments, _free_spans, _spread_syllable

    def ch(t, x0, x1):
        return {"text": t, "x0": x0, "x1": x1}

    # 공백 글자가 있어도 보이는 글자 사이 간격으로 끊는다(값 [증감])
    line = [ch("0", 0, 5), ch(".", 5, 7), ch("3", 7, 12), ch(" ", 12, 15), ch("[", 30, 33), ch("0", 33, 38), ch("]", 38, 41)]
    assert [f[2] for f in _fragments(line, 10)] == ["0.3", "[0]"]
    assert [f[2] for f in _fragments([ch("간", 0, 9), ch(" ", 9, 12), ch("질", 13, 22)], 10)] == ["간 질"]
    assert _spread_syllable("구") and not _spread_syllable("9")  # 한 자리 값은 균등 배분이 아니다
    assert _free_spans(0, 10, [(2, 4), (3, 6)]) == [(0, 2), (6, 10)]


@needs_bench
def test_pdf_unruled_columns_and_wrapped_labels():
    """선 없는 열(심장 질환 하위 셋)·병합 연도 칸·이름 | 값 이 번갈아 오는 순위 표(사망원인통계, 칸마다 클립)."""
    pdf = glob.glob(os.path.join(BENCH, "real3", "156351825", "*.pdf"))[0]
    grids = [b.table.grid() for b in parse(pdf).blocks if b.table and b.page in (8, 15)]
    rows = [r for g in grids for r in g]
    # 원본 HWPX 처럼 성별 칸은 행마다 따로(첫 행에만 글), 연도는 두 열 병합
    assert ["", "2018년", "2018년", "130.8", "16.5", "63.9", "25.6", "38.3", "46.7"] in rows
    assert ["8", "간 질환", "14.5", "만성 하기도\n질환", "13.2", "만성 하기도\n질환", "6,608", "2.2", "12.9", "↓-2", "-"] in rows


def test_pdf_strip_paths_keeps_text_and_state():
    """경로 명령을 걷어 내도 글 문자열·글 연산자·좌표 변환은 남는다(문자열 안의 '1 2 m' 도)."""
    from process_gpt_document_parser.formats.pdf_objects import strip_stream

    src = (b"q 0.5 w [3 2] 0 d 0 0 1 RG 10 20 m 30 40 l S Q "
           b"q 1 0 0 1 5 5 cm 0 0 10 10 re W n /Cs1 cs 0.2 0.3 0.4 sc "
           b"BT /F1 12 Tf 2 Tc (1 2 m) Tj [(a) -120 (b)] TJ <0041> Tj ET Q")
    out = strip_stream(src)
    for kept in (b"1 0 0 1 5 5 cm", b"BT", b"/F1 12 Tf", b"2 Tc", b"(1 2 m) Tj", b"[(a) -120 (b)] TJ", b"<0041> Tj", b"ET"):
        assert kept in out
    for gone in (b" re", b" w ", b" RG", b" l ", b" S ", b" W ", b" sc"):
        assert gone not in out
    assert out.count(b"q") == out.count(b"Q") == 1  # 비게 된 q … Q 는 지운다


@pytest.mark.skipif(not os.path.isdir(DP_PDFS) or not os.path.isdir(os.path.join(BENCH, "real5")), reason="벤치 샘플이 없다")
def test_pdf_pdfium_paths_match_pdfminer():
    """선·사각형을 pdfium 으로 읽어도 pdfminer 로 다 읽은 것과 출력이 같다(한컴 칸 클립·그래프·영문 표)."""
    from process_gpt_document_parser.formats import pdf as pdf_format

    files = [os.path.join(DP_PDFS, "01030000000187.pdf"), os.path.join(DP_PDFS, "01030000000116.pdf"),
             sorted(glob.glob(os.path.join(BENCH, "real5", "**", "*.pdf"), recursive=True))[0],
             glob.glob(os.path.join(BENCH, "real3", "156351825", "*.pdf"))[0]]
    for path in files:
        out = {}
        for engine in ("pdfminer", "pdfium"):
            pdf_format.ENGINE = engine
            try:
                out[engine] = parse(path).to_markdown()
            finally:
                pdf_format.ENGINE = "pdfium"
        assert out["pdfium"] == out["pdfminer"], path
