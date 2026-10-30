#!/usr/bin/env bash
# 固定运行全部离线测试：不需要 API key，也不会访问网络。
set -euo pipefail

cd "$(dirname "$0")"

exec python3 -B -m unittest \
  test_agent_contract \
  test_bfcl \
  test_bfcl_score \
  test_benchmarks \
  test_events \
  test_session \
  test_evaluation_summary \
  test_evaluation \
  test_run_lifecycle \
  test_token_usage
