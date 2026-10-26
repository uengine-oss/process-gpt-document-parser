# AGENTS.md

HWP·HWPX·DOC·DOCX·PDF 를 같은 블록 구조로 읽는 파서. 무엇을·왜: [docs/intent.md](docs/intent.md),
동작 명세: [docs/spec.md](docs/spec.md), 남은 일: [docs/plan.md](docs/plan.md), 측정: [docs/VERIFICATION.md](docs/VERIFICATION.md).

## 명령

```bash
uv venv -p 3.11 .venv && uv pip install -p .venv -e ".[all,dev]"
.venv/Scripts/python -m pytest -q                                   # 시험 (Windows 경로)
.venv/Scripts/python -m process_gpt_document_parser <파일> [--json] [--table-format markdown|markdown-first|html]
```

샘플·비교 스크립트는 저장소 밖 `../../test-field/parser-bench/` (`real/`, `data/synth/`, `external/`,
`document_parser_cmp/README.md`). 시험은 그 폴더가 없으면 건너뛴다(`PARSER_BENCH` 로 경로 지정).

## 구조

- `model.py` 출력 모델·표 렌더, `__init__.py` 포맷 판별·`parse()`·오류 감싸기, `images.py` 그림 판별·콜백 병렬 호출
- `formats/{hwp,hwpx,doc,docx,pdf}.py` 포맷마다 `parse(data: bytes) -> Document` (PDF 만 ocr·images 인자)
- `formats/hwp3.py`·`hwp3_chars.py` HWP 3.x(한글 97 이하). `hwp.parse` 가 서명을 보고 넘긴다
- `formats/pdf_layout.py` 글자→줄→블록, 괘선 없는 표. `formats/pdf_subrows.py` 하위 행 나누기(memento 에서 옮김)
- `formats/pdf_objects.py` pdfium 선·사각형·곡선, 내용 스트림에서 경로 걷어 내기·칸 클립 읽기(VERIFICATION "pdfium 엔진")

## 규칙

- 의존성은 허용형 라이선스만. 새로 넣으면 NOTICE.md 에 적는다. AGPL·GPL(PyMuPDF, pyhwp 등) 금지.
- HWP·HWPX·DOC·DOCX 에 외부 실행 파일(LibreOffice·한컴·rhwp)이나 다른 파서 라이브러리를 쓰지 않는다.
- 라이브러리 안에서 모델·네트워크를 부르지 않는다. 그림 설명·OCR 은 콜백.
- PDF 튜닝은 `document_parser_cmp/pdf_split.json` 의 dev 쪽에서만 보고, holdout 은 마지막에 한 번 잰다.
- 출력 동작을 바꾸면 docs/spec.md 를 같은 커밋에서 고치고, 수치가 바뀌면 docs/VERIFICATION.md 에 명령과 함께 남긴다.
- 포맷 파서 안의 오류는 `ParseError` 계열로. 내부 예외는 `parse()` 가 감싼다.
- 휴리스틱 임계값을 특정 문서에 맞춰 고르지 않는다. 바꾸면 parser-bench 홀드아웃까지 다시 잰다.
- 코드 주석은 국소 "왜" 한 줄. 설계 근거는 docs/ 에.
- 1분 넘게 걸릴 측정·채점은 반드시 진행을 남기고 감시한다:
  - 스크립트가 N건마다 `[n/전체 경과초]` 를 `flush=True`(또는 `python -u`)로 찍는다.
  - 출력은 로그 파일로 받는다. `| grep`·`| tail` 로 넘기면 끝날 때까지 버퍼에 막혀 진행이 안 보인다.
  - 백그라운드로 돌리고 진행·오류(`Traceback|Error`) 줄을 감시한다. 진행이 멈추면 CPU 시간으로 살아 있는지 확인한다.
  - 문서마다 독립인 채점은 프로세스 풀로 병렬. 같은 출력은 다시 재지 않는다(바뀐 문서만).
  - 전체를 다시 재는 스크립트(`compare3.py`·`bench_score_all.py`)를 한두 파서 확인에 쓰지 않는다. 바뀐 쪽만 재는 `ai_table/ai_delta.py` 를 쓴다.

## 에이전트가 여기서 실제로 틀린 것

