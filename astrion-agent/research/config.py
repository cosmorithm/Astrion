"""Research budgets shared by runner, CLI and API (decision rounds, not tokens)."""

DEFAULT_STEPS = 50
DEFAULT_WORKERS = 5
MAX_WORKERS = 5
DEEPSEEK_CONTEXT_TOKENS = 1_048_576
DEFAULT_CONTEXT_CHARS = 600_000
MAX_CONTEXT_CHARS = 786_432


def context_policy(base_url, model):
    from urllib.parse import urlsplit
    official = urlsplit(base_url or '').hostname == 'api.deepseek.com'
    known = model in {'deepseek-flash', 'deepseek-v4-pro', 'deepseek-v4-flash'}
    return {'model': model, 'window_tokens': DEEPSEEK_CONTEXT_TOKENS if official and known else None,
            'budget_chars': DEFAULT_CONTEXT_CHARS if official and known else 12_000,
            'max_budget_chars': MAX_CONTEXT_CHARS,
            'source': 'deepseek_official' if official and known else 'unverified_provider'}


def validate_budgets(max_steps, max_workers, worker_steps):
    if type(max_steps) is not int or not 0 <= max_steps <= DEFAULT_STEPS:
        raise ValueError("主 Agent 轮数须为 0–50")
    if type(worker_steps) is not int or not 1 <= worker_steps <= DEFAULT_STEPS:
        raise ValueError("子 Agent 轮数须为 1–50")
    if type(max_workers) is not int or not 0 <= max_workers <= MAX_WORKERS:
        raise ValueError("每次运行最多创建 5 个子 Agent（包括串行与并发任务）")
