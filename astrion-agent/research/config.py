"""Research budgets shared by runner, CLI and API (decision rounds, not tokens)."""

DEFAULT_STEPS = 50
DEFAULT_WORKERS = 5
MAX_WORKERS = 5


def validate_budgets(max_steps, max_workers, worker_steps):
    if type(max_steps) is not int or not 0 <= max_steps <= DEFAULT_STEPS:
        raise ValueError("主 Agent 轮数须为 0–50")
    if type(worker_steps) is not int or not 1 <= worker_steps <= DEFAULT_STEPS:
        raise ValueError("子 Agent 轮数须为 1–50")
    if type(max_workers) is not int or not 0 <= max_workers <= MAX_WORKERS:
        raise ValueError("每次运行最多创建 5 个子 Agent（包括串行与并发任务）")
