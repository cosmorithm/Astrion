"""Astrion 可复用的增强决策：规划、候选调用、验证、复核与有界修正。

它只提出调用，不执行工具；工具执行和授权由调用方运行时负责。
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Callable

from tooling import ToolValidationError, _validate
from decision_prompts import (
    PLAN_PROMPT, DECISION_PROMPT, REVIEW_PROMPT, REPAIR_PROMPT,
    OUTPUT_SCHEMA_VERSION, prompt_policy,
)


def _parse_plan(message: dict) -> dict:
    plan = _json_object(message)
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != OUTPUT_SCHEMA_VERSION:
        raise ValueError("plan schema_version 不受支持")
    if not isinstance(plan.get("subtasks"), list) or any(not isinstance(x, str) for x in plan["subtasks"]):
        raise ValueError("subtasks 必须是字符串列表")
    evidence = plan.get("parameter_evidence")
    if not isinstance(evidence, list):
        raise ValueError("parameter_evidence 必须是列表")
    for item in evidence:
        if not isinstance(item, dict) or not {"tool", "parameter", "value", "source", "quote", "explanation"}.issubset(item):
            raise ValueError("parameter_evidence 项缺少必需字段")
        if any(item[key] is not None and not isinstance(item[key], str) for key in ("tool", "parameter", "quote")):
            raise ValueError("tool、parameter 和 quote 必须是字符串或 null")
        if item["source"] not in {"user_explicit", "inferred", "tool_default", "unknown"}:
            raise ValueError("参数来源无效")
        if not isinstance(item["explanation"], str):
            raise ValueError("explanation 必须是字符串")
    missing = plan.get("missing_information")
    if not isinstance(missing, list):
        raise ValueError("missing_information 必须是列表")
    for item in missing:
        if not isinstance(item, dict) or not {"tool", "parameter", "subtask_index", "required", "default_available", "blocks_action", "reason"}.issubset(item):
            raise ValueError("missing_information 项缺少必需字段")
        if not isinstance(item["tool"], str) or not isinstance(item["parameter"], str) or not isinstance(item["reason"], str):
            raise ValueError("缺失参数及原因必须是字符串")
        if any(item[key] is not None and type(item[key]) is not bool
               for key in ("required", "default_available", "blocks_action")):
            raise ValueError("required、default_available 和 blocks_action 必须是布尔或 null")
        _check_index(item["subtask_index"], len(plan["subtasks"]), "subtask_index")
    gaps = plan.get("capability_gaps")
    if not isinstance(gaps, list):
        raise ValueError("capability_gaps 必须是列表")
    for item in gaps:
        if not isinstance(item, dict) or not {"subtask_index", "tool", "missing_capability", "reason"}.issubset(item):
            raise ValueError("capability_gaps 项缺少必需字段")
        _check_index(item["subtask_index"], len(plan["subtasks"]), "subtask_index")
        if item["tool"] is not None and not isinstance(item["tool"], str):
            raise ValueError("capability_gaps 的 tool 必须是字符串或 null")
        if not isinstance(item["missing_capability"], str) or not isinstance(item["reason"], str):
            raise ValueError("能力缺口及原因必须是字符串")
    if not isinstance(plan.get("tool_assessment"), str):
        raise ValueError("tool_assessment 必须是字符串")
    return plan


def _check_index(value, length: int, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0 or value >= length):
        raise ValueError(f"{name} 必须是有效的零起始索引或 null")


def audit_plan_evidence(plan: dict, messages: list[dict], tools: list[dict]) -> list[dict]:
    """核对来源引用与默认值是否存在；不声称理解或证明引用的语义。"""
    user_texts = [(index, m["content"]) for index, m in enumerate(messages)
                  if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)]
    tool_map = {tool.get("function", {}).get("name"): tool.get("function", {})
                for tool in tools if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
    audits = []
    for index, item in enumerate(plan.get("parameter_evidence", [])):
        source, tool_name, parameter = item["source"], item["tool"], item["parameter"]
        function = tool_map.get(tool_name)
        schema = function.get("parameters", {}) if isinstance(function, dict) else {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        param_schema = properties.get(parameter) if isinstance(properties, dict) else None
        status = "not_verifiable"
        match_method = None
        matched_message_indices = []
        if source == "user_explicit":
            quote = item.get("quote") if "quote" in item else item.get("evidence")
            status, match_method, matched_message_indices = _match_user_quote(quote, user_texts)
        elif source == "tool_default":
            if not isinstance(param_schema, dict) or "default" not in param_schema:
                status = "default_not_declared"
            elif type(param_schema["default"]) is type(item["value"]) and param_schema["default"] == item["value"]:
                status = "default_matches"
            else:
                status = "default_mismatch"
        audits.append({"index": index, "tool": tool_name, "parameter": parameter,
                       "source": source, "status": status,
                       "match_method": match_method, "matched_message_indices": matched_message_indices,
                       "tool_parameter_known": isinstance(param_schema, dict)})
    return audits


def _match_user_quote(quote, user_texts: list[tuple[int, str]]) -> tuple[str, str | None, list[int]]:
    """只匹配原文或剥去一对外围引号的原文，不从解释中提取片段。"""
    if not isinstance(quote, str) or not quote.strip():
        return "quote_not_supplied", None, []
    exact = [index for index, text in user_texts if quote in text]
    if exact:
        return "quote_found", "exact", exact
    trimmed = quote.strip()
    if len(trimmed) >= 2 and (trimmed[0], trimmed[-1]) in {
        ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
    }:
        unwrapped = trimmed[1:-1]
        matched = [index for index, text in user_texts if unwrapped and unwrapped in text]
        if matched:
            return "quote_found", "outer_quotes_removed", matched
    return "quote_not_found", None, []


def audit_missing_information(plan: dict, tools: list[dict]) -> list[dict]:
    """对模型声明的缺失参数核对 schema 的必填和显式默认值，不判定语义。"""
    tool_map = {tool.get("function", {}).get("name"): tool.get("function", {})
                for tool in tools if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
    audits = []
    for index, item in enumerate(plan.get("missing_information", [])):
        function = tool_map.get(item["tool"])
        schema = function.get("parameters", {}) if isinstance(function, dict) else {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        param_schema = properties.get(item["parameter"]) if isinstance(properties, dict) else None
        required = schema.get("required", []) if isinstance(schema, dict) else []
        known = isinstance(param_schema, dict) and isinstance(required, list)
        actual_required = item["parameter"] in required if known else None
        has_default = "default" in param_schema if known else None
        audits.append({"index": index, "tool": item["tool"], "parameter": item["parameter"],
                       "schema_parameter_known": known, "schema_required": actual_required,
                       "schema_default_declared": has_default,
                       "required_claim_matches": (item["required"] == actual_required
                           if known and item["required"] is not None else None),
                       "default_claim_matches": (item["default_available"] == has_default
                           if known and item["default_available"] is not None else None)})
    return audits


def audit_capability_gaps(plan: dict, tools: list[dict]) -> list[dict]:
    names = {tool["function"].get("name") for tool in tools
             if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
    return [{"index": index, "tool": item["tool"],
             "tool_known": item["tool"] in names if item["tool"] is not None else None}
            for index, item in enumerate(plan.get("capability_gaps", []))]


def _parse_review(message: dict) -> dict:
    review = _json_object(message)
    if type(review.get("schema_version")) is not int or review["schema_version"] != OUTPUT_SCHEMA_VERSION:
        raise ValueError("review schema_version 不受支持")
    if type(review.get("needs_revision")) is not bool or not isinstance(review.get("issues"), list):
        raise ValueError("review 需要布尔 needs_revision 和 issues 列表")
    allowed = {"structure", "missing_goal", "tool_mismatch", "parameter_semantics",
               "answer_quality", "safety", "uncertainty"}
    for issue in review["issues"]:
        fields = {"type", "description", "subtask_index", "user_quote", "tool", "parameter", "call_index"}
        if not isinstance(issue, dict) or not fields.issubset(issue):
            raise ValueError("review issue 缺少必需字段")
        if not isinstance(issue["type"], str) or issue["type"] not in allowed:
            raise ValueError("review issue type 无效")
        if not isinstance(issue["description"], str):
            raise ValueError("review issue description 必须是字符串")
        if any(issue[key] is not None and not isinstance(issue[key], str)
               for key in ("user_quote", "tool", "parameter")):
            raise ValueError("user_quote、tool 和 parameter 必须是字符串或 null")
        for key in ("subtask_index", "call_index"):
            if issue[key] is not None and (type(issue[key]) is not int or issue[key] < 0):
                raise ValueError(f"{key} 必须是非负整数或 null")
    return review


def audit_review_evidence(review: dict, plan: dict, candidate: dict,
                          messages: list[dict], tools: list[dict]) -> list[dict]:
    """核对复核引用的文本和索引；匹配不等于复核判断为真。"""
    texts = [(index, m["content"]) for index, m in enumerate(messages)
             if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)]
    tool_map = {tool["function"].get("name"): tool["function"] for tool in tools
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
    calls = candidate.get("tool_calls") or []
    if not isinstance(calls, list):
        calls = []
    output = []
    for index, issue in enumerate(review.get("issues", [])):
        status, method, matches = _match_user_quote(issue["user_quote"], texts)
        tool_name, parameter, call_index, subtask_index = (
            issue["tool"], issue["parameter"], issue["call_index"], issue["subtask_index"]
        )
        tool = tool_map.get(tool_name)
        schema = tool.get("parameters", {}) if isinstance(tool, dict) else {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        call_known = call_index is not None and call_index < len(calls)
        function = calls[call_index].get("function", {}) if call_known and isinstance(calls[call_index], dict) else {}
        output.append({"index": index, "quote_status": status, "quote_match_method": method,
                       "matched_message_indices": matches,
                       "tool_known": tool_name in tool_map if tool_name is not None else None,
                       "parameter_known": parameter in properties if parameter is not None and isinstance(properties, dict) and tool is not None else None,
                       "subtask_index_valid": subtask_index < len(plan.get("subtasks", [])) if subtask_index is not None else None,
                       "call_index_valid": call_known if call_index is not None else None,
                       "call_tool_matches": function.get("name") == tool_name if call_known and isinstance(function, dict) and tool_name is not None else None})
    return output


def _json_object(message: dict) -> dict:
    text = message.get("content")
    if not isinstance(text, str):
        raise ValueError("模型未返回 JSON 文本")
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        if "\n" not in text:
            raise ValueError("JSON 代码块缺少内容")
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    return value


def _project_schema(schema: dict, unsupported: list[str], path: str = "$") -> dict:
    """只投影已支持的约束；其余约束显式记录为未验证。"""
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema 不是对象")
    supported = {"type", "properties", "required", "additionalProperties", "items",
                 "enum", "minLength", "minItems", "description", "default"}
    ignored_metadata = {"description", "title", "default", "examples", "$schema"}
    for key in schema:
        if key not in supported and key not in ignored_metadata:
            unsupported.append(f"{path}.{key}")
    projected = {key: copy.deepcopy(value) for key, value in schema.items()
                 if key in supported and key not in {"properties", "items", "default"}}
    if projected.get("type") not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise ValueError(f"{path}: 不支持的类型 {projected.get('type')!r}")
    if "additionalProperties" in projected and type(projected["additionalProperties"]) is not bool:
        unsupported.append(f"{path}.additionalProperties")
        del projected["additionalProperties"]
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise ValueError(f"{path}.properties 不是对象")
        projected["properties"] = {
            key: _project_schema(value, unsupported, f"{path}.{key}")
            for key, value in schema["properties"].items()
        }
    if "items" in schema:
        projected["items"] = _project_schema(schema["items"], unsupported, f"{path}[]")
    return projected


def validate_candidate(message: dict, tools: list[dict]) -> dict:
    """验证模型候选调用；对不支持的 schema 单独标记，不伪称通过全部约束。"""
    issues, unchecked = [], []
    tool_map = {tool.get("function", {}).get("name"): tool.get("function", {})
                for tool in tools if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        return {"issues": ["tool_calls must be a list"], "unchecked": []}
    for index, call in enumerate(calls):
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            issues.append(f"call {index}: missing function")
            continue
        name, raw = function.get("name"), function.get("arguments")
        if name not in tool_map:
            issues.append(f"call {index}: unknown tool {name!r}")
            continue
        try:
            if not isinstance(raw, str):
                raise ValueError("arguments must be a JSON string")
            args = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (ValueError, TypeError) as exc:
            issues.append(f"call {index} {name}: {exc}")
            continue
        try:
            projected = _project_schema(tool_map[name].get("parameters", {}), unchecked, name)
            _validate(args, projected)
        except ToolValidationError as exc:
            issues.append(f"call {index} {name}: {exc.code}: {exc}")
        except (TypeError, ValueError, KeyError) as exc:
            unchecked.append(f"{name}: schema not fully supported: {exc}")
    return {"issues": issues, "unchecked": sorted(set(unchecked))}


def _with_decision_context(messages: list[dict], context: str) -> list[dict]:
    """把内部辅助信息放在用户消息中，不破坏工具调用配对或创建相邻用户消息。"""
    result = copy.deepcopy(messages)
    if result and result[-1].get("role") == "user" and isinstance(result[-1].get("content"), str):
        result[-1]["content"] += "\n\n[Decision context]\n" + context
    else:
        result.append({"role": "user", "content": "[Decision context]\n" + context})
    return result


def _valid_call_signatures(message: dict, tools: list[dict]) -> Counter:
    """记录原本通过结构验证的调用，用于防止修复时丢掉或改动它们。"""
    signatures = Counter()
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        return signatures
    for call in calls:
        if validate_candidate({"tool_calls": [call]}, tools)["issues"]:
            continue
        function = call["function"]
        arguments = json.loads(function["arguments"])
        signature = (function["name"], json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        signatures[signature] += 1
    return signatures


def enhanced_decision(messages: list[dict], tools: list[dict],
                      call_model: Callable[[list[dict], list[dict], str], dict]) -> dict:
    """针对同一用户轮次执行最多四次模型调用；不执行任何业务工具。"""
    source = {"conversation": copy.deepcopy(messages), "tools": copy.deepcopy(tools)}
    plan_response = call_model([
        {"role": "system", "content": PLAN_PROMPT},
        {"role": "user", "content": json.dumps(source, ensure_ascii=False)},
    ], [], "decision.plan")
    try:
        plan = _parse_plan(plan_response)
        plan_error = None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        plan = {"schema_version": OUTPUT_SCHEMA_VERSION, "subtasks": [], "parameter_evidence": [],
                "missing_information": [], "capability_gaps": [], "tool_assessment": ""}
        plan_error = str(exc)
    plan_audit = audit_plan_evidence(plan, messages, tools)
    missing_audit = audit_missing_information(plan, tools)
    capability_audit = audit_capability_gaps(plan, tools)
    candidate_messages = _with_decision_context(
        messages, DECISION_PROMPT + "\nTentative plan and limited evidence audit: " + json.dumps(
            {"plan": plan, "evidence_audit": plan_audit, "missing_information_audit": missing_audit,
             "capability_gaps_audit": capability_audit}, ensure_ascii=False)
    )
    candidate = call_model(candidate_messages, copy.deepcopy(tools), "decision.candidate")
    validation = validate_candidate(candidate, tools)
    candidate_for_review = {key: copy.deepcopy(candidate[key]) for key in ("role", "content", "tool_calls") if key in candidate}
    review_data = {**source, "plan": plan, "plan_evidence_audit": plan_audit,
                   "missing_information_audit": missing_audit, "capability_gaps_audit": capability_audit,
                   "candidate": candidate_for_review, "validation": validation}
    review_response = call_model([
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user", "content": json.dumps(review_data, ensure_ascii=False)},
    ], [], "decision.review")
    try:
        review = _parse_review(review_response)
        review_error = None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        review = {"schema_version": OUTPUT_SCHEMA_VERSION, "needs_revision": False, "issues": []}
        review_error = str(exc)
    review_audit = audit_review_evidence(review, plan, candidate, messages, tools)
    # 语义复核是模型判断，只记录疑问。只有可确定的结构错误才触发修正。
    repair_needed = bool(validation["issues"])
    final = candidate
    repair_accepted = False
    repair_rejection = None
    if repair_needed:
        repair_messages = _with_decision_context(messages, REPAIR_PROMPT + "\n" + json.dumps({
            "candidate": candidate_for_review, "validation_issues": validation["issues"],
        }, ensure_ascii=False))
        repaired = call_model(repair_messages, copy.deepcopy(tools), "decision.repair")
        repaired_validation = validate_candidate(repaired, tools)
        if repaired_validation["issues"]:
            repair_rejection = "修正后的调用仍有结构错误"
        elif _valid_call_signatures(candidate, tools) - _valid_call_signatures(repaired, tools):
            repair_rejection = "修正改变或丢失了原本结构合法的调用"
        elif candidate.get("tool_calls") and not repaired.get("tool_calls"):
            repair_rejection = "修正删除了全部原始工具调用"
        else:
            final = repaired
            repair_accepted = True
    final_validation = validate_candidate(final, tools)
    return {"message": final, "initial_message": candidate, "prompt_policy": prompt_policy(), "plan": plan,
            "plan_error": plan_error, "plan_evidence_audit": plan_audit,
            "missing_information_audit": missing_audit, "capability_gaps_audit": capability_audit,
            "review": review, "review_error": review_error, "review_evidence_audit": review_audit,
            "initial_validation": validation, "final_validation": final_validation,
            "repair_attempted": repair_needed, "repair_accepted": repair_accepted,
            "repair_rejection": repair_rejection, "semantic_review_advisory_only": True}
