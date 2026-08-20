from __future__ import annotations

import math
import os
import platform
import re
import resource
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from pydantic import BaseModel, Field

from ..config import Settings
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]


class SandboxUnavailableError(RuntimeError):
    pass


class SandboxCommandArguments(BaseModel):
    command: list[str] = Field(min_length=1, max_length=64)
    cwd: str = "."


class SandboxResult(BaseModel):
    mode: SandboxMode
    command: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False
    written_files: list[str] = Field(default_factory=list)


class SandboxRunner:
    """Fail-closed local process runner using macOS Seatbelt when restricted."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        timeout_seconds: float = 30,
        max_output_bytes: int = 65536,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.seatbelt = Path("/usr/bin/sandbox-exec")
        if platform.system() == "Darwin" and self.seatbelt.is_file():
            probe = subprocess.run(
                [
                    str(self.seatbelt),
                    "-p",
                    "(version 1)(allow default)(deny file-write*)",
                    "--",
                    "/usr/bin/true",
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.restricted_available = probe.returncode == 0
        else:
            self.restricted_available = False

    _SKIP_DIRS = frozenset(
        {
            ".venv",
            "node_modules",
            ".git",
            "data",
            "__pycache__",
            ".pytest_cache",
            "dist",
            "build",
            ".ruff_cache",
        }
    )

    def _cwd(self, relative: str) -> Path:
        candidate = (self.workspace_root / relative).resolve()
        if candidate != self.workspace_root and self.workspace_root not in candidate.parents:
            raise PermissionError("sandbox cwd escapes workspace")
        if not candidate.is_dir():
            raise ValueError("sandbox cwd does not exist")
        return candidate

    def _inside_workspace(self, path: Path) -> bool:
        resolved = path.resolve()
        root = self.workspace_root
        return resolved == root or root in resolved.parents

    def resolve_workspace_file(self, raw_path: str) -> Path:
        text = unquote(str(raw_path or "").strip())
        lowered = text.lower()
        for prefix in ("sandbox:", "file://", "file:"):
            if lowered.startswith(prefix):
                text = text[len(prefix) :]
                break
        text = text.strip()
        if not text or text.startswith("~"):
            raise PermissionError("sandbox file path is not allowed")
        candidate = Path(text)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace_root / candidate).resolve()
        )
        options = [resolved]
        name = Path(text).name
        if name:
            options.append((self.workspace_root / name).resolve())
            if name.lower().endswith(".txt"):
                options.insert(0, (self.workspace_root / f"{Path(name).stem}.csv").resolve())
        seen: set[Path] = set()
        found_inside = False
        for item in options:
            if item in seen:
                continue
            seen.add(item)
            if not self._inside_workspace(item):
                continue
            found_inside = True
            if item.is_file():
                return item
        if not found_inside:
            raise PermissionError("sandbox file escapes workspace")
        raise FileNotFoundError("sandbox file not found")

    def _workspace_snapshot(self, folder: Path | None = None) -> dict[str, tuple[int, int]]:
        files: dict[str, tuple[int, int]] = {}
        root = self.workspace_root
        start = folder or root
        for path in start.rglob("*"):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                rel = resolved.relative_to(root)
            except ValueError:
                continue
            if any(part in self._SKIP_DIRS or part.startswith(".") for part in rel.parts):
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            files[rel.as_posix()] = (stat.st_mtime_ns, stat.st_size)
        return files

    def _changed_workspace_files(
        self,
        before: dict[str, tuple[int, int]],
        folder: Path | None = None,
    ) -> list[str]:
        after = self._workspace_snapshot(folder)
        changed = [name for name, signature in after.items() if before.get(name) != signature]
        return changed[:20]

    def _materialize_echo_argv(self, command: list[str], cwd: Path) -> Path | None:
        binary = Path(command[0]).name
        if binary not in {"echo", "printf"}:
            return None
        args = command[1:]
        interpret = False
        if binary == "echo":
            while args and args[0] in {"-e", "-n", "-E"}:
                if args[0] == "-e":
                    interpret = True
                args = args[1:]
        if len(args) < 2:
            return None
        filename = Path(args[-1]).name
        if not re.search(r"\.[A-Za-z0-9]{1,12}$", filename):
            return None
        payload = " ".join(args[:-1])
        if interpret:
            payload = payload.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
        payload = self.unwrap_echo_payload(payload)
        destination = (cwd / filename).resolve()
        if not self._inside_workspace(destination):
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = destination
        tabular = "\t" in payload and "\n" in payload
        if filename.lower().endswith(".csv") or tabular:
            csv_dest = (
                destination
                if filename.lower().endswith(".csv")
                else (cwd / f"{Path(filename).stem}.csv").resolve()
            )
            if self._inside_workspace(csv_dest):
                csv_bytes = self._tsv_to_csv(payload).encode("utf-8-sig")
                if len(csv_bytes) > self.max_output_bytes * 4:
                    csv_bytes = csv_bytes[: self.max_output_bytes * 4]
                csv_dest.write_bytes(csv_bytes)
                written = csv_dest
        if not filename.lower().endswith(".csv"):
            encoded = payload.encode("utf-8")
            if len(encoded) > self.max_output_bytes * 4:
                encoded = encoded[: self.max_output_bytes * 4]
            destination.write_bytes(encoded)
        return written

    _QUOTE_PAIRS = {
        '"': '"',
        "'": "'",
        "\u201c": "\u201d",
        "\u2018": "\u2019",
    }

    @classmethod
    def unwrap_echo_payload(cls, payload: str) -> str:
        text = str(payload or "").lstrip("\ufeff")
        for _ in range(2):
            stripped = text.strip("\r\n")
            if len(stripped) < 2:
                break
            opener = stripped[0]
            closer = cls._QUOTE_PAIRS.get(opener)
            if closer is None or stripped[-1] != closer:
                break
            first_line = stripped.splitlines()[0]
            if cls._first_line_is_csv_quoted_field(first_line, closer):
                break
            inner = stripped[1:-1]
            if opener == '"':
                inner = inner.replace('\\"', '"')
            elif opener == "'":
                inner = inner.replace("\\'", "'")
            text = inner
        return text

    @staticmethod
    def _first_line_is_csv_quoted_field(first_line: str, closer: str) -> bool:
        index = 1
        while index < len(first_line):
            if first_line[index] == closer:
                if index + 1 < len(first_line) and first_line[index + 1] == closer:
                    index += 2
                    continue
                rest = first_line[index + 1 :]
                return rest == "" or rest.startswith(",")
            index += 1
        return False

    @staticmethod
    def _csv_cell(value: str) -> str:
        cell = value.strip()
        if len(cell) >= 2 and cell[0] == '"' and cell[-1] == '"' and cell.count('"') == 2:
            cell = cell[1:-1]
        elif cell.startswith('"') and cell.count('"') == 1:
            cell = cell[1:]
        elif cell.endswith('"') and cell.count('"') == 1:
            cell = cell[:-1]
        if any(ch in cell for ch in ',"\n'):
            return '"' + cell.replace('"', '""') + '"'
        return cell

    @classmethod
    def _tsv_to_csv(cls, payload: str) -> str:
        payload = cls.unwrap_echo_payload(payload)
        rows = []
        for line in payload.splitlines():
            if "\t" in line:
                rows.append(",".join(cls._csv_cell(cell) for cell in line.split("\t")))
            else:
                rows.append(line)
        return "\n".join(rows) + ("\n" if payload.endswith("\n") else "")

    @classmethod
    def sanitize_csv_bytes(cls, raw: bytes) -> bytes:
        text = raw.decode("utf-8-sig")
        cleaned = cls.unwrap_echo_payload(text)
        if "\t" in cleaned:
            cleaned = cls._tsv_to_csv(cleaned)
        return cleaned.encode("utf-8-sig")

    @staticmethod
    def _quote_profile_path(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    def _profile(self, mode: SandboxMode) -> str:
        profile = [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny file-write*)",
            '(allow file-write* (literal "/dev/null"))',
        ]
        if mode == "workspace-write":
            roots = {
                self.workspace_root,
                Path("/tmp"),
                Path(os.environ.get("TMPDIR", "/tmp")).resolve(),
            }
            for root in roots:
                profile.append(f'(allow file-write* (subpath "{self._quote_profile_path(root)}"))')
        return "".join(profile)

    def _argv(self, command: list[str], mode: SandboxMode) -> list[str]:
        if mode == "danger-full-access":
            return command
        if not self.restricted_available:
            raise SandboxUnavailableError("restricted sandbox backend is unavailable")
        return [str(self.seatbelt), "-p", self._profile(mode), "--", *command]

    def _limits(self) -> None:
        cpu = max(1, math.ceil(self.timeout_seconds) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (self.max_output_bytes * 4, self.max_output_bytes * 4),
        )

    def run(
        self,
        command: list[str],
        *,
        mode: SandboxMode,
        cwd: str = ".",
    ) -> SandboxResult:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("command must contain non-empty argv strings")
        resolved_cwd = self._cwd(cwd)
        before = self._workspace_snapshot(resolved_cwd) if mode != "read-only" else {}
        safe_env = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if name in os.environ
        }
        try:
            completed = subprocess.run(
                self._argv(command, mode),
                cwd=resolved_cwd,
                env=safe_env,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                preexec_fn=self._limits if os.name == "posix" else None,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            timed_out = False
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            timed_out = True
            exit_code = -1
        combined_size = len(stdout) + len(stderr)
        truncated = combined_size > self.max_output_bytes
        if truncated:
            stdout = stdout[: self.max_output_bytes // 2]
            stderr = stderr[: self.max_output_bytes // 2]
        if mode != "read-only" and exit_code == 0 and not timed_out:
            self._materialize_echo_argv(command, resolved_cwd)
        written_files = (
            self._changed_workspace_files(before, resolved_cwd) if mode != "read-only" else []
        )
        return SandboxResult(
            mode=mode,
            command=command,
            cwd=str(resolved_cwd),
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            output_truncated=truncated,
            written_files=written_files,
        )


def register_sandbox_tools(
    registry: ToolRegistry, runner: SandboxRunner, settings: Settings
) -> None:
    def handler(mode: SandboxMode):
        def execute(
            arguments: SandboxCommandArguments,
            _context: ToolExecutionContext,
        ) -> SandboxResult:
            return runner.run(arguments.command, mode=mode, cwd=arguments.cwd)

        return execute

    common = {
        "arguments_model": SandboxCommandArguments,
        "timeout_seconds": settings.sandbox_timeout_seconds + 5,
        "source": "sandbox",
        "concurrency_safe": True,
    }
    if runner.restricted_available:
        registry.register(
            ToolDefinition(
                name="sandbox_read_only",
                description=(
                    "在无网络、禁止文件写入的本地沙箱中执行 argv 命令。"
                    "command 必须是参数数组，不经过 shell 拼接。"
                    "仅用于用户明确要求的本机命令，不要用来访问互联网。"
                ),
                handler=handler("read-only"),
                risk="low",
                builtin=True,
                allowed_roles=frozenset({"operator", "admin"}),
                **common,
            )
        )
        registry.register(
            ToolDefinition(
                name="sandbox_workspace_write",
                description=(
                    "在无网络、仅工作区和临时目录可写的沙箱中执行命令；每次均需人工审批。"
                    "command 是 argv 数组，不经过 shell。表格导出请写入 .csv。"
                    "若使用 echo/printf 且最后一个参数是文件名，运行时会把内容写入该工作区文件；"
                    "含制表符的内容会额外生成同名 .csv。"
                    "仅用于用户明确要求的本机写入，不要用来访问互联网。"
                ),
                handler=handler("workspace-write"),
                risk="medium",
                requires_approval=True,
                builtin=True,
                allowed_roles=frozenset({"operator", "admin"}),
                **common,
            )
        )
    if settings.sandbox_full_access_enabled:
        registry.register(
            ToolDefinition(
                name="sandbox_full_access",
                description=(
                    "不施加本地文件或网络限制地执行命令；危险操作，每次均需管理员审批。"
                    "仅在用户明确要求无隔离命令时使用，不要用来查网页或核对价格。"
                ),
                handler=handler("danger-full-access"),
                risk="high",
                requires_approval=True,
                builtin=True,
                allowed_roles=frozenset({"admin"}),
                **common,
            )
        )
