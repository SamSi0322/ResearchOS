"""String enums used across models, schemas and services."""

from __future__ import annotations

from enum import Enum


class ProjectStatus(str, Enum):
    active = "active"
    paused = "paused"
    archived = "archived"
    packaged = "packaged"


class FunnelStage(str, Enum):
    S0 = "S0"  # formal candidate pool
    S1 = "S1"  # structured screening
    S2 = "S2"  # pilot validation
    S3 = "S3"  # robustness validation
    S4 = "S4"  # draft/review/package


class IdeaStage(str, Enum):
    raw = "raw"
    normalized = "normalized"
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"


class IdeaDecision(str, Enum):
    pending = "pending"
    keep = "keep"
    reject = "reject"
    promote = "promote"
    waived = "waived"


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"
    timed_out = "timed_out"


class RunResultClass(str, Enum):
    succeeded_valid = "succeeded_valid"
    succeeded_invalid = "succeeded_invalid"
    failed_retriable = "failed_retriable"
    failed_terminal = "failed_terminal"
    canceled = "canceled"


class DraftStatus(str, Enum):
    drafting = "drafting"
    in_review = "in_review"
    revised = "revised"
    accepted = "accepted"
    superseded = "superseded"


class ReviewSeverity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ReviewState(str, Enum):
    open = "open"
    resolved = "resolved"
    waived = "waived"
    reopened = "reopened"


class ReviewerClass(str, Enum):
    methodology = "methodology"
    statistics = "statistics"
    novelty = "novelty"
    reproducibility = "reproducibility"
    manuscript = "manuscript"
    package = "package"


class PackageStatus(str, Enum):
    draft = "draft"
    frozen = "frozen"
    superseded = "superseded"


class SessionStatus(str, Enum):
    scheduled = "scheduled"
    completed = "completed"
    canceled = "canceled"


class AuditKind(str, Enum):
    project_created = "project_created"
    provider_credential_added = "provider_credential_added"
    provider_credential_deleted = "provider_credential_deleted"
    provider_credential_tested = "provider_credential_tested"
    ideas_generated = "ideas_generated"
    idea_decision = "idea_decision"
    funnel_advanced = "funnel_advanced"
    spec_generated = "spec_generated"
    code_generated = "code_generated"
    run_started = "run_started"
    run_finished = "run_finished"
    result_validated = "result_validated"
    draft_created = "draft_created"
    draft_revised = "draft_revised"
    review_issue_opened = "review_issue_opened"
    review_issue_resolved = "review_issue_resolved"
    review_issue_waived = "review_issue_waived"
    package_created = "package_created"
    human_override = "human_override"
    session_logged = "session_logged"
    budget_event = "budget_event"
    approval_created = "approval_created"
    approval_approved = "approval_approved"
    approval_rejected = "approval_rejected"
    approval_clarification_requested = "approval_clarification_requested"
    approval_expired = "approval_expired"
    approval_reminder_sent = "approval_reminder_sent"
    pipeline_paused = "pipeline_paused"
    pipeline_resumed = "pipeline_resumed"
    context_bundle_uploaded = "context_bundle_uploaded"
    context_bundle_extracted = "context_bundle_extracted"
    context_bundle_failed = "context_bundle_failed"


class ProviderName(str, Enum):
    openai = "openai"
    anthropic = "anthropic"
    mock = "mock"


class CodeWorkerKind(str, Enum):
    claude_code = "claude_code"
    codex = "codex"


class TaskKind(str, Enum):
    """Logical task types that get routed to providers."""

    idea_generation = "idea_generation"
    structured_screening = "structured_screening"
    spec_generation = "spec_generation"
    code_generation = "code_generation"
    code_review = "code_review"
    result_analysis = "result_analysis"
    draft_generation = "draft_generation"
    review = "review"


class GateKey(str, Enum):
    """Named approval checkpoints along the pipeline.

    ``post_shortlist``       Gate A. After an idea shortlist has been
                              formed, before any batch / pilot run starts.
    ``post_pilot_evidence``  Gate B. After the first pilot batch completes
                              and before deeper / final runs.
    ``pre_package_freeze``   Gate C. Before a manuscript package is frozen
                              for delivery.
    """

    post_shortlist = "post_shortlist"
    post_pilot_evidence = "post_pilot_evidence"
    pre_package_freeze = "pre_package_freeze"


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    clarification_requested = "clarification_requested"
    expired = "expired"
    canceled = "canceled"


class ApprovalDecision(str, Enum):
    approve = "approve"
    reject = "reject"
    request_changes = "request_changes"


class ContextBundleStatus(str, Enum):
    pending = "pending"
    extracting = "extracting"
    ok = "ok"
    failed = "failed"
    deferred = "deferred"
