from pathlib import Path
from ..storage.artifacts import Artifacts, MAX_FILE
from ..execution.service import Computation

COMMANDS = {
    "artifact-add",
    "artifacts",
    "artifact-read",
    "artifact-export",
    "execute",
    "execution-status",
}


def register(sub):
    for name in sorted(COMMANDS):
        parser = sub.add_parser(name)
        parser.add_argument("--project", type=int, required=True)
        if name == "artifact-add":
            parser.add_argument("path", type=Path)
        elif name in {"artifact-read", "artifact-export"}:
            parser.add_argument("artifact_id")
            if name == "artifact-read":
                parser.add_argument("--offset", type=int, default=0)
            else:
                parser.add_argument("output", type=Path)
        elif name == "artifacts":
            parser.add_argument("--offset", type=int, default=0)
        elif name == "execute":
            parser.add_argument("script", type=Path)
            parser.add_argument(
                "--input",
                action="append",
                default=[],
                help="Project artifact ID; repeat for multiple inputs",
            )
            parser.add_argument("--timeout", type=int, default=20)


def dispatch(store, args):
    repository = Artifacts(store)
    if args.command == "artifact-add":
        with args.path.open("rb") as handle:
            data = handle.read(MAX_FILE + 1)
        return repository.put(
            args.project, args.path.name, data, provenance={"kind": "user_upload"}
        )
    if args.command == "artifacts":
        return repository.listing(args.project, args.offset)
    if args.command == "artifact-read":
        return repository.read(args.project, args.artifact_id, args.offset)
    if args.command == "artifact-export":
        data = repository.bytes(args.project, args.artifact_id)
        with args.output.open("xb") as handle:
            handle.write(data)
        return {"output": str(args.output), "bytes": len(data)}
    service = Computation(store, args.project)
    if args.command == "execution-status":
        return service.executor.availability()
    with args.script.open(encoding="utf-8") as handle:
        code = handle.read(100001)
    return service.execute(code, args.input, args.timeout)
