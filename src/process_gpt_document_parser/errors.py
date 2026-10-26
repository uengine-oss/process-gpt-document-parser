class ParseError(Exception):
    """문서를 읽지 못했다."""


class UnsupportedFormatError(ParseError):
    """지원하지 않는 포맷이다."""


class EncryptedDocumentError(ParseError):
    """암호가 걸려 있어 읽을 수 없다."""
