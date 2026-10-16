from __future__ import annotations

import json
import os
import re
import sys
from typing import Final

AGENT_LINE_BYTES: Final = 480


def sanitize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        char
        for char in value
        if char in "\n\t"
        or (
            ord(char) >= 32
            and not 127 <= ord(char) <= 159
            and not 0xD800 <= ord(char) <= 0xDFFF
        )
    )


def one_line(value: object) -> str:
    return " ".join(sanitize_text(value).split())


def dump_json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    # JSON leaves DEL and C1 controls raw, and terminals may act on them.
    return re.sub("[\x7f-\x9f]", lambda match: f"\\u{ord(match.group()):04x}", text)


def split_long_line(line: str, max_bytes: int) -> list[str]:
    chunks = []
    while len(line.encode()) > max_bytes:
        bytes_used = 0
        cut = 0
        for index, char in enumerate(line):
            char_bytes = len(char.encode())
            if bytes_used + char_bytes > max_bytes:
                break
            bytes_used += char_bytes
            cut = index + 1

        whitespace = max(line.rfind(" ", 0, cut), line.rfind("\t", 0, cut))
        if whitespace > 0 and len(line[:whitespace].encode()) >= max_bytes // 2:
            chunks.append(line[:whitespace])
            line = line[whitespace + 1 :]
        else:
            chunks.append(line[:cut])
            line = line[cut:]
    chunks.append(line)
    return chunks


def write_output(value: str, raw: bool = False) -> None:
    """Write text for reading, or `raw` JSON for parsers, which must reach them unaltered."""
    if not raw:
        value = sanitize_text(value)
        # hax elides line content beyond 500 bytes from tool results.
        if os.environ.get("AI_AGENT") == "hax":
            parts = (split_long_line(line, AGENT_LINE_BYTES) for line in value.splitlines())
            value = "\n".join(part for line_parts in parts for part in line_parts)
    sys.stdout.write(value if value.endswith("\n") else value + "\n")
