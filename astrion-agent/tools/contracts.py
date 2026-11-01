"""Declaration helpers using the sole ToolSpec contract in tooling.py."""

import json
from tooling import ToolSpec

S = {"type": "string", "minLength": 1}
I = {"type": "integer"}


def spec(name, description, properties, required=()):
    return ToolSpec(
        name,
        description,
        {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        output_description="JSON；来源、完整性与分页字段明确；错误由 ToolResult.error 返回",
    )


def dump(data):
    text = json.dumps(data, ensure_ascii=False, allow_nan=False)
    if len(text) > 24000:
        raise ValueError("tool_output_limit: reduce page size or read a narrower selection")
    return text
