"""Per-turn native tool definitions (guardrails G1/G2).

A tool's argument model is built with Pydantic every turn, so a single source yields both the
JSON schema the model sees and the validator its output must pass: ID-like fields are enums of
values that really exist right now, extra fields are forbidden, and an empty enum means the tool
is not offered at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

SemanticCheck = Callable[[BaseModel], str | None]  # returns a problem description, or None


def enum_of(values: Sequence[str]) -> Any:
    """`Literal[...]` over the given real values. Callers must not pass an empty sequence."""
    if not values:
        raise ValueError("an enum needs at least one real value; omit the tool instead")
    return Literal[tuple(dict.fromkeys(values))]  # de-duplicated, order kept


def args_model(name: str, **fields: tuple[Any, Any]) -> type[BaseModel]:
    model: type[BaseModel] = create_model(  # type: ignore[call-overload]
        name, __config__=ConfigDict(extra="forbid", frozen=True), **fields
    )
    return model


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args: type[BaseModel]
    check: SemanticCheck | None = field(default=None, compare=False)

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _strip_titles(_inline_refs(self.args.model_json_schema())),
            },
        }


def _strip_titles(schema: Any) -> Any:
    """Drop titles and present single-value Literals (`const`) as one-element enums, like all ID fields."""
    if isinstance(schema, dict):
        node = {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
        if "const" in node and "enum" not in node:
            node["enum"] = [node.pop("const")]
        return node
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace `$ref`s to local `$defs` with the definitions themselves (small models read flat schemas better)."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return walk(defs[ref.removeprefix("#/$defs/")])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result
