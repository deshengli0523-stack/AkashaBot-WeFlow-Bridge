import hashlib


def pseudonym(value: object) -> str:
    text = "" if value is None else str(value)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"id:{digest}"


def message_meta(content: object) -> str:
    text = "" if content is None else str(content)
    kind = "empty" if not text else "text"
    return f"type={kind} length={len(text)}"
