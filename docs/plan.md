# Plan

출처: [intent.md](intent.md), [spec.md](spec.md). 검증 결과: [VERIFICATION.md](VERIFICATION.md).

## 1차 (2026-10-16, 완료 — 커밋 f94529c)

1. 공통 출력 모델·`parse()`·포맷 판별·오류 3종 (`model.py`, `__init__.py`, `errors.py`)
2. HWPX·DOCX: memento `app/plugins/parsers/hwpx_structured.py`·`docx_structured.py` 에서 옮김(stdlib XML 로)
3. HWP 5.0 자체 구현 → 같은 문서 HWPX 와 출력 일치 확인
4. DOC 자체 구현 → LibreOffice 로 만든 DOC 을 원본 DOCX 와, Word 가 만든 DOC(Apache POI 시험 데이터)을 LibreOffice 텍스트와 비교
5. PDF: memento PyMuPDF 파이프라인(읽기 순서·괘선 없는 표·하위 행·머리말·그림 영역·스캔 OCR)을 pdfplumber+pypdfium2 로 재구현
   → 실제 PDF 18건을 같은 문서 HWPX 글과 비교해 memento 이상 확인
6. 표 출력 형식 3종(markdown·markdown-first·html) LLM 질의응답 비교 → 기본 markdown
7. README·NOTICE·VERIFICATION·시험

## 2차 (2026-10-19)

1. HWP 3.x 자체 구현(`formats/hwp3.py`·`hwp3_chars.py`) → 한컴 변환본 9쌍과 비교(VERIFICATION "HWP 3.x")
2. PDF 격차 원인 분류(dev 절반) → 글자 정리 규칙(합자·발음 기호·줄 끝 하이픈) + 선택 `layout` 콜백
3. docling 비교 채점(olmOCR·한국 PDF)
4. 한국 PDF 표: 점선·숨은 칸 경계 등 → 처음 보는 15건(real4) 36.0 → 66.3%(docling 66.1%). Camelot 비교·표 찾기 콜백
5. 표 구조 모델 콜백(TableFormer, finer): real4 76.1%, 52건 83.0%(docling 78.3%)

## 남은 일 (우선순위 순)

1. **memento 교체**: `document_processor.py`·`plugins/parsers` 를 이 패키지로. memento 쪽 형식(PDF 쪽별 Document +
   blocks_json, 그 밖 BLOCKS_KEY)으로 변환, `PARSER_VERSION` 올림, PyMuPDF·extract-hwp 제거.
   완료 조건: parser-bench `run_parse.py` → `score_synth.py`·`score_real.py` 가 교체 전보다 나쁘지 않을 것.
   memento 표 LLM(`MEMENTO_TABLE_LLM`)은 PyMuPDF 페이지 객체를 쓰므로 따로 옮겨야 한다.
2. VLM 을 OCR 대신: 그림 벤치(BENCHMARKS "문서 속 그림")에서 `describe`+luna 가 PDF 99문항 중 94. 남은 일: 판정을 썸네일·
   해시로 싸게 먼저 해서 건너뛸 그림(로고·글 레이어와 같은 띠)이 새는 것 줄이기, 사내 모델로 같은 벤치, 스캔 쪽 전체를 VLM 으로
   읽는 벤치. 레이아웃·표 모델 콜백과 AI 표 모드는 뺐다(VERIFICATION "AI 모드·모델 모드 뺌").
   다음 표 튜닝 전에는 새 평가 문서를 또 받는다(real4 는 이제 본 문서).
3. PDF 스캔 쪽: RapidOCR 콜백 예(`ocr_rapid.py`)로 쟀다(VERIFICATION). README 사용 예에 넣을지 정한다.
4. 각주 처리를 포맷 간에 맞추기(spec "우려").
5. 큰 병합 칸이 많은 서식의 표 출력: 서식 질문셋을 만들어 markdown·html·markdown-first 정답률과 토큰을 잰다.
6. 배포: github.com/uengine-oss/process-gpt-document-parser 의 git 태그로 설치한다(v0.2.0). PyPI 등록은 필요할 때.
7. 필요하면: Word 6/95 DOC, PDF 문단 나누기(줄 들여쓰기·줄 끝 길이 단서).

## 위험

- PDF 는 pdfplumber(pdfminer) 판단에 기댄다. 새 양식 PDF 는 parser-bench 홀드아웃(real2·real3·icdar)으로 다시 재야 한다.
- 비교 스크립트·샘플은 저장소 밖(`test-field/parser-bench/`)이라 그 폴더가 없으면 수치를 재현할 수 없다.
