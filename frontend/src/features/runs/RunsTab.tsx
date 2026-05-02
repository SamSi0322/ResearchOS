import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StatusBadge from "../../app/components/StatusBadge";
import Toast from "../../app/components/Toast";

export default function RunsTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const runs = useQuery({
    queryKey: ["runs", projectId],
    queryFn: () => api.listRuns(projectId),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "running") ? 2000 : false),
  });
  const specs = useQuery({ queryKey: ["specs", projectId], queryFn: () => api.listSpecs(projectId) });
  const [specId, setSpecId] = useState("");
  // Default to the two-step collaboration: headless builder pass followed by
  // an independent headless reviewer pass. Both steps run via HTTP provider
  // adapters; no interactive Claude Code / Codex sessions are involved.
  // Operators can still drop to a single worker per run when needed.
  const [worker, setWorker] = useState("two_step");
  const [seed, setSeed] = useState(0);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const startMut = useMutation({
    mutationFn: () => api.startRun(projectId, { spec_id: specId, worker, seed }),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["runs", projectId] });
      setSelectedRun(run.id);
      setToast(`Run ${run.id.slice(0, 8)} ${run.status}`);
    },
    onError: (e: Error) => setToast(e.message),
  });

  const analyzeMut = useMutation({
    mutationFn: (run_id: string) => api.analyzeRun(projectId, run_id),
    onSuccess: (res) => {
      qc.invalidateQueries();
      setToast(`Verdict: ${res.verdict}, claims: ${res.claim_ids.length}`);
    },
    onError: (e: Error) => setToast(e.message),
  });

  const runDetail = useQuery({
    queryKey: ["run", selectedRun],
    queryFn: () => api.getRun(projectId, selectedRun!),
    enabled: !!selectedRun,
  });

  return (
    <div>
      <BatchCard projectId={projectId} />

      <div className="card">
        <h3>Start a run</h3>
        <div className="row">
          <div className="field" style={{ minWidth: 320 }}>
            <label>Spec</label>
            <select value={specId} onChange={(e) => setSpecId(e.target.value)}>
              <option value="">— pick a spec —</option>
              {specs.data?.map((s) => (
                <option key={s.id} value={s.id}>
                  v{s.version} — {s.hypothesis.slice(0, 60)}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ minWidth: 220 }}>
            <label>Worker</label>
            <select value={worker} onChange={(e) => setWorker(e.target.value)}>
              <option value="two_step">Builder + Reviewer (recommended)</option>
              <option value="claude_code">Builder only (headless code worker)</option>
              <option value="codex">Reviewer only (headless review worker)</option>
            </select>
            <span className="help">
              Both passes run headless via provider APIs. No local Claude Code
              or Codex session is involved at runtime.
            </span>
          </div>
          <div className="field" style={{ minWidth: 100 }}>
            <label>Seed</label>
            <input
              type="number"
              value={seed}
              onChange={(e) => setSeed(Number(e.target.value) || 0)}
            />
          </div>
          <div style={{ alignSelf: "end", paddingBottom: 12 }}>
            <button
              className="primary"
              disabled={!specId || startMut.isPending}
              onClick={() => startMut.mutate()}
            >
              {startMut.isPending ? "Running…" : "Start run"}
            </button>
          </div>
        </div>
      </div>

      <div className="card">
        <h3>Runs</h3>
        {runs.data && runs.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Status</th>
                <th>Result</th>
                <th>Metrics</th>
                <th>Provider</th>
                <th style={{ width: 220 }}></th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id}>
                  <td>
                    <button
                      className="small ghost"
                      onClick={() => setSelectedRun(r.id)}
                      style={{ padding: 0 }}
                    >
                      {r.id}
                    </button>
                    <div style={{ fontSize: 11, color: "#888" }}>
                      seed={r.seed} · exit={r.exit_code ?? "-"}
                    </div>
                  </td>
                  <td>
                    <StatusBadge value={r.status} mock={r.mock} />
                  </td>
                  <td>
                    <StatusBadge value={r.result_class || undefined} />
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {summariseMetrics(r.metrics)}
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {String(r.provider_routing?.provider || "")}/
                    {String(r.provider_routing?.model || "")}
                  </td>
                  <td>
                    <button
                      className="small"
                      disabled={analyzeMut.isPending}
                      onClick={() => analyzeMut.mutate(r.id)}
                    >
                      Analyze
                    </button>{" "}
                    <button className="small" onClick={() => setSelectedRun(r.id)}>
                      Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">No runs yet.</div>
        )}
      </div>

      {selectedRun && runDetail.data && <RunDetail run={runDetail.data} />}

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}

