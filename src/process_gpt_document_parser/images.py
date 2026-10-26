"""그림 형식 판별과 그림 설명 콜백 호출."""
from __future__ import annotations

import hashlib
import io
import struct
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from .model import Document

# (그림 bytes, mime) → 설명. 라이브러리는 모델을 부르지 않고 호출자가 넘긴 함수만 쓴다.
Describe = Callable[[bytes, str], str]
# 쪽 PNG → 받아쓴 글
Ocr = Callable[[bytes], str]

# 짧은 변이 이보다 작은 그림은 아이콘·장식으로 보고 설명하지 않는다
MIN_PX = 80

_MIME_BY_EXT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "bmp": "image/bmp", "tif": "image/tiff", "tiff": "image/tiff", "webp": "image/webp",
    "wmf": "image/wmf", "emf": "image/emf",
}


def mime_from_name(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else name.lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def sniff_mime(data: bytes, fallback: str = "application/octet-stream") -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\xd7\xcd\xc6\x9a" or data[:4] == b"\x01\x00\x09\x00":
        return "image/wmf"
    if data[:4] == b"\x01\x00\x00\x00" and data[40:44] == b" EMF":
        return "image/emf"
    return fallback


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """헤더만 읽어 (가로, 세로). 모르는 형식이면 None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w), abs(h)
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except struct.error:
        return None
    return None


def worth_describing(data: bytes) -> bool:
    """설명할 만한 크기인가. 크기를 못 읽으면 설명한다(놓치는 쪽보다 한 번 더 부르는 쪽이 낫다)."""
    size = image_size(data)
    return size is None or min(size) >= MIN_PX


def to_web_image(data: bytes, mime: str) -> Tuple[bytes, str]:
    """모델이 못 받는 형식(bmp·tiff 등)을 PNG 로. Pillow 가 없거나 실패하면 그대로."""
    if mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        return data, mime
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue(), "image/png"
    except Exception:
        return data, mime


def run_parallel(tasks: List[Tuple[str, Callable[[], str]]], max_workers: int) -> Dict[str, str]:
    """(키, 함수) 목록을 병렬로. 실패한 작업은 빈 글로 두고 계속한다."""

    def run(task):
        key, fn = task
        try:
            return key, (fn() or "").strip()
        except Exception as exc:  # 콜백 실패가 파싱 전체를 막지 않게
            print(f"[document-parser] {key} 실패: {exc}", file=sys.stderr)
            return key, ""

    if not tasks:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return dict(pool.map(run, tasks))


def describe_images(doc: Document, describe: Describe, max_workers: int = 4) -> None:
    """문서 그림에 설명을 단다. 같은 그림은 한 번만 부르고 설명은 처음 나온 자리에만 둔다."""
    first: Dict[str, object] = {}
    tasks = []
    for img in doc.images:
        if img.description or not worth_describing(img.data):
            continue
        digest = hashlib.sha1(img.data).hexdigest()
        if digest in first:
            continue
        first[digest] = img
        data, mime = to_web_image(img.data, img.mime)
        tasks.append((digest, (lambda d=data, m=mime: describe(d, m))))
    results = run_parallel(tasks, max_workers)
    for digest, img in first.items():
        img.description = results.get(digest) or None
