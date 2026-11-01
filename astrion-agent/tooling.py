"""工具声明、调用验证与执行结果；不依赖具体模型或工具来源。"""
from __future__ import annotations

import copy
import inspect
import json
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    output_description: str = ""
    terminal: bool = False

    def as_api_tool(self) -> dict:
        description = self.description
        if self.output_description:
            description += "；返回：" + self.output_description
        return {"type": "function", "function": {
            "name": self.name, "description": description,
            "parameters": copy.deepcopy(self.input_schema),
        }}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    raw_arguments: object


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    outcome: str
    content: str
    arguments: dict | None = None
    error: dict | None = None
    duration_seconds: float | None = None


class ToolValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validate(value, schema: dict, path: str = "参数") -> None:
    kind = schema.get("type")
    valid = {
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: type(x) is int,
        "number": lambda x: type(x) in (int, float) and math.isfinite(x),
        "boolean": lambda x: type(x) is bool,
        "null": lambda x: x is None,
    }
    if kind not in valid:
        raise ToolValidationError("unsupported_schema", f"不支持的 schema 类型: {kind!r}")
    if not valid[kind](value):
        raise ToolValidationError("invalid_argument_type", f"{path} 必须是 {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError("invalid_argument_value", f"{path} 不在允许的枚举值内")
    if kind == "string" and len(value) < schema.get("minLength", 0):
        raise ToolValidationError("invalid_argument_value", f"{path} 长度不足")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ToolValidationError("missing_required_argument", f"缺少必填参数: {path}.{key}")
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties", True) is False:
                    raise ToolValidationError("unexpected_argument", f"未知参数: {path}.{key}")
            else:
                _validate(item, properties[key], f"{path}.{key}")
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ToolValidationError("invalid_argument_value", f"{path} 元素不足")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{index}]")


def _validate_schema(schema: dict) -> None:
    if not isinstance(schema, dict):
        raise ValueError("工具 schema 必须是对象")
    allowed = {"type", "description", "properties", "required", "additionalProperties",
               "items", "minItems", "minLength", "enum", "default"}
    unknown = set(schema) - allowed
    if unknown:
        raise ValueError(f"工具 schema 包含未支持的关键字: {sorted(unknown)}")
    kind = schema.get("type")
    if kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise ValueError(f"不支持的 schema 类型: {kind!r}")
    for key in ("minItems", "minLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"{key} 必须是非负整数")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("enum 必须是非空列表")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list) or any(
            not isinstance(key, str) or key not in properties for key in required
        ):
            raise ValueError("对象 schema 的 properties 或 required 无效")
        if type(schema.get("additionalProperties", True)) is not bool:
            raise ValueError("additionalProperties 只支持布尔值")
        for child in properties.values():
            _validate_schema(child)
    if kind == "array" and "items" in schema:
        _validate_schema(schema["items"])
    if "default" in schema:
        _validate(schema["default"], schema, "default")
    if "enum" in schema:
        base = {key: value for key, value in schema.items() if key != "enum"}
        for item in schema["enum"]:
            _validate(item, base, "enum")


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec], implementations: dict[str, Callable]):
        self.specs = {spec.name: spec for spec in specs}
        if len(self.specs) != len(specs):
            raise ValueError("工具名称重复")
        self.implementations = implementations
        missing = set(self.specs) - set(implementations)
        if missing:
            raise ValueError(f"工具缺少实现: {sorted(missing)}")
        for spec in specs:
            if not isinstance(spec.name, str) or not spec.name.strip():
                raise ValueError("工具名必须非空")
            _validate_schema(spec.input_schema)
            if spec.input_schema.get("type") != "object":
                raise ValueError("工具参数 schema 必须是 object")
            signature = inspect.signature(implementations[spec.name])
            properties = spec.input_schema.get("properties", {})
            accepts_extra = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
            for name in properties:
                param = signature.parameters.get(name)
                if not accepts_extra and (param is None or param.kind == inspect.Parameter.POSITIONAL_ONLY):
                    raise ValueError(f"工具 {spec.name} 的参数 {name} 无法绑定到实现")
            required = set(spec.input_schema.get("required", []))
            for param in signature.parameters.values():
                if (param.default is inspect.Parameter.empty
                    and param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                    and param.name not in required):
                    raise ValueError(f"工具 {spec.name} 的实现要求参数 {param.name}，但 schema 未声明必填")

    def prepare(self, call: ToolCall) -> tuple[ToolSpec, dict, Callable]:
        if call.name not in self.specs or call.name not in self.implementations:
            raise ToolValidationError("unknown_tool", f"未知工具: {call.name}")
        raw = call.raw_arguments
        if raw is None or raw == "":
            raw = "{}"
        if not isinstance(raw, str):
            raise ToolValidationError("invalid_json", "工具参数必须是 JSON 字符串")
        try:
            args = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, TypeError) as exc:
            raise ToolValidationError("invalid_json", f"工具参数不是有效 JSON: {exc}") from exc
        if not isinstance(args, dict):
            raise ToolValidationError("invalid_argument_type", "工具参数必须是 JSON 对象")
        spec = self.specs[call.name]
        _validate(args, spec.input_schema)
        implementation = self.implementations[call.name]
        try:
            inspect.signature(implementation).bind(**args)
        except TypeError as exc:
            raise ToolValidationError("binding_error", f"工具实现参数不匹配: {exc}") from exc
        if call.name in {"finish", "ask_user"}:
            key = "answer" if call.name == "finish" else "question"
            if not args[key].strip():
                raise ToolValidationError("invalid_argument_value", f"{key} 必须是非空字符串")
        return spec, args, implementation

    def execute(self, call: ToolCall, on_start: Callable[[dict], None] | None = None) -> ToolResult:
        try:
            _spec, args, implementation = self.prepare(call)
        except ToolValidationError as exc:
            return ToolResult(call.id, call.name, "rejected", f"ERROR: {exc}",
                              error={"type": type(exc).__name__, "code": exc.code, "message": str(exc)})
        if on_start is not None:
            on_start(args)
        started = perf_counter()
        try:
            content = str(implementation(**args))
        except Exception as exc:
            return ToolResult(call.id, call.name, "error", f"ERROR: {type(exc).__name__}: {exc}",
                              args, {"type": type(exc).__name__, "code": getattr(exc, "code", "execution_failed"), "message": str(exc)},
                              perf_counter() - started)
        return ToolResult(call.id, call.name, "success", content, args, duration_seconds=perf_counter() - started)