- 셸 heredoc 으로 파이썬 파일을 쓰면 `"\n"` 같은 이스케이프가 실제 줄바꿈·제어 문자로 박힌다(`\u` 유니코드 이스케이프도 실제 글자로). 코드는 Edit/Write 로만 쓰고, 보이지 않는 글자는 쓴 뒤 grep 으로 확인한다.
- Windows 에서 `open(p, "w")` 로 다시 쓰면 CRLF 가 섞인다. `newline=""` 를 쓰거나 Edit 로.
- memento 스크립트를 콘솔에서 돌리면 cp949 로 출력하다 죽는다. `PYTHONIOENCODING=utf-8 PYTHONUTF8=1`.
- Windows 는 파일명 대소문자를 구분하지 않아 `*.doc` 과 `*.DOC` glob 이 같은 파일을 두 번 잡는다.
- 경로에 `[` `]` 가 든 파일(법령 서식)은 glob 패턴으로 쓰면 안 잡힌다. `glob.escape` 또는 `os.listdir`.
- "잃은 글" 지표만 보면 글을 다 쏟아내는 변경이 좋아 보인다. 정밀도·순서 점수를 같이 본다.
- 병합 칸을 채운 정답과 비교하면 서식 문서의 재현율이 낮게 나온다(측정 착시). 칸 글 단위로 확인한다.
- HWP 3.x 글자 코드를 표준 코덱 대응만 믿고 넓히면 틀린 글자가 조용히 섞인다(11행이 가타카나가 아니라 괘선이었다).
  새 영역은 한컴 변환본과 맞대어(`cmp_hwp3.py`) 확인한 뒤에만 연다.
- 튜닝용에서 docling 을 앞선 결과(한국 표 78.7 vs 75.3)가 평가용에서는 72.5 vs 81.9 였다. 튜닝용 문서에 많던 양식(점선)에 맞은 것.
  평가용 실패를 보고 고치면 그 문서는 튜닝용이 되므로 새 평가용(`pdf_split.json` fresh)을 따로 받는다.
- 낱말 재현율 측정이 `<[^>]+>` 로 태그를 지워 "<" 하나가 생기면 다음 ">" 까지 본문을 지웠다(없는 손실). 태그만 지운다.
- 벤치 러너 출력을 `| grep` 으로 넘기면 olmOCR 실행이 소리 없이 중간에 끝났다(로그 파일로 돌리면 끝까지 간다). 로그로 돌리고
  마지막 `files` 줄과 파일 수를 확인한다. 이어 돌리면 캐시된 것은 건너뛴다.
- 한컴 PDF 의 칸 클립을 `re W n` 꼴로만 세서 "한컴 PDF 엔 칸 클립이 없다" 고 잘못 결론냈다. 한컴은 `m l l l h W* n` 경로로 건다.
  PDF 안 정보를 찾을 때는 연산자 꼴을 가정하지 말고 실제 그리기 명령을 찍어 본다.
- 비교 러너는 editable 설치를 쓴다. 파싱이 도는 중에 소스를 고치면 앞뒤 코드가 섞인 결과가 나온다. 측정이 끝난 뒤 고친다.
- GPU 모델 파서(모델 모드·docling)를 러너 기본 작업 수로 돌리면 프로세스마다 모델을 GPU 에 올려 메모리가 차고 거의 멈춘다
  (30분에 2건). `--workers 2` 로 돌린다.
- 러너가 "errors 0" 이라고 끝나도 출력이 빠질 수 있다(작업 프로세스가 죽은 뒤 555건이 조용히 누락). 채점 전에 파서×벤치마다
  `.md`+`.err` 수가 문서 수와 같은지 본다. `bench_stop.ps1` 은 비교 폴더의 python 을 전부 죽이므로 채점 중에는 쓰지 않는다.
- olmOCR `load_tests` 는 파일 줄 순서를 바꿔 돌려준다. 시험 파일에서 문항을 빼거나 고칠 때 줄 번호로 짝짓지 말고 `id` 로 고른다.
- 표 시험 생성기가 정답의 빈 칸(None)을 빼면 HTML 에서 그 행이 왼쪽으로 밀려 이웃 정답이 틀어진다(ICDAR 22문항). 빈 자리도 빈 칸으로 넣는다.
- AI 모드 비교를 kordoc 까지 세 파서 전부 다시 재는 스크립트로 돌리고 `| grep` 으로 받아, 20분 넘게 진행도 안 보인 채 기다렸다
  (단일 프로세스·출력 버퍼). 바뀐 문서만 병렬로 재고 진행을 찍으니 몇 분이었다.
- 표 시험을 다시 만들면(`korean_bench.py --build`) 끝에 52건 점수 파일(README 차트 원본)까지 새로 쓴다. 문서 범위를 바꿨으면
  점수 파일을 다시 만들어 README 수치와 같은지 본다.

## 완료 전

1. `pytest -q` 통과 결과를 붙인다.
2. 파싱 결과가 바뀌는 변경이면 `document_parser_cmp/` 비교를 다시 돌려(HWP↔HWPX `cmp_hwp.py`, HWP 3.x `cmp_hwp3.py`,
   DOC `cmp_doc.py`·`poi_doc_score.py`, PDF `run_parsers.py` → `pdf_split_score.py`) 전후 수치를 보고한다. 실패하면 시험이 아니라 코드를 고친다.
3. 버그를 고치면 그 버그를 재현하는 시험을 먼저 넣는다.
