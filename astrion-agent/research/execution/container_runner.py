"""Trusted entrypoint inside the isolated container, never executed on the host.

The subprocess below is for collecting results. Isolation is enforced by Docker.
User output, including this JSON protocol, remains untrusted data on the host.
"""

import base64
import json
import os
from pathlib import Path
import resource
import subprocess
import sys

LIMIT = 4_000_000
MAX_TOTAL = 12_000_000


def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def main():
    directory = Path("/work")
    stdout = directory / ".stdout"
    stderr = directory / ".stderr"
    timeout = int(sys.argv[1])
    status, error = "completed", None
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(
            [sys.executable, "-B", "/inputs/analysis.py"],
            cwd=directory,
            stdout=out,
            stderr=err,
            preexec_fn=limits,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            code, status, error = None, "timeout", {"code": "execution_timeout"}
    if code not in (0, None):
        status, error = "failed", {"code": "python_failed"}
    files, omitted, total = [], [], 0
    # Only root-level regular output files, no symlinks or recursive traversal.
    for path in sorted(directory.iterdir()):
        if path.name.startswith("."):
            continue
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix.lower()
            not in {".csv", ".tsv", ".json", ".txt", ".md", ".py", ".png", ".jpg", ".jpeg", ".pdf"}
        ):
            omitted.append(path.name)
            continue
        size = path.stat().st_size
        if size > LIMIT or total + size > MAX_TOTAL or len(files) >= 20:
            omitted.append(path.name)
            continue
        with path.open("rb") as handle:
            data = handle.read(LIMIT + 1)
        if len(data) > LIMIT:
            omitted.append(path.name)
            continue
        total += len(data)
        files.append({"name": path.name, "base64": base64.b64encode(data).decode("ascii")})

    def capture(path):
        # Bound reads even if user code modified the log files.
        if path.is_symlink() or not path.is_file():
            return "", True
        with path.open("rb") as handle:
            data = handle.read(12001)
        return data[:12000].decode("utf-8", errors="replace"), len(data) > 12000

    out, out_cut = capture(stdout)
    err, err_cut = capture(stderr)
    print(
        json.dumps(
            {
                "status": status,
                "exit_code": code,
                "stdout": out,
                "stderr": err,
                "stdout_truncated": out_cut,
                "stderr_truncated": err_cut,
                "error": error,
                "files": files,
                "omitted_files": omitted[:50],
            }
        )
    )


if __name__ == "__main__":
    main()
