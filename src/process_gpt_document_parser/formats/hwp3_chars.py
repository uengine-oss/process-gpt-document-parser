"""HWP 3.x 글자 코드(hchar) → 유니코드.

명세는 hchar 를 "한글의 내부 코드, 한글 설명서 참조"로만 적었다. 아래 영역 규칙은 같은 문서를 한컴이 HWPX·HWP5 로
변환한 본과 맞대어 확인했다(docs/spec.md "HWP 3.x 글자 코드"). 그 밖의 코드는 None 으로 돌려 경고로 남긴다.
"""
from __future__ import annotations

from typing import Optional

# 한컴 변환본과 맞대어 확인한 코드. KS X 1001 행 규칙보다 앞선다(가운뎃점·물결은 한컴이 고른 유니코드를 따른다)
_OBSERVED = {
    0x81: "“", 0x82: "”", 0x83: "‘", 0x84: "’",
    0x2F11: "◦", 0x3404: "․", 0x340D: "～", 0x37ED: "═",
}
_SYMBOL_BASE = 0x3400  # KS X 1001 기호 행, 행마다 0x60 칸
_SYMBOL_ROWS = {1, 2, 3, 5, 6, 8}  # 한컴 변환본으로 확인한 행만. 11행 자리에 가타카나가 아닌 괘선이 있었다
_HANJA_BASE, _HANJA_COUNT = 0x4000, 4888  # KS X 1001 한자 42~93행을 순서대로


def _ksx1001(row: int, col: int) -> Optional[str]:
    if not (1 <= col <= 94):
        return None
    try:
        return bytes((0xA0 + row, 0xA0 + col)).decode("euc-kr")
    except UnicodeDecodeError:
        return None


def decode_hchar(c: int) -> Optional[str]:
    """못 바꾸면 None."""
    if c < 0x80:
        return chr(c)
    if c in _OBSERVED:
        return _OBSERVED[c]
    if 0x2000 <= c < 0x2300:  # 유니코드 값 그대로(일반 구두점·화살표·수학 기호). 0x2022·0x2219 로 확인
        return chr(c)
    if c >= 0x8000:  # 상용 조합형 한글(KS X 1001 부속서 3)
        try:
            return bytes((c >> 8, c & 0xFF)).decode("johab")
        except UnicodeDecodeError:
            return None
    if _SYMBOL_BASE <= c < 0x4000:
        row, col = divmod(c - _SYMBOL_BASE, 0x60)
        return _ksx1001(row + 1, col) if row + 1 in _SYMBOL_ROWS else None
    if _HANJA_BASE <= c < _HANJA_BASE + _HANJA_COUNT:
        row, col = divmod(c - _HANJA_BASE, 94)
        return _ksx1001(42 + row, col + 1)  # 독음이 다른 중복 한자는 호환 영역 글자 그대로(한컴 변환본과 같다)
    return None
