from .base import BaseCodeWorker, CodeWorkerResult, CodeWorkerRequest
from .job_runner import JobRunner, assert_not_interactive_agent, get_job_runner

__all__ = [
    "BaseCodeWorker",
    "CodeWorkerResult",
    "CodeWorkerRequest",
    "ClaudeCodeWorker",
    "CodexWorker",
    "JobRunner",
    "get_job_runner",
    "assert_not_interactive_agent",
]


def __getattr__(name: str):
    if name == "ClaudeCodeWorker":
        from .claude_code_worker import ClaudeCodeWorker

        return ClaudeCodeWorker
    if name == "CodexWorker":
        from .codex_worker import CodexWorker

        return CodexWorker
    raise AttributeError(name)
