#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在不修改安装包的前提下，覆盖 BFCL 官方 handler 的 API 模型名。

BFCL 的 ``--model`` 是 registry name，同时决定结果目录和 scorer。
严格对照实验需要保持 registry/scorer 不变，但把 DeepSeek handler 实际
发给供应商 API 的 ``model=`` 改为指定值。本启动器只允许 ``generate``，
不修改 venv 中的第三方源码。
"""

from __future__ import annotations

import argparse
import json
import sys


def override_model_config(registry_model: str, api_model: str, *, mapping=None) -> dict:
    """覆盖一个 DeepSeek registry config，并返回可记录的摘要。"""
    if not isinstance(registry_model, str) or not registry_model.strip():
        raise ValueError("registry_model 必须是非空字符串")
    if not isinstance(api_model, str) or not api_model.strip():
        raise ValueError("api_model 必须是非空字符串")
    if mapping is None:
        from bfcl_eval.constants.model_config import MODEL_CONFIG_MAPPING
        mapping = MODEL_CONFIG_MAPPING
    if registry_model not in mapping:
        raise ValueError(f"BFCL registry 中没有模型: {registry_model}")
    config = mapping[registry_model]
    handler_name = getattr(config.model_handler, "__name__", "")
    if handler_name != "DeepSeekAPIHandler":
        raise ValueError(
            "API 模型覆盖目前只允许 DeepSeekAPIHandler，"
            f"当前是 {handler_name or '<unknown>'}"
        )
    original = config.model_name
    config.model_name = api_model.strip()
    return {
        "registry_model": registry_model.strip(),
        "original_api_model": original,
        "effective_api_model": config.model_name,
        "handler": handler_name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="使用指定 API model 运行 BFCL 官方 DeepSeek handler"
    )
    parser.add_argument("--registry-model", required=True)
    parser.add_argument("--api-model", required=True)
    parser.add_argument(
        "--check", action="store_true",
        help="只验证覆盖并输出摘要，不执行 BFCL、不调用模型",
    )
    args, official_args = parser.parse_known_args(argv)
    summary = override_model_config(args.registry_model, args.api_model)
    print(
        "[Astrion BFCL API model override] "
        + json.dumps(summary, ensure_ascii=False, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )
    if args.check:
        if official_args:
            parser.error("--check 不接受 BFCL 命令参数")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not official_args or official_args[0] != "generate":
        parser.error("覆盖启动器后只能跟 BFCL generate 命令")

    from bfcl_eval.__main__ import cli
    sys.argv = ["bfcl", *official_args]
    result = cli()
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
