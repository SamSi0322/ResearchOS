// Typed thin client for the ResearchOS FastAPI backend. No schema codegen -
// we keep the shapes narrow and permissive on purpose, because the backend is
// local and under the same tree. If you add a field, add it here.

export const API_BASE =
  (import.meta as any).env?.VITE_API_BASE || "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = "request failed";
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data);
    } catch {
      try {
        detail = await res.text();
      } catch {
        /* ignore */
      }
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  const text = await res.text();
  if (!text) return undefined as unknown as T;
  return JSON.parse(text) as T;
}

/* ---------- types (kept permissive) ---------- */

export interface Project {
  id: string;
  title: string;
  status: string;
  student_name: string;
  student_ref?: string | null;
  mentor_name: string;
  advisor_name?: string | null;
  research_direction: string;
  target_venues: string[];
  constraints?: string | null;
  exploration_strategy?: string | null;
  provider_profile: string;
  notes?: string | null;
  created_at: string;
  updated_at: string;
  brief?: {
    research_direction: string;
    constraints?: string | null;
    target_venues: string[];
    budget_usd: number;
    strategy?: string | null;
    raw_context?: string | null;
  } | null;
  human_in_loop_enabled?: boolean;
  primary_approver_email?: string | null;
  cc_emails?: string[];
  approval_timeout_hours?: number;
  reminder_interval_hours?: number;
  approval_gates?: string[];
}

export interface ProjectCreateInput {
  title: string;
  student_name: string;
  student_ref?: string;
  mentor_name: string;
  advisor_name?: string;
  research_direction: string;
  target_venues: string[];
  constraints?: string;
  exploration_strategy?: string;
  provider_profile?: string;
  budget_usd?: number;
  notes?: string;
  human_in_loop_enabled?: boolean;
  primary_approver_email?: string;
  cc_emails?: string[];
  approval_timeout_hours?: number;
  reminder_interval_hours?: number;
  approval_gates?: string[];
}

