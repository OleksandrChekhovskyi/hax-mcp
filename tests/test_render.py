from __future__ import annotations

import base64
import pathlib
import unittest

from support import TOOLS, Json
from hax_mcp import render


class SchemaTests(unittest.TestCase):
    def test_schema_types_are_compact(self) -> None:
        defs: Json = {"Label": {"type": "object", "properties": {"id": {"type": "string"}}}}
        cases: list[tuple[Json, str]] = [
            ({"enum": ["a", "b"]}, '"a" | "b"'),
            ({"type": "array", "items": {"enum": [1, 2]}}, "(1 | 2)[]"),
            ({"anyOf": [{"type": "string"}, {"type": "null"}]}, "string | null"),
            ({"$ref": "#/$defs/Label"}, "{id?: string}"),
            ({"$ref": "https://example.com/schema"}, "schema"),
            (
                {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]},
                "{n: number}",
            ),
            ({"type": ["string", "integer"]}, "string | integer"),
            ({}, "any"),
        ]
        for schema, expected in cases:
            self.assertEqual(render.schema_type(schema, defs, 1), expected)

    def test_deep_objects_collapse(self) -> None:
        deep = {"type": "object", "properties": {"a": {"type": "object", "properties": {}}}}
        nested = {"type": "object", "properties": {"x": deep}}
        self.assertEqual(render.schema_type(nested, {}, 1), "{x?: {a?: object}}")


class ToolTests(unittest.TestCase):
    def test_tool_list_puts_required_parameters_first(self) -> None:
        self.assertEqual(
            render.format_tools(TOOLS[:2]),
            "echo(text, count?) — Echo the arguments back.\nfail()",
        )

    def test_signature_truncates_long_parameter_lists(self) -> None:
        properties = {f"p{i}": {"type": "string"} for i in range(10)}
        tool = {"name": "t", "inputSchema": {"type": "object", "properties": properties}}
        self.assertTrue(render.signature(tool, 3).endswith("(p0?, p1?, p2?, …)"))

    def test_summary_takes_first_sentence_within_limit(self) -> None:
        self.assertEqual(render.summary("First.\nSecond."), "First.")
        self.assertEqual(len(render.summary("x" * 500)), render.SUMMARY_CHARS)

    def test_tool_details_include_defaults_and_hints(self) -> None:
        self.assertEqual(
            render.format_tool(TOOLS[0]).splitlines(),
            [
                "echo(text, count?)",
                "",
                "Echo the arguments back. Useful for tests.",
                "",
                "Parameters:",
                "  count?: integer = 1",
                "  text: string — Text to echo",
                "",
                "Read-only.",
            ],
        )


class ResultTests(unittest.TestCase):
    def test_json_text_is_pretty_printed(self) -> None:
        blocks: Json = [{"type": "text", "text": '{"a": [1]}'}, {"type": "text", "text": "{x"}]
        self.assertEqual(
            render.format_result({"content": blocks}), '{\n  "a": [\n    1\n  ]\n}\n\n{x'
        )

    def test_structured_content_is_used_without_content(self) -> None:
        self.assertEqual(render.format_result({"structuredContent": {"a": 1}}), '{\n  "a": 1\n}')

    def test_binary_content_is_saved_to_a_file(self) -> None:
        data = base64.b64encode(b"\x89PNG").decode()
        result: Json = {"content": [{"type": "image", "data": data, "mimeType": "image/png"}]}
        text = render.format_result(result)
        path = pathlib.Path(text.removeprefix("[image saved to ").removesuffix("]"))
        self.addCleanup(path.unlink)
        self.assertEqual(path.suffix, ".png")
        self.assertEqual(path.read_bytes(), b"\x89PNG")


if __name__ == "__main__":
    unittest.main()
