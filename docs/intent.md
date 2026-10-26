# Intent: 자체 문서 파싱 엔진

Author: uengine-csh. Status: accepted (2026-10-16).

## Problem

memento 의 파서는 앱 안에 흩어져 있고, 핵심 처리를 라이선스·배포 제약이 있는 외부 코드와 실행 파일이 한다.

- PDF: PyMuPDF — AGPL-3.0. 배포·SaaS 시 전체 소스 공개 의무, 아니면 Artifex 상용 라이선스.
- HWP: extract-hwp(BSD)·rhwp 변환에 기댄다. DOC: LibreOffice 로 DOCX 변환 후 파싱.
- HWPX·DOCX 만 자체 구현.

## Proposed outcome

HWP·HWPX·DOC·DOCX·PDF 를 하나의 API·출력 구조로 읽는 독립 패키지 `process-gpt-document-parser`.

- 핵심 파싱은 우리 코드. HWP·DOC 은 공개 명세(한컴 HWP 5.0, MS-DOC)로 직접 구현.
- 공개SW 는 허용형만 쓰고 숨기지 않고 밝힌다(NOTICE.md).
- memento 가 첫 사용자로 이 패키지로 갈아탄다.

## Affected users and systems

- process-gpt-memento: 인제스트 파서 교체 대상.
- 운영: LibreOffice·rhwp 같은 외부 실행 파일 의존이 없어진다.

## Constraints

- AGPL·GPL 계열 의존성 금지.
- memento 파서보다 품질이 떨어지면 안 된다(같은 문서로 비교).
- 라이브러리는 모델을 직접 부르지 않는다. 그림 설명·OCR 은 호출자가 콜백으로 넘긴다.

## Open questions

- Word 6/95 DOC 지원 여부(POI 시험 데이터 160개 중 12개).
- 큰 병합 칸이 많은 서식의 표 출력 형식(글이 약 20배로 부풀어 오름).
- 문단 간격 단서가 없는 PDF 의 문단 나누기.
