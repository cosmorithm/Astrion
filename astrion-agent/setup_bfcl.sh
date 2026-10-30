#!/usr/bin/env bash
# 在独立 venv 安装固定版本的 BFCL 官方 harness。
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${BFCL_PYTHON:-python3.12}"
VENV_DIR="${BFCL_VENV_DIR:-.venv-bfcl}"

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r requirements-bfcl.txt

echo "BFCL 已安装到 ${VENV_DIR}"
echo "下一步：${VENV_DIR}/bin/python -m benchmarks bfcl doctor"
