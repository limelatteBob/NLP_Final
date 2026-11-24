from __future__ import annotations

import os
import json
from .acceptance import file_manifest
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .config import Config
from .storage import safe_path


class DockerSandbox:
    def __init__(self, config: Config):
        self.config = config

    def doctor(self) -> dict:
        if not shutil.which("docker"):
            return {"available": False, "reason": "Docker CLI not installed"}
        try:
            server = subprocess.run(["docker", "info", "--format", "{{.OSType}}"],
                                    capture_output=True, text=True, timeout=8)
            if server.returncode or server.stdout.strip() != "linux":
                return {"available": False, "reason": "Start Docker with Linux containers"}
            image = subprocess.run(["docker", "image", "inspect", self.config.docker_image],
                                   capture_output=True, timeout=8)
            return {"available": image.returncode == 0, "image": self.config.docker_image,
                    "reason": "ready" if image.returncode == 0 else "Image missing; pull it explicitly"}
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "reason": "Docker daemon unavailable or timed out"}

    def command(self, root: Path, relative: str, name: str, verification: str | None = None) -> list[str]:
        safe_path(root, relative)
        user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and os.getuid() else "65534:65534"
        # Container-side deadline also applies if the CLI process disappears.
        wrapper = ("import subprocess,sys; "
                   f"r=subprocess.run([sys.executable,sys.argv[1]],timeout={self.config.tool_timeout}); "
                   "sys.exit(r.returncode)")
        command = ["docker", "run", "--rm", "--pull=never", "--name", name,
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=64", "--memory=256m", "--cpus=1", "--user", user,
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m", "--workdir", "/work",
                "--mount", f"type=bind,source={root.resolve()},target=/work"]
        if (root / "inputs").is_dir():
            command.extend(["--mount", f"type=bind,source={(root / 'inputs').resolve()},target=/work/inputs,readonly"])
        if verification:
            command.extend(["--mount", f"type=bind,source={Path(__file__).with_name('verification_runner.py').resolve()},target=/controller/verification_runner.py,readonly"])
            return command + [self.config.docker_image, "python", "-I", "/controller/verification_runner.py", relative.replace("\\", "/"), verification]
        return command + [self.config.docker_image, "python", "-I", "-c", wrapper, relative.replace("\\", "/")]

    def verify(self, root: Path, relative: str) -> dict:
        token = uuid.uuid4().hex
        # Snapshot BEFORE execution; bind all observed files to exactly those bytes.
        before = {p.relative_to(root).as_posix(): p for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
        hashes = file_manifest(root, before)
        result = self._execute(root, relative, token)
        marker = 'EVOTASK_VERIFICATION_' + token + ':'
        records = [line[len(marker):] for line in result['output'].splitlines() if line.startswith(marker)]
        evidence = json.loads(records[-1]) if records else {'executed': 0, 'failures': [{'type':'MissingTestEvidence'}]}
        observed = set(evidence.get('reads', [])) | {relative}
        evidence['manifest'] = {p: hashes[p] for p in sorted(observed) if p in hashes}
        evidence['created_during_test'] = sorted(observed - hashes.keys())
        evidence['changed'] = [p for p, h in evidence['manifest'].items() if not safe_path(root,p).is_file() or file_manifest(root,[p])[p] != h]
        evidence['command'] = ['python','-I','/controller/verification_runner.py',relative]
        evidence['image'] = self.config.docker_image
        result['verification'] = evidence
        result['success'] = bool(result['success'] and evidence.get('executed',0) > 0 and not evidence.get('exit_code') and not evidence['changed'])
        if not result['success'] and not records:
            result['output'] += '\nNoTestsExecuted: controller did not receive execution evidence'
        return result

    def execute(self, root: Path, relative: str) -> dict:
        return self._execute(root, relative)

    def _execute(self, root: Path, relative: str, verification: str | None = None) -> dict:
        path = safe_path(root, relative)
        if not path.is_file() or path.suffix != ".py":
            raise ValueError("Execution requires an existing workspace .py file")
        readiness = self.doctor()
        if not readiness["available"]:
            raise RuntimeError("sandbox_unavailable: " + readiness["reason"] + "; no host execution fallback")
        # Ordinary Linux users retain their uid; Windows bind mounts are managed by Docker.
        if hasattr(os, "getuid") and os.getuid() == 0:
            raise RuntimeError("Run the controller as a non-root user so bind-mount ownership stays restricted")
        name = "evotask-" + uuid.uuid4().hex[:16]
        started = time.monotonic()
        reason = None
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(self.command(root, relative, name, verification), stdout=output, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    if time.monotonic() - started > self.config.tool_timeout + 10:
                        reason = "timeout"
                        break
                    if os.fstat(output.fileno()).st_size > self.config.max_output_bytes:
                        reason = "output_limit"
                        break
                    time.sleep(0.05)
                if reason:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
                    process.kill()
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                try:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            output.seek(0)
            text = output.read(self.config.max_output_bytes).decode("utf-8", errors="replace")
        return {"exit_code": process.returncode, "output": text, "seconds": time.monotonic() - started,
                "sandbox": "docker", "error": reason, "success": process.returncode == 0 and reason is None}
