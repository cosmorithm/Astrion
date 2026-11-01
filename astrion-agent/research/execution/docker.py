"""Docker isolation with bounded host-side capture and fail-closed availability."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
from time import monotonic
from uuid import uuid4

MAX_PROTOCOL = 18_000_000


def client_env():
    # These configure only the Docker client, never the container environment.
    allowed = {
        "PATH",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "TMPDIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def failure(status, code, message):
    return {
        "status": status,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error": {"code": code, "message": message},
        "files": [],
    }


def capture(command, timeout, max_bytes=MAX_PROTOCOL):
    """Drain both pipes without unbounded buffering; kill client on deadline."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=client_env(),
        start_new_session=True,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = monotonic() + timeout
    size = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("execution deadline exceeded")
                for key, _ in selector.select(min(remaining, 0.2)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        size += len(data)
                        if size > max_bytes:
                            raise ValueError("execution_output_limit")
                        buffers[key.data].extend(data)
            process.wait(timeout=max(0.01, deadline - monotonic()))
    finally:
        if process.poll() is None:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()
        process.stderr.close()
    return process.returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])


class DockerExecutor:
    def __init__(self, image=None):
        self.image = image or os.environ.get("ASTRION_EXECUTION_IMAGE") or "astrion-research:1"
        self.binary = shutil.which("docker")

    def availability(self):
        if not self.binary:
            return {"available": False, "reason": "Docker CLI is not installed"}
        try:
            code, out, err = capture(
                [self.binary, "image", "inspect", "--format", "{{.Id}}", self.image], 8, 16000
            )
            image_id = out.decode().strip()
            if code or not image_id.startswith("sha256:"):
                return {
                    "available": False,
                    "reason": "Docker daemon or prebuilt research image is unavailable",
                }
            return {"available": True, "image_id": image_id}
        except (OSError, TimeoutError, ValueError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "reason": type(exc).__name__}

    def command(self, directory, timeout, name, image_id):
        if "," in str(directory):
            raise ValueError("invalid_mount_path")
        return [
            self.binary,
            "run",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--cpus=1",
            "--memory=512m",
            "--memory-swap=512m",
            "--ulimit=nofile=128:128",
            "--user=65534:65534",
            "--workdir=/work",
            "--tmpfs=/work:rw,noexec,nosuid,size=33554432,mode=1777",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16777216,mode=1777",
            "--mount",
            f"type=bind,src={directory},dst=/inputs,readonly",
            "--env=HOME=/tmp",
            "--env=MPLBACKEND=Agg",
            "--env=MPLCONFIGDIR=/work/.mpl",
            image_id,
            str(timeout),
        ]

    def execute(self, directory, timeout=20):
        available = self.availability()
        if not available["available"]:
            return failure("unavailable", "sandbox_unavailable", available["reason"])
        name = "astrion-" + uuid4().hex
        started = monotonic()
        try:
            command = self.command(directory, timeout, name, available["image_id"])
            code, out, err = capture(command, timeout + 5)
            if code:
                result = failure("failed", "container_failed", err[:2000].decode(errors="replace"))
                result["exit_code"] = code
            else:
                result = json.loads(out)
                if not isinstance(result, dict):
                    raise ValueError("invalid_execution_protocol")
        except (TimeoutError, subprocess.TimeoutExpired):
            result = failure("timeout", "execution_timeout", "Container exceeded its deadline")
        except (OSError, ValueError) as exc:
            result = failure("failed", "execution_protocol_error", str(exc)[:500])
        finally:
            # Killing the client alone does not reliably stop the container.
            try:
                capture([self.binary, "rm", "--force", name], 5, 16000)
            except (OSError, TimeoutError, ValueError, subprocess.TimeoutExpired):
                pass
        return {
            **result,
            "image_id": available["image_id"],
            "duration_seconds": monotonic() - started,
        }
