from __future__ import annotations

import base64
import binascii
import contextlib
import json
import mimetypes
import os
import re
import tempfile
from collections.abc import Sequence
from typing import Final, Optional, cast

from . import JsonObject, JsonValue
from .text import dump_json, one_line, sanitize_text

SIGNATURE_PARAMS: Final = 8
SUMMARY_CHARS: Final = 120


def resolve_ref(ref: str, defs: JsonObject) -> JsonValue:
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            return defs.get(ref[len(prefix) :])
    return None


def schema_type(schema: JsonValue, defs: JsonObject, depth: int = 0) -> str:
    """A compact TypeScript-like type for a JSON Schema, inlining objects up to depth 3."""
    if not isinstance(schema, dict):
        return "any"
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = resolve_ref(ref, defs)
        if target is None or depth >= 3:
            return ref.rsplit("/", 1)[-1]
        return schema_type(target, defs, depth + 1)
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        values = [json.dumps(value, ensure_ascii=False) for value in enum[:12]]
        return " | ".join(values + (["…"] if len(enum) > 12 else []))
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            types = dict.fromkeys(schema_type(option, defs, depth + 1) for option in options)
            return " | ".join(types)
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(one_line(k) for k in kind)
    if kind == "array":
        item = schema_type(schema.get("items"), defs, depth + 1)
        return f"({item})[]" if " | " in item else f"{item}[]"
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties and depth < 3:
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        fields = (
            f"{name}{'' if name in required else '?'}: {schema_type(value, defs, depth + 1)}"
            for name, value in properties.items()
        )
        return "{" + ", ".join(fields) + "}"
    return kind if isinstance(kind, str) else "any"


def schema_defs(schema: JsonObject) -> JsonObject:
    defs: JsonObject = {}
    for key in ("definitions", "$defs"):
        if isinstance(schema.get(key), dict):
            defs.update(cast(JsonObject, schema[key]))
    return defs


def parameters(tool: JsonObject) -> tuple[JsonObject, list[str], JsonObject]:
    schema = tool.get("inputSchema")
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties")
    required = schema.get("required")
    return (
        properties if isinstance(properties, dict) else {},
        [name for name in required if isinstance(name, str)] if isinstance(required, list) else [],
        schema_defs(schema),
    )


def signature(tool: JsonObject, limit: Optional[int] = None) -> str:
    properties, required, _ = parameters(tool)
    names = [name for name in properties if name in required]
    names += [f"{name}?" for name in properties if name not in required]
    if limit is not None and len(names) > limit:
        names = names[:limit] + ["…"]
    return f"{tool['name']}({', '.join(names)})"


def summary(description: object) -> str:
    text = one_line(description)
    end = re.search(r"(?<=[.!?])\s", text)
    sentence = text[: end.start()] if end else text
    return sentence if len(sentence) <= SUMMARY_CHARS else sentence[: SUMMARY_CHARS - 1] + "…"


def format_tools(tools: Sequence[JsonObject]) -> str:
    lines = []
    for tool in tools:
        text = summary(tool.get("description"))
        lines.append(signature(tool, SIGNATURE_PARAMS) + (f" — {text}" if text else ""))
    return "\n".join(lines) if lines else "(no tools)"


def format_tool(tool: JsonObject) -> str:
    lines = [signature(tool)]
    description = sanitize_text(tool.get("description")).strip()
    if description:
        lines.extend(["", description])
    properties, required, defs = parameters(tool)
    if properties:
        lines.extend(["", "Parameters:"])
    for name, schema in properties.items():
        line = f"  {name}{'' if name in required else '?'}: {schema_type(schema, defs, 1)}"
        if isinstance(schema, dict):
            if "default" in schema:
                line += f" = {json.dumps(schema['default'], ensure_ascii=False)}"
            text = one_line(schema.get("description"))
            line += f" — {text}" if text else ""
        lines.append(line)
    annotations = tool.get("annotations")
    if isinstance(annotations, dict):
        if annotations.get("readOnlyHint") is True:
            lines.extend(["", "Read-only."])
        elif annotations.get("destructiveHint") is True:
            lines.extend(["", "Destructive."])
    output = tool.get("outputSchema")
    if isinstance(output, dict):
        output_type = schema_type(output, schema_defs(output), 1)
        lines.extend(["", f"Structured output (--json .structuredContent): {output_type}"])
    return "\n".join(lines)


def save_blob(data: JsonValue, mime_type: JsonValue) -> str:
    try:
        content = base64.b64decode(str(data), validate=True)
    except (binascii.Error, ValueError):
        return "undecodable data"
    suffix = mimetypes.guess_extension(str(mime_type)) if isinstance(mime_type, str) else None
    fd, path = tempfile.mkstemp(prefix="hax-mcp-", suffix=suffix or ".bin")
    with os.fdopen(fd, "wb") as file:
        file.write(content)
    return f"saved to {path}"


def readable_text(value: JsonValue) -> str:
    text = sanitize_text(value)
    if text.lstrip()[:1] in ("{", "["):
        with contextlib.suppress(ValueError):
            return dump_json(json.loads(text))
    return text


def format_result(result: JsonObject) -> str:
    parts = []
    content = result.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(readable_text(block.get("text")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {save_blob(block.get('data'), block.get('mimeType'))}]")
        elif kind == "resource_link":
            name = one_line(block.get("name"))
            parts.append(f"[resource {one_line(block.get('uri'))}{' ' + name if name else ''}]")
        elif kind == "resource" and isinstance(block.get("resource"), dict):
            resource = cast(JsonObject, block["resource"])
            if "text" in resource:
                parts.append(readable_text(resource["text"]))
            else:
                saved = save_blob(resource.get("blob"), resource.get("mimeType"))
                parts.append(f"[resource {one_line(resource.get('uri'))} {saved}]")
        else:
            parts.append(f"[unsupported {one_line(kind)} content]")
    if not parts and "structuredContent" in result:
        parts.append(dump_json(result["structuredContent"]))
    return "\n\n".join(parts)
