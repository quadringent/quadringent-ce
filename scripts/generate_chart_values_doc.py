"""Generate the chart values reference from its JSON schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "chart" / "values.schema.json"
OUTPUT_PATH = REPO_ROOT / "docs" / "product" / "chart-values.md"


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Suit un $ref unique (les schémas de cette chart n'en imbriquent pas)."""

    if "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/definitions/"), f"unsupported $ref: {ref}"
        return schema["definitions"][ref.removeprefix("#/definitions/")]
    return node


def _type_label(schema: dict[str, Any], node: dict[str, Any]) -> str:
    node = _resolve(schema, node)
    if "oneOf" in node:
        return " | ".join(_type_label(schema, branch) for branch in node["oneOf"])
    node_type = node.get("type", "any")
    if isinstance(node_type, list):
        return " | ".join(node_type)
    if node_type == "array":
        items = node.get("items", {})
        return f"array<{_type_label(schema, items) if items else 'any'}>"
    return str(node_type)


def _rows(schema: dict[str, Any], node: dict[str, Any], prefix: str, required: set[str]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    resolved = _resolve(schema, node)
    properties = resolved.get("properties")
    if not properties:
        return rows
    required_here = set(resolved.get("required", []))
    for key, child in properties.items():
        path = f"{prefix}.{key}" if prefix else key
        child_resolved = _resolve(schema, child)
        marker = "yes" if key in required_here or path in required else "no"
        rows.append((path, _type_label(schema, child), marker, child_resolved.get("description", "")))
        rows.extend(_rows(schema, child, path, required))
    return rows


def render(schema: dict[str, Any]) -> str:
    top_required = set(schema.get("required", []))
    rows = _rows(schema, schema, "", top_required)
    lines = [
        "# Quadringent chart values reference",
        "",
        "Generated from `chart/values.schema.json` by "
        "`scripts/generate_chart_values_doc.py` — do not edit manually.",
        "",
        "| Key | Type | Required (root) | Description |",
        "|---|---|---|---|",
    ]
    for path, type_label, marker, description in rows:
        description = description.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{path}` | `{type_label}` | {marker} | {description} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--check", action="store_true", help="fail if the existing output is stale")
    args = parser.parse_args()

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    content = render(schema)

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != content:
            raise SystemExit(f"{args.output} is stale: rerun scripts/generate_chart_values_doc.py")
        return

    args.output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
