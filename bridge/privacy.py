import hashlib
import json
import re


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9._-]{8,}"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"
    ),
    re.compile(
        r"""(?ix)
        (["']?)
        (?:api[\s_-]?key|access[\s_-]?token|password|secret|token)
        \1
        \s*[:=]\s*
        (["']?)
        [^"',;\s}&]+
        \2
        """
    ),
)
_BACKSLASH = chr(92)
_UNQUOTED_PATH_TERMINATORS = frozenset("\"'\r\n\t,;}]，。！？；")
_PATH_SEPARATORS = (_BACKSLASH, "/")
_NON_FILE_URI_BEFORE_PATH = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*):/{2}[^\s\"']*$"
)
_PATH_TEXT_BOUNDARY_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "but",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "is",
        "please",
        "reply",
        "should",
        "that",
        "the",
        "then",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "would",
    }
)
_PATH_TEXT_BOUNDARY_PREFIXES = (
    "然后",
    "并且",
    "但是",
    "不过",
    "接着",
    "随后",
    "之后",
    "请",
    "再",
    "回复",
    "帮我",
    "告诉我",
)


def pseudonym(value: object) -> str:
    text = "" if value is None else str(value)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"id:{digest}"


def message_meta(content: object) -> str:
    text = "" if content is None else str(content)
    kind = "empty" if not text else "text"
    return f"type={kind} length={len(text)}"


def redact_log_text(value: object, *, redact_paths: bool = True) -> str:
    """Redact credentials and local paths while preserving ordinary chat text."""
    text = "" if value is None else str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _redact_windows_paths(text) if redact_paths else text


def _redact_windows_paths(text: str) -> str:
    """Redact drive and UNC paths without embedding private path examples."""
    output = []
    index = 0
    while index < len(text):
        drive_path = (
            index + 2 < len(text)
            and text[index].isalpha()
            and text[index + 1] == ":"
            and text[index + 2] in _PATH_SEPARATORS
            and (
                index == 0
                or not (text[index - 1].isalnum() or text[index - 1] == "_")
            )
        )
        unc_path = (
            (
                text.startswith(_BACKSLASH * 2, index)
                or text.startswith("//", index)
            )
            and (index == 0 or text[index - 1] != ":")
        )
        uri_match = _NON_FILE_URI_BEFORE_PATH.search(text[:index])
        inside_non_file_uri = bool(
            uri_match and uri_match.group(1).lower() != "file"
        )
        if inside_non_file_uri:
            drive_path = False
            unc_path = False
        if not (drive_path or unc_path):
            output.append(text[index])
            index += 1
            continue

        end = index + (3 if drive_path else 2)
        quote = text[index - 1] if index > 0 and text[index - 1] in "\"'" else ""
        while end < len(text):
            if quote:
                if text[end] == quote:
                    break
            else:
                if text[end] in _UNQUOTED_PATH_TERMINATORS:
                    break
                if text[end].isspace():
                    next_start = end
                    while (
                        next_start < len(text)
                        and text[next_start].isspace()
                        and text[next_start] not in "\r\n\t"
                    ):
                        next_start += 1
                    next_end = next_start
                    while (
                        next_end < len(text)
                        and not text[next_end].isspace()
                        and text[next_end] not in _UNQUOTED_PATH_TERMINATORS
                    ):
                        next_end += 1
                    next_component = text[next_start:next_end]
                    current_component = text[
                        max(
                            text.rfind(_BACKSLASH, index, end),
                            text.rfind("/", index, end),
                        )
                        + 1 : end
                    ]
                    next_word = next_component.strip("()[]<>").lower()
                    text_boundary = (
                        next_word in _PATH_TEXT_BOUNDARY_WORDS
                        or next_component.startswith(_PATH_TEXT_BOUNDARY_PREFIXES)
                    )
                    contains_separator = any(
                        separator in next_component
                        for separator in _PATH_SEPARATORS
                    )
                    introduces_extension = (
                        "." in next_component and "." not in current_component
                    )
                    continues_extensionless = (
                        "." not in current_component
                        and not text_boundary
                    )
                    continues_path = (
                        contains_separator
                        or introduces_extension
                        or continues_extensionless
                    )
                    if not continues_path:
                        break
                    end = next_start
                    continue
            end += 1
        output.append("[REDACTED]")
        index = end
    return "".join(output)


def chat_record(
    *,
    event: str,
    scope: str,
    contact: object,
    body: object,
    status: str,
    sender: object = "",
) -> str:
    """Return one JSON log line containing a full chat audit record."""
    payload = {
        "event": str(event),
        "scope": str(scope),
        "contact": redact_log_text(contact),
    }
    if sender not in (None, ""):
        payload["sender"] = redact_log_text(sender)
    payload["status"] = str(status)
    payload["body"] = redact_log_text(body)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for separator in ("\u0085", "\u2028", "\u2029"):
        encoded = encoded.replace(separator, f"\\u{ord(separator):04x}")
    return encoded