function summariseMetrics(m: Record<string, unknown> | undefined): string {
  if (!m || Object.keys(m).length === 0) return "-";
  const baseline = (m as any).baseline || {};
  const variant = (m as any).variant || {};
  const delta = (m as any).delta || {};
  const keys = Object.keys(delta).slice(0, 2);
  return keys
    .map((k) => `${k}: v=${num(variant[k])} Δ=${num(delta[k])}`)
    .join(" · ");
}

function num(v: unknown): string {
  if (typeof v !== "number") return String(v ?? "-");
  if (Math.abs(v) < 1e-3) return v.toExponential(2);
  return v.toFixed(4);
}

function RunDetail({ run }: { run: ReturnType<typeof api.getRun> extends Promise<infer T> ? T : never }) {
  return (
    <div className="card">
      <h3>Run {run.id}</h3>
      <div className="grid two">
        <div>
          <h4 style={{ fontSize: 12, color: "#666", textTransform: "uppercase" }}>Metadata</h4>
          <dl className="kv">
            <dt>Status</dt>
            <dd><StatusBadge value={run.status} mock={run.mock} /></dd>
            <dt>Result</dt>
            <dd><StatusBadge value={run.result_class || undefined} /></dd>
            <dt>Exit code</dt>
            <dd>{run.exit_code ?? "-"}</dd>
            <dt>Seed</dt>
            <dd>{run.seed}</dd>
            <dt>Code hash</dt>
            <dd className="mono" style={{ fontSize: 11 }}>{run.code_hash?.slice(0, 16) || "-"}</dd>
            <dt>Provider</dt>
            <dd className="mono" style={{ fontSize: 11 }}>
              {String(run.provider_routing?.provider || "")}/{String(run.provider_routing?.model || "")}
            </dd>
            <dt>Workspace</dt>
            <dd className="mono" style={{ fontSize: 11 }}>{run.workspace_path}</dd>
          </dl>
        </div>
        <div>
          <h4 style={{ fontSize: 12, color: "#666", textTransform: "uppercase" }}>Metrics</h4>
          <pre className="mono">{JSON.stringify(run.metrics, null, 2)}</pre>
        </div>
      </div>
      {run.stdout_log && (
        <>
          <h4 style={{ fontSize: 12, color: "#666", textTransform: "uppercase" }}>stdout</h4>
          <pre className="log">{run.stdout_log}</pre>
        </>
      )}
      {run.stderr_log && (
        <>
          <h4 style={{ fontSize: 12, color: "#666", textTransform: "uppercase" }}>stderr</h4>
          <pre className="log">{run.stderr_log}</pre>
        </>
      )}
      {run.artifacts && run.artifacts.length > 0 && (
        <>
          <h4 style={{ fontSize: 12, color: "#666", textTransform: "uppercase" }}>Artifacts</h4>
          <table>
            <thead>
              <tr>
                <th>Kind</th>
                <th>Name</th>
                <th>Size</th>
                <th>sha256</th>
              </tr>
            </thead>
            <tbody>
              {run.artifacts.map((a) => (
                <tr key={a.id}>
                  <td><span className="badge">{a.kind}</span></td>
                  <td className="mono">{a.name}</td>
                  <td>{a.size_bytes}</td>
                  <td className="mono" style={{ fontSize: 10 }}>{a.sha256?.slice(0, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

function BatchCard({ projectId }: { projectId: string }) {
  const ideas = useQuery({
    queryKey: ["ideas", projectId],
    queryFn: () => api.listIdeas(projectId),
  });
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [batchWorker, setBatchWorker] = useState("two_step");
  const [running, setRunning] = useState(false);
  // The backend batch endpoint returns a list of per-idea outcomes. We keep
  // an internal shape that also has a "run" nested object for the table
  // renderer below.
  const [outcomes, setOutcomes] = useState<
    Array<{
      idea_id: string;
      run?: { id: string; status: string; result_class?: string | null; mock: boolean };
      error?: string;
    }>
  >([]);
  const [blocked, setBlocked] = useState<string | null>(null);

  const toggle = (id: string) =>
    setSelected((s) => ({ ...s, [id]: !s[id] }));

  const runBatch = async () => {
    const chosen = Object.entries(selected)
      .filter(([, v]) => v)
      .map(([k]) => k);
    if (chosen.length === 0) return;
    setRunning(true);
    setOutcomes([]);
    setBlocked(null);
    try {
      // The backend is the source of truth for HITL gating, concurrency,
      // and budget caps. Do not fan out spec/run creation from the browser.
      const res = await api.batchRun(projectId, {
        idea_ids: chosen,
        worker: batchWorker,
      });
      // Normalise outcomes to the same shape the table renderer used before
      // (with a nested run object) so we don't rewrite the markup below.
      setOutcomes(
        res.outcomes.map((o) => ({
          idea_id: o.idea_id,
          run: o.run_id
            ? {
                id: o.run_id,
                status: o.run_status || "unknown",
                result_class: o.result_class ?? null,
                mock: false,
              }
            : undefined,
          error: o.error ?? undefined,
        }))
      );
    } catch (e) {
      const msg = (e as Error).message || String(e);
      // 409 / 402 bodies come back as a string "{status} {detail}" from
      // lib/api.ts's `request`. Detect them and render a dedicated banner
      // so the operator can see why the batch didn't start.
      if (msg.startsWith("409") && msg.includes("approval_required")) {
        setBlocked(
          "Batch blocked: human approval required. Open the Approvals tab to decide."
        );
      } else if (msg.startsWith("402") && msg.includes("budget_exceeded")) {
        setBlocked(
          "Batch blocked: project budget ceiling reached. Raise the ceiling in the brief or wind down."
        );
      } else {
        setBlocked(`Batch failed: ${msg}`);
      }
    } finally {
      setRunning(false);
    }
  };

  const selectedCount = Object.values(selected).filter(Boolean).length;
  const promotedIdeas =
    ideas.data?.filter((i) => i.stage !== "S0" || i.decision !== "reject") || [];

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>Batch (run several ideas in parallel)</h3>
        <span style={{ color: "#666", fontSize: 12 }}>
          backend-orchestrated · concurrency = RESEARCHOS_CONCURRENCY_PER_BATCH
        </span>
      </div>
      {blocked && (
        <div
          className="warning-banner"
          style={{ marginTop: 8, background: "#fdecea", borderLeft: "4px solid #c0392b" }}
        >
          {blocked}
        </div>
      )}
      <div className="row" style={{ marginTop: 8, alignItems: "flex-end" }}>
        <div className="field" style={{ minWidth: 220 }}>
          <label>Worker (applies to every selected idea)</label>
          <select
            value={batchWorker}
            onChange={(e) => setBatchWorker(e.target.value)}
          >
            <option value="two_step">Builder + Reviewer (recommended)</option>
            <option value="claude_code">Builder only (headless code worker)</option>
            <option value="codex">Reviewer only (headless review worker)</option>
          </select>
        </div>
        <div style={{ alignSelf: "end", paddingBottom: 12 }}>
          <button
            className="primary"
            disabled={selectedCount === 0 || running}
            onClick={runBatch}
          >
            {running
              ? `Running ${selectedCount}…`
              : `Batch-run ${selectedCount} selected idea(s)`}
          </button>
        </div>
      </div>
      {promotedIdeas.length === 0 ? (
        <div className="empty">No ideas to batch. Generate + promote first.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th style={{ width: 28 }}></th>
              <th>Stage</th>
              <th>Idea</th>
              <th>Decision</th>
              <th>Score</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {promotedIdeas.map((i) => {
              const outcome = outcomes.find((o) => o.idea_id === i.id);
              return (
                <tr key={i.id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={!!selected[i.id]}
                      onChange={() => toggle(i.id)}
                    />
                  </td>
                  <td>
                    <span className="badge">{i.stage}</span>
                  </td>
                  <td>
                    <div style={{ fontSize: 13 }}>{i.title}</div>
                    <div className="mono" style={{ fontSize: 10, color: "#888" }}>
                      {i.id}
                    </div>
                  </td>
                  <td>
                    <StatusBadge value={i.decision} />
                  </td>
                  <td>{i.score?.toFixed(2) ?? "-"}</td>
                  <td>
                    {outcome ? (
                      outcome.run ? (
                        <>
                          <StatusBadge
                            value={outcome.run.status}
                            mock={outcome.run.mock}
                          />{" "}
                          <StatusBadge value={outcome.run.result_class || undefined} />
                        </>
                      ) : (
                        <span className="badge bad">{outcome.error}</span>
                      )
                    ) : (
                      <span style={{ color: "#888", fontSize: 11 }}>—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
