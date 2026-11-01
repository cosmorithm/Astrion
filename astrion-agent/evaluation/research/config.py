"""Budgets for the research exam; independent of the workbench defaults."""

DEFAULT_STEPS = 10
DEFAULT_WORKERS = 3
DEFAULT_WORKER_STEPS = 6
MAX_WORKERS = 3


def validate_workers(workers):
    if type(workers) is not int or not 0 <= workers <= MAX_WORKERS:
        raise ValueError('科研考试每次最多创建 3 个子 Agent')
