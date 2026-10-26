# 제3자 구성 요소와 참고 명세

process-gpt-document-parser — Copyright 2026 uEngine. Apache License 2.0([LICENSE](LICENSE)).

이 라이브러리는 아래 공개 소프트웨어를 의존성으로 쓴다. 소스에 포함(vendoring)하지 않고 패키지 의존성으로만 설치한다.
모두 상용 배포·SaaS 제공에 소스 공개 의무가 없는 허용형 라이선스다. AGPL·GPL 계열은 쓰지 않는다.

| 구성 요소 | 쓰는 곳 | 라이선스 | 설치 |
|---|---|---|---|
| olefile | HWP·DOC 의 OLE 컨테이너 읽기 | BSD-2-Clause | 기본 |
| pdfplumber | PDF 글자·선·표 찾기 | MIT | `[pdf]` |
| pdfminer.six | pdfplumber 의 PDF 해석기 | MIT | `[pdf]` |
| pypdfium2 (PDFium 바이너리 포함) | PDF 선·사각형 읽기, 쪽·그림 영역 렌더 | Apache-2.0 / BSD-3-Clause, PDFium 은 BSD-3-Clause | `[pdf]` |
| Pillow | 렌더한 쪽을 PNG 로, bmp·tiff 를 PNG 로 | MIT-CMU (HPND) | `[pdf]`, `[images]` |
| cryptography | 배포용 HWP 의 AES 복호화, pdfminer 의 암호 PDF | Apache-2.0 또는 BSD-3-Clause | `[hwp-distribution]`, `[pdf]` |

PDFium 바이너리에는 FreeType(FTL), libjpeg-turbo(IJG·BSD), OpenJPEG(BSD-2), Little CMS(MIT), libpng, zlib, ICU 등이
들어 있다. 각 라이선스 원문은 설치된 `pypdfium2-*.dist-info/licenses/` 에 있다. FreeType 은 FTL 을 택하며,
FTL 은 문서에 FreeType 사용 사실을 밝힐 것을 요구한다: "Portions of this software are copyright © The FreeType Project
(www.freetype.org). All rights reserved."

## 참고한 공개 명세

- **HWP 5.0·3.x**: 한글과컴퓨터 「한글 문서 파일 형식 5.0」, 「한글 문서 파일 형식 3.0 / HWPML」(revision 1.2,
  공개 문서). 한컴의 공개 조건에 따라 밝힌다:
  "본 제품은 한글과컴퓨터의 한/글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다."
- **HWPX**: KS X 6101 (OWPML).
- **HWP 3.x 글자 코드**: KS X 1001(완성형 기호·한자, 상용 조합형 부속서 3). 대응은 파이썬 표준 코덱(euc-kr·johab)을 쓴다.
- **DOC**: Microsoft Open Specifications [MS-DOC] Word (.doc) Binary File Format, [MS-ODRAW] Office Drawing Binary File Format.
- **DOCX**: ECMA-376 / ISO/IEC 29500 Office Open XML.

## 검증에 쓴 모델 (라이브러리에 들어 있지 않음)

docs 의 모델 수치는 아래 공개 모델을 콜백으로 꽂아 쟀다. 라이브러리는 이 모델·코드를 포함하지 않는다.
레이아웃·표 구조 콜백은 2026-10-23 뺐고, 그 수치는 기록으로만 남는다.

- 레이아웃 검출: docling-layout-heron (RT-DETRv2), Apache-2.0
- 표 구조 인식: TableFormer (docling-models), CDLA-Permissive-2.0 / Apache-2.0. 실행에 docling 파이프라인(MIT)을 썼다.
- OCR: RapidOCR (PaddleOCR 모델), Apache-2.0