export interface ApprovalRequest {
  id: string;
  project_id: string;
  stage_key: string;
  status: string;
  decision?: string | null;
  decision_note?: string | null;
  approver_email: string;
  cc_emails: string[];
  requested_at: string;
  resolved_at?: string | null;
  timeout_at: string;
  reminder_count: number;
  last_reminder_at?: string | null;
  token: string;
  context_snapshot: Record<string, unknown>;
  outbox_path?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ContextBundleSummary {
  id: string;
  filename: string;
  size_bytes: number;
  extraction_status: string;
  text_file_count: number;
  total_text_chars: number;
  created_at: string;
}

// Canonical provider-validation result returned by /providers/test and
// /smoke/ping. The ``category`` field is the UI's single source of truth for
// what message to show — see SettingsPage for the translation.
export type ValidationCategory =
  | "ok"
  | "auth_error"
  | "model_error"
  | "network_error"
  | "config_error"
  | "provider_error";

export interface ProviderValidationResult {
  ok: boolean;
  category: ValidationCategory;
  provider: string;
  requested_model?: string | null;
  actual_model?: string | null;
  http_status?: number | null;
  provider_error_code?: string | null;
  message: string;
  response_preview?: string | null;
  latency_ms: number;
  execution_mode: "headless_api";
}

export interface ProviderValidationLog {
  id: string;
  credential_id: string | null;
  source: "providers_test" | "smoke_ping" | string;
  provider: string;
  category: ValidationCategory;
  http_status: number | null;
  provider_error_code: string | null;
  requested_model: string | null;
  actual_model: string | null;
  latency_ms: number;
  message: string;
  execution_mode: "headless_api";
  created_at: string;
}

export interface ProviderCredential {
  id: string;
  provider: "openai" | "anthropic" | "mock";
  label: string;
  masked_preview: string;
  default_model?: string | null;
  default_for: string[];
  base_url?: string | null;
  is_default: boolean;
  notes?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProviderCredentialInput {
  provider: "openai" | "anthropic" | "mock";
  label: string;
  api_key: string;
  default_model?: string;
  default_for?: string[];
  base_url?: string;
  is_default?: boolean;
  notes?: string;
}

export interface Idea {
  id: string;
  project_id: string;
  title: string;
  summary: string;
  hypothesis?: string | null;
  novelty_claim?: string | null;
  target_metric?: string | null;
  cluster_tag?: string | null;
  stage: string;
  decision: string;
  score?: number | null;
  rationale?: string | null;
  created_at: string;
  updated_at: string;
  meta?: Record<string, unknown>;
  scorecards?: Array<{
    id: string;
    stage: string;
    novelty: number;
    feasibility: number;
    rigor: number;
    impact: number;
    overall: number;
    rubric?: Record<string, unknown>;
  }>;
}

export interface Spec {
  id: string;
  project_id: string;
  idea_id: string;
  version: number;
  hypothesis: string;
  problem_framing: string;
  target_metrics: string[];
  dataset_assumptions: string;
  baseline: string;
  experiment_plan: string;
  constraints: string;
  success_criteria: string[];
  stop_criteria: string[];
  budget_estimate_usd: number;
  meta?: Record<string, unknown>;
  created_at: string;
}

export interface Run {
  id: string;
  project_id: string;
  spec_id: string;
  idea_id: string;
  status: string;
  result_class?: string | null;
  exit_code?: number | null;
  seed: number;
  code_hash?: string | null;
  workspace_path: string;
  provider_routing?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  config?: Record<string, unknown>;
  summary?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  mock: boolean;
  artifacts?: Artifact[];
  stdout_log?: string | null;
  stderr_log?: string | null;
  created_at: string;
}

export interface Artifact {
  id: string;
  kind: string;
  name: string;
  path: string;
  size_bytes: number;
  sha256?: string | null;
  mock: boolean;
  meta?: Record<string, unknown>;
  created_at: string;
}

export interface Draft {
  id: string;
  manuscript_id: string;
  version: number;
  status: string;
  claim_ids: string[];
  meta?: Record<string, unknown>;
  notes?: string | null;
  mock: boolean;
  sections: Array<{
    id: string;
    key: string;
    title: string;
    content: string;
    order_index: number;
    claim_refs: string[];
    evidence_refs: Array<Record<string, unknown>>;
  }>;
  created_at: string;
  updated_at: string;
}

export interface Manuscript {
  id: string;
  project_id: string;
  title: string;
  target_venue?: string | null;
  status: string;
  drafts: Draft[];
  created_at: string;
  updated_at: string;
}

export interface ReviewIssue {
  id: string;
  project_id: string;
  draft_id?: string | null;
  subject_kind: string;
  subject_id: string;
  reviewer_class: string;
  severity: string;
  state: string;
  description: string;
  evidence?: string | null;
  suggested_remediation?: string | null;
  resolution_note?: string | null;
  meta?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DeliveryPackage {
  id: string;
  project_id: string;
  version: number;
  status: string;
  zip_path?: string | null;
  sha256?: string | null;
  size_bytes: number;
  summary?: string | null;
  notes?: string | null;
  supersedes_id?: string | null;
  included_ids: Record<string, string[]>;
  mock: boolean;
  created_at: string;
}

export interface Session {
  id: string;
  project_id: string;
  scheduled_at: string;
  mentor_name: string;
  status: string;
  notes?: string | null;
  student_participation_notes?: string | null;
  next_actions: string[];
  unresolved_blockers: string[];
  student_must_understand: string[];
  created_at: string;
}

export interface AuditEvent {
  id: string;
  project_id?: string | null;
  kind: string;
  actor: string;
  subject_kind?: string | null;
  subject_id?: string | null;
  message?: string | null;
  payload?: Record<string, unknown>;
  created_at: string;
}

export interface Claim {
  id: string;
  project_id: string;
  idea_id?: string | null;
  run_id?: string | null;
  text: string;
  kind: string;
  value?: string | null;
  quantitative: boolean;
  evidence_refs: Array<Record<string, unknown>>;
  mock: boolean;
  created_at: string;
}

export interface FunnelSummary {
  by_stage: Record<string, number>;
  targets: Record<string, number>;
}

/* ---------- client ---------- */

export const api = {
  /* health */
  health: () => request<{ ok: boolean; service: string; version: string }>("/health"),

  /* providers */
  listProviders: () => request<ProviderCredential[]>("/providers"),
  addProvider: (body: ProviderCredentialInput) =>
    request<ProviderCredential>("/providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateProvider: (
    id: string,
    body: Partial<ProviderCredentialInput> & { api_key?: string }
  ) =>
    request<ProviderCredential>(`/providers/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteProvider: (id: string) =>
    request<{ message: string }>(`/providers/${id}`, { method: "DELETE" }),
  testProvider: (credential_id: string, prompt?: string) =>
    request<ProviderValidationResult>(`/providers/test`, {
      method: "POST",
      body: JSON.stringify({ credential_id, prompt: prompt || "Respond with OK." }),
    }),
  validationHistory: (filters?: {
    credential_id?: string;
    provider?: string;
    limit?: number;
  }) => {
    const params = new URLSearchParams();
    if (filters?.credential_id) params.set("credential_id", filters.credential_id);
    if (filters?.provider) params.set("provider", filters.provider);
    if (filters?.limit) params.set("limit", String(filters.limit));
    const qs = params.toString();
    return request<ProviderValidationLog[]>(
      `/providers/validation/history${qs ? `?${qs}` : ""}`
    );
  },
  validationLatest: () =>
    request<ProviderValidationLog[]>(`/providers/validation/latest`),

  /* projects */
  listProjects: () => request<Project[]>("/projects"),
  createProject: (body: ProjectCreateInput) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),
  getProject: (id: string) => request<Project>(`/projects/${id}`),
  updateProject: (id: string, body: Partial<ProjectCreateInput>) =>
    request<Project>(`/projects/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  funnelSummary: (project_id: string) =>
    request<FunnelSummary>(`/projects/${project_id}/funnel/summary`),

  /* ideas */
  listIdeas: (project_id: string, stage?: string) =>
    request<Idea[]>(`/projects/${project_id}/ideas${stage ? `?stage=${stage}` : ""}`),
  generateIdeas: (project_id: string, count: number, extra_context?: string) =>
    request<Idea[]>(`/projects/${project_id}/ideas/generate`, {
      method: "POST",
      body: JSON.stringify({ count, extra_context }),
    }),
  scoreIdeas: (project_id: string, stage: string) =>
    request<unknown[]>(
      `/projects/${project_id}/ideas/score?stage=${stage}`,
      { method: "POST", body: JSON.stringify({}) }
    ),
  advanceFunnel: (
    project_id: string,
    from_stage: string,
    to_stage: string,
    keep_count?: number
  ) =>
    request<{ promoted: string[]; rejected: string[]; to_stage: string }>(
      `/projects/${project_id}/ideas/advance`,
      {
        method: "POST",
        body: JSON.stringify({
          from_stage,
          to_stage,
          keep_count,
          auto_reject: true,
        }),
      }
    ),
  ideaDecision: (
    project_id: string,
    idea_id: string,
    decision: string,
    rationale?: string,
    promote_to_stage?: string
  ) =>
    request<Idea>(`/projects/${project_id}/ideas/${idea_id}/decision`, {
      method: "PUT",
      body: JSON.stringify({ decision, rationale, promote_to_stage }),
    }),

  /* specs */
  listSpecs: (project_id: string, idea_id?: string) =>
    request<Spec[]>(
      `/projects/${project_id}/specs${idea_id ? `?idea_id=${idea_id}` : ""}`
    ),
  generateSpec: (project_id: string, idea_id: string, extra_instructions?: string) =>
    request<Spec>(`/projects/${project_id}/specs/generate`, {
      method: "POST",
      body: JSON.stringify({ idea_id, extra_instructions }),
    }),

  /* runs */
  listRuns: (project_id: string) => request<Run[]>(`/projects/${project_id}/runs`),
  getRun: (project_id: string, id: string) =>
    request<Run>(`/projects/${project_id}/runs/${id}`),
  startRun: (
    project_id: string,
    body: { spec_id: string; worker: string; seed?: number; extra_instructions?: string }
  ) =>
    request<Run>(`/projects/${project_id}/runs/start`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  analyzeRun: (project_id: string, run_id: string) =>
    request<{
      run_id: string;
      verdict: string;
      metrics: Record<string, unknown>;
      baseline_delta: Record<string, unknown>;
      promoted_idea: boolean;
      claim_ids: string[];
    }>(`/projects/${project_id}/runs/${run_id}/analyze`, { method: "POST" }),

  /* drafts & claims */
  listManuscripts: (project_id: string) =>
    request<Manuscript[]>(`/projects/${project_id}/drafts`),
  generateDraft: (
    project_id: string,
    body: { manuscript_title?: string; target_venue?: string; include_run_ids?: string[] }
  ) =>
    request<Draft>(`/projects/${project_id}/drafts/generate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listClaims: (project_id: string) =>
    request<Claim[]>(`/projects/${project_id}/drafts/claims/all`),

  /* reviews */
  listIssues: (project_id: string) =>
    request<ReviewIssue[]>(`/projects/${project_id}/reviews`),
  runReviewers: (project_id: string, draft_id?: string) =>
    request<ReviewIssue[]>(`/projects/${project_id}/reviews/run`, {
      method: "POST",
      body: JSON.stringify({ draft_id }),
    }),
  updateIssue: (
    project_id: string,
    issue_id: string,
    patch: { state?: string; resolution_note?: string; severity?: string }
  ) =>
    request<ReviewIssue>(`/projects/${project_id}/reviews/${issue_id}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  /* packages */
  listPackages: (project_id: string) =>
    request<DeliveryPackage[]>(`/projects/${project_id}/packages`),
  buildPackage: (
    project_id: string,
    body: { include_mock?: boolean; allow_with_waived_p2?: boolean; notes?: string }
  ) =>
    request<DeliveryPackage>(`/projects/${project_id}/packages/build`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  downloadPackage: (project_id: string, pkg_id: string) =>
    `${API_BASE}/projects/${project_id}/packages/${pkg_id}/download`,

  /* sessions */
  listSessions: (project_id: string) =>
    request<Session[]>(`/projects/${project_id}/sessions`),
  createSession: (
    project_id: string,
    body: Partial<Session> & {
      scheduled_at: string;
      mentor_name: string;
      status?: string;
      next_actions?: string[];
      unresolved_blockers?: string[];
      student_must_understand?: string[];
    }
  ) =>
    request<Session>(`/projects/${project_id}/sessions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateSession: (project_id: string, session_id: string, body: Partial<Session>) =>
    request<Session>(`/projects/${project_id}/sessions/${session_id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteSession: (project_id: string, session_id: string) =>
    request<{ ok: boolean }>(`/projects/${project_id}/sessions/${session_id}`, {
      method: "DELETE",
    }),

  /* audit */
  listAudit: (project_id?: string) =>
    request<AuditEvent[]>(
      `/audit${project_id ? `?project_id=${project_id}` : ""}`
    ),

  /* smoke */
  smokeHealth: () =>
    request<{
      smoke_mode: boolean;
      run_mode: string;
      execution_mode: "headless_api";
      openai_ready: boolean;
      anthropic_ready: boolean;
      credentials: Array<{
        id: string;
        provider: string;
        label: string;
        masked_preview: string;
        default_model?: string | null;
      }>;
      settings_snapshot: Record<string, unknown>;
      bootstrap: Record<string, unknown>;
    }>(`/smoke/health`),
  smokePing: (provider: "openai" | "anthropic") =>
    request<ProviderValidationResult>(`/smoke/ping`, {
      method: "POST",
      body: JSON.stringify({ provider, prompt: "Respond with OK." }),
    }),
  smokeRun: (body?: {
    idea_count?: number;
    worker?: string;
    concurrency?: number;
  }) =>
    request<{
      project_id: string;
      smoke_mode: boolean;
      idea_ids: string[];
      batch: Array<Record<string, unknown>>;
      draft_id: string | null;
      review_issue_count: number;
      package_id: string | null;
      package_zip_path: string | null;
      messages: string[];
    }>(`/smoke/run`, {
      method: "POST",
      body: JSON.stringify(body || {}),
    }),

  /* manuscript quality + readiness + review summary */
  latestQuality: (project_id: string) =>
    request<Record<string, unknown>>(
      `/projects/${project_id}/drafts/quality/latest`
    ),
  readiness: (project_id: string) =>
    request<{
      tier: string;
      package_decision: string;
      reasons: string[];
      quality: Record<string, unknown> | null;
      review: Record<string, unknown>;
      smoke_mode: boolean;
      has_mock_inputs: boolean;
    }>(`/projects/${project_id}/drafts/readiness`),
  reviewSummary: (project_id: string) =>
    request<Record<string, unknown>>(
      `/projects/${project_id}/drafts/review-summary`
    ),

  /* package manifest + manuscript fetch */
  packageManifest: (project_id: string, package_id: string) =>
    request<Record<string, unknown>>(
      `/projects/${project_id}/packages/${package_id}/manifest`
    ),
  packageManuscriptMarkdown: async (project_id: string, package_id: string) => {
    const res = await fetch(
      `${API_BASE}/projects/${project_id}/packages/${package_id}/manuscript.md`,
      { headers: { Accept: "text/plain" } }
    );
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.text();
  },
  downloadPackageManuscriptPdf: (project_id: string, package_id: string) =>
    `${API_BASE}/projects/${project_id}/packages/${package_id}/manuscript.pdf`,
  downloadLatestManuscriptPdf: (project_id: string) =>
    `${API_BASE}/projects/${project_id}/packages/manuscript/latest-pdf`,

  /* approvals */
  listProjectApprovals: (project_id: string) =>
    request<ApprovalRequest[]>(`/projects/${project_id}/approvals`),
  createApproval: (
    project_id: string,
    body: { stage_key: string; context_snapshot?: Record<string, unknown> }
  ) =>
    request<ApprovalRequest>(`/projects/${project_id}/approvals`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listPendingApprovals: () => request<ApprovalRequest[]>(`/approvals`),
  decideApproval: (
    approval_id: string,
    body: { decision: string; note?: string; actor?: string }
  ) =>
    request<ApprovalRequest>(`/approvals/${approval_id}/decision`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  scanReminders: () =>
    request<{ reminded: string[]; count: number }>(`/approvals/scan/reminders`, {
      method: "POST",
    }),
  scanExpirations: () =>
    request<{ expired: string[]; count: number }>(`/approvals/scan/expire`, {
      method: "POST",
    }),

  /* context bundles */
  listContextBundles: (project_id: string) =>
    request<ContextBundleSummary[]>(`/projects/${project_id}/context-bundles`),
  uploadContextBundle: async (project_id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(
      `${API_BASE}/projects/${project_id}/context-bundles`,
      { method: "POST", body: fd }
    );
    if (!res.ok) {
      let detail = "upload failed";
      try {
        const data = await res.json();
        detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data);
      } catch {
        detail = await res.text();
      }
      throw new Error(`${res.status} ${detail}`);
    }
    return (await res.json()) as ContextBundleSummary;
  },
  deleteContextBundle: (project_id: string, bundle_id: string) =>
    request<{ ok: boolean }>(
      `/projects/${project_id}/context-bundles/${bundle_id}`,
      { method: "DELETE" }
    ),

  /* Backend-orchestrated batch runner.
   *
   * Replaces the previous browser-side spec/run fanout, which bypassed
   * backend HITL gating and concurrency control. Approval-blocked batches
   * surface as a 409 with a structured `approval_required` body; the
   * caller catches the Error thrown by `request` and can parse the
   * message for details.
   */
  batchRun: (
    project_id: string,
    body: { idea_ids: string[]; worker?: string; concurrency?: number }
  ) =>
    request<{
      outcomes: Array<{
        idea_id: string;
        spec_id?: string | null;
        run_id?: string | null;
        run_status?: string | null;
        result_class?: string | null;
        verdict?: string | null;
        claim_ids: string[];
        error?: string | null;
      }>;
      total: number;
      succeeded: number;
      failed: number;
    }>(`/projects/${project_id}/runs/batch`, {
      method: "POST",
      body: JSON.stringify({
        idea_ids: body.idea_ids,
        worker: body.worker || "two_step",
        concurrency: body.concurrency ?? null,
      }),
    }),
};
