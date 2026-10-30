"""Astrion Agent 赛后评测：运行记录、可注册的裁判和报告。"""

from .judges import JudgeRegistry, default_registry
from .runner import evaluate_case
from .suite import summarize_suite

__all__ = ["JudgeRegistry", "default_registry", "evaluate_case", "summarize_suite"]
