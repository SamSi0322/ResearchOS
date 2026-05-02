"""Local experiment job runner.

For the local MVP we just use ``asyncio.create_subprocess_exec`` + a
``BoundedSemaphore`` for concurrency. Each run writes stdout/stderr to the
workspace ``logs/`` directory and a ``metrics.json`` is expected in
``outputs/``. The runner does not know about ORM state - the caller
(ExperimentRunnerService) owns the DB row updates.

Runtime guardrail
-----------------
This runner ONLY spawns ``sys.executable`` (the Python interpreter). It must
never shell out to interactive coding agents (``codex``, ``claude``,
``claude-code``). ResearchOS runtime is strictly headless: all provider work
happens via HTTP adapters under ``app/providers/``. The check below is
intentional defence-in-depth — if a future contributor adds an agent-binary
spawn here we want a loud refusal rather than a silent behaviour change.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.utils import get_logger


# Names of interactive coding-agent binaries we explicitly refuse to spawn
# from the runtime. The development workflow may use them (hence Claude Code
# and Codex as part of how this repo was BUILT), but nothing under
# ``app/workers/`` is allowed to depend on their UI / processes at runtime.
_FORBIDDEN_AGENT_EXECUTABLE_NAMES = (
    "codex",
    "codex.exe",
    "codex.cmd",
    "claude",
    "claude.exe",
    "claude.cmd",
    "claude-code",
    "claude-code.exe",
)


def assert_not_interactive_agent(executable: str) -> None:
    """Refuse to spawn an interactive coding-agent binary.

    Matches by basename so absolute paths, PATH-resolved names, and ``.cmd``
    shims all get caught. Raises ``RuntimeError`` rather than returning a
    boolean so a misuse surfaces immediately in local dev and in audit logs.
    """
    base = (executable or "").strip().strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base in _FORBIDDEN_AGENT_EXECUTABLE_NAMES:
        raise RuntimeError(
            f"refusing to spawn interactive coding-agent binary '{base}' "
            "from runtime code: ResearchOS runtime is headless (execution_mode="
            "headless_api) and uses provider adapters under app/providers/. "
            "If you need to automate Codex / Claude Code from a script, use "
            "their non-interactive modes (`codex exec`, `claude -p/--print`) "
            "from dev tooling outside the runtime."
        )


# Any env var whose name contains one of these fragments is withheld from the
# subprocess. The point is to prevent model-generated code from reading the
# parent process's secrets (our master key, any provider key accidentally in
# env, DB URLs, etc.).  We also keep an explicit allowlist of environment
# essentials so Python can still start on Windows.
_SECRET_ENV_SUBSTRINGS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CRED",
    "AUTH",
    "APP_MASTER",
    "RESEARCHOS_DB_URL",
    "RESEARCHOS_SECRETS_DIR",
)
_ALWAYS_KEEP = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONUNBUFFERED",
    "PYTHONIOENCODING",
}


def _scrub_env(source: dict[str, str]) -> dict[str, str]:
    """Return a minimal env dict safe to pass to untrusted generated code."""
    clean: dict[str, str] = {}
    for k, v in source.items():
        upper = k.upper()
        if upper in _ALWAYS_KEEP:
            clean[k] = v
            continue
        if any(frag in upper for frag in _SECRET_ENV_SUBSTRINGS):
            continue
        # Skip anything that starts with RESEARCHOS_ as a defence-in-depth.
        if upper.startswith("RESEARCHOS_"):
            continue
        clean[k] = v
    return clean


def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Best-effort: kill the subprocess and any grandchildren it spawned."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            # /T = tree kill, /F = force. Run synchronously with no stdio.
            import subprocess  # local import to avoid top-level cost

            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:  # noqa: BLE001
        # Fall back to killing just the direct child so we at least release it.
        try:
            proc.kill()
        except ProcessLookupError:
            pass

logger = get_logger(__name__)


@dataclass
class JobOutcome:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


@dataclass
class SetupOutcome:
    ok: bool
    python_executable: str
    stdout: str
    stderr: str
    duration_s: float
    installed: list[str] = field(default_factory=list)
    skipped: bool = False


class JobRunner:
    def __init__(self, max_concurrent: int, default_timeout: int) -> None:
        # Semaphores bind to the event loop they were created on. The backend
        # is a singleton used across multiple pytest-asyncio test loops, so we
        # create the semaphore lazily the first time ``run_python`` is awaited
        # on a given loop.
        self._max_concurrent = max(1, int(max_concurrent))
        self.default_timeout = default_timeout
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop = None

    def _current_sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self._max_concurrent)
            self._sem_loop = loop
        return self._sem

    async def run_python(
        self,
        *,
        cwd: Path,
        script: str = "train.py",
        env_extra: dict[str, str] | None = None,
        timeout: int | None = None,
        python_executable: str | None = None,
    ) -> JobOutcome:
        script_path = Path(cwd) / script
        if not script_path.exists():
            return JobOutcome(
                exit_code=127,
                stdout="",
                stderr=f"script not found: {script_path}",
                timed_out=False,
                duration_s=0.0,
            )

        python_exec = python_executable or sys.executable
        logger.info("job_runner starting", extra={"cwd": str(cwd), "script": script})
        return await self._run_exec(
            [python_exec, str(script_path)],
            cwd=cwd,
            env_extra=env_extra,
            timeout=timeout,
        )

    async def prepare_python_env(
        self,
        *,
        cwd: Path,
        requirements_file: str = "requirements.txt",
        timeout: int | None = None,
    ) -> SetupOutcome:
        requirements_path = Path(cwd) / requirements_file
        installed = _read_requirements(requirements_path)
        if not requirements_path.exists() or not installed:
            return SetupOutcome(
                ok=True,
                python_executable=sys.executable,
                stdout="",
                stderr="",
                duration_s=0.0,
                installed=installed,
                skipped=True,
            )

        venv_dir = Path(cwd).parent / ".venv_run"
        create = await self._run_exec(
            [sys.executable, "-m", "venv", str(venv_dir)],
            cwd=Path(cwd),
            timeout=max(timeout or self.default_timeout, 120),
        )
        python_exec = _venv_python_path(venv_dir)
        if create.exit_code != 0 or create.timed_out or not python_exec.exists():
            return SetupOutcome(
                ok=False,
                python_executable=sys.executable,
                stdout=create.stdout,
                stderr=create.stderr,
                duration_s=create.duration_s,
                installed=installed,
            )

        install = await self._run_exec(
            [
                str(python_exec),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements_path),
            ],
            cwd=Path(cwd),
            timeout=max(timeout or self.default_timeout, 300),
            env_extra={"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        return SetupOutcome(
            ok=install.exit_code == 0 and not install.timed_out,
            python_executable=str(python_exec),
            stdout=_join_output(create.stdout, install.stdout),
            stderr=_join_output(create.stderr, install.stderr),
            duration_s=create.duration_s + install.duration_s,
            installed=installed,
        )

    async def _run_exec(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env_extra: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> JobOutcome:
        env = _scrub_env(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        if env_extra:
            env.update({k: v for k, v in env_extra.items() if v is not None})

        timeout = timeout or self.default_timeout
        spawn_kwargs: dict = {}
        if sys.platform == "win32":
            spawn_kwargs["creationflags"] = 0x00000200
        else:
            spawn_kwargs["start_new_session"] = True

        async with self._current_sem():
            executable = str(argv[0])
            assert_not_interactive_agent(executable)
            loop = asyncio.get_event_loop()
            started = loop.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *[str(a) for a in argv],
                    cwd=str(cwd),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **spawn_kwargs,
                )
            except (OSError, FileNotFoundError) as e:
                return JobOutcome(
                    exit_code=127,
                    stdout="",
                    stderr=f"spawn failed: {e!s}",
                    timed_out=False,
                    duration_s=0.0,
                )

            communicate_task = asyncio.create_task(proc.communicate())
            try:
                stdout, stderr, timed_out = await self._wait_for_process(
                    proc=proc,
                    communicate_task=communicate_task,
                    timeout=timeout,
                    loop=loop,
                )
            except asyncio.TimeoutError:
                _terminate_process_tree(proc)
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=5
                    )
                except asyncio.TimeoutError:
                    stdout, stderr = b"", b"process tree did not exit in time after kill"
                timed_out = True

            duration = loop.time() - started
            return JobOutcome(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=timed_out,
                duration_s=duration,
            )

    async def _wait_for_process(
        self,
        *,
        proc: asyncio.subprocess.Process,
        communicate_task: asyncio.Task,
        timeout: int,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[bytes, bytes, bool]:
        deadline = loop.time() + timeout
        while True:
            if communicate_task.done():
                stdout, stderr = await communicate_task
                return stdout, stderr, False
            # In WSL/sandboxed Python, the child watcher can observe a
            # successful returncode while communicate()/wait() remains blocked
            # on pipe finalization. Treat the process as complete instead of
            # waiting until the full timeout and misclassifying a valid run.
            if proc.returncode is not None:
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate_task),
                        timeout=0.25,
                    )
                except asyncio.TimeoutError:
                    communicate_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await communicate_task
                    return (
                        b"",
                        b"process exited but output pipes did not close before runner grace period",
                        False,
                    )
                return stdout, stderr, False
            remaining = deadline - loop.time()
            if remaining <= 0:
                communicate_task.cancel()
                with suppress(asyncio.CancelledError):
                    await communicate_task
                raise asyncio.TimeoutError
            await asyncio.sleep(min(0.05, remaining))


def _venv_python_path(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _read_requirements(requirements_path: Path) -> list[str]:
    if not requirements_path.exists():
        return []
    entries: list[str] = []
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


def _join_output(first: str, second: str) -> str:
    if first and second:
        return f"{first.rstrip()}\n{second.lstrip()}"
    return first or second


_instance: JobRunner | None = None


def get_job_runner() -> JobRunner:
    global _instance
    if _instance is None:
        s = get_settings()
        _instance = JobRunner(max_concurrent=s.max_concurrency, default_timeout=s.run_timeout)
    return _instance
