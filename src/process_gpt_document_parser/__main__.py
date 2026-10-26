"""python -m process_gpt_document_parser <파일> [--json] [--out 파일]"""
from __future__ import annotations

import argparse
import json
import sys

from . import parse


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="process-gpt-document-parser")
    ap.add_argument("path")
    ap.add_argument("--json", action="store_true", help="블록 구조를 JSON 으로")
    ap.add_argument("--out", help="출력 파일(기본: 표준 출력)")
    ap.add_argument("--table-format", default="markdown", choices=["markdown", "markdown-first", "html"])
    args = ap.parse_args(argv)

    doc = parse(args.path)
    text = json.dumps(doc.to_dict(), ensure_ascii=False, indent=2) if args.json else doc.to_markdown(args.table_format)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    for w in doc.warnings:
        print(f"[warning] {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
