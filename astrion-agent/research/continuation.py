"""Fork a completed checkpoint; continuation never writes to the parent scope."""

import re
from uuid import uuid4
from pathlib import Path

from memory import Memory
from context_history import copy_history
from context_adapter import adapter_from_env
from .runs import RunRepository
from .citations import observed_ranges


def load_parent(store, project, run_id):
    parent = RunRepository(store, project).get(run_id)
    info = parent.get("context_archive") or {}
    path = (store.path.resolve().parent / "context.sqlite3")
    if (not isinstance(info.get("path"), str) or Path(info["path"]).resolve() != path
            or not path.is_file() or path.is_symlink()
            or not re.fullmatch(r"[a-f0-9]{32}", info.get("scope", ""))):
        raise ValueError("research_checkpoint_unavailable: 该历史运行缺少可恢复上下文")
    memory = Memory.from_archive(path, info["scope"])
    return parent, memory


def fork_parent(store, project, run_id, *, prompt, budget_chars):
    parent, previous = load_parent(store, project, run_id)
    state = previous.to_dict()
    scope = uuid4().hex
    state["archive_scope"] = state["archive_snapshot"]["scope"] = scope
    memory = Memory.from_dict(state, system_prompt=prompt, budget_chars=budget_chars)
    memory.attach_archive(store.path.resolve().parent / "context.sqlite3")
    copy_history(previous.archive, memory.archive)
    memory.context_adapter = adapter_from_env()
    ranges = parent.get("observed_ranges")
    if ranges is None:
        ranges = observed_ranges(RunRepository(store, project).trace(run_id))
    return parent, memory, ranges
