"""Project artifacts and isolated computation, shared by root and research workers."""

from research.execution.service import Computation
from .contracts import S, I, spec, dump

IDS = {"type": "array", "items": S}
COMPUTATION_SPECS = [
    spec(
        "list_artifacts",
        "列出当前项目数据、代码、图表和报告。文件以不可变 ID 标识。",
        {"offset": I, "limit": I},
    ),
    spec(
        "read_project_file",
        "按 artifact_id 读取 UTF-8 数据或脚本，offset 分页；二进制用产物下载。",
        {"artifact_id": S, "offset": I, "limit": I},
        ["artifact_id"],
    ),
    spec(
        "write_project_file",
        "保存新的文本产物，不覆盖旧文件。来源 ID 必须属于本项目；保存不代表计算已执行。",
        {
            "name": S,
            "content": {"type": "string"},
            "source_artifact_ids": IDS,
            "source_document_ids": {"type": "array", "items": I},
        },
        ["name", "content"],
    ),
    spec(
        "execute_python",
        "在断网容器执行 Python，输入 artifact_id 映射为 /inputs/原文件名；输出写 /work 根目录。可用 numpy/pandas/matplotlib。返回真实状态、stdout/stderr、脚本及产物 ID。不可用时禁止声称已运行。超时 1–60 秒，输出最多20个文件，每个4MB。",
        {"code": S, "input_artifact_ids": IDS, "timeout": I},
        ["code"],
    ),
]


class ComputationTools:
    def __init__(self, store, project, executor=None):
        self.service = Computation(store, project, executor)

    def list_artifacts(self, offset=0, limit=20):
        return dump(self.service.artifacts.listing(self.service.project, offset, limit))

    def read_project_file(self, artifact_id, offset=0, limit=4000):
        return dump(self.service.artifacts.read(self.service.project, artifact_id, offset, limit))

    def write_project_file(self, name, content, source_artifact_ids=(), source_document_ids=()):
        return dump(self.service.write(name, content, source_artifact_ids, source_document_ids))

    def execute_python(self, code, input_artifact_ids=(), timeout=20):
        result = self.service.execute(code, input_artifact_ids, timeout)
        for key in ("stdout", "stderr"):
            if len(result[key]) > 4000:
                result[key] = result[key][:4000]
                result[key + "_truncated"] = True
        result["artifacts"] = [
            {k: item[k] for k in ("id", "name", "sha256", "size")} for item in result["artifacts"]
        ]
        return dump(result)

    def implementations(self):
        return {item.name: getattr(self, item.name) for item in COMPUTATION_SPECS}
