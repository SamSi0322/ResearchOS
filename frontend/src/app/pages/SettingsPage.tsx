import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ProviderCredentialInput,
  ProviderValidationLog,
  ProviderValidationResult,
  ValidationCategory,
} from "../../lib/api";
import Modal from "../components/Modal";
import Toast from "../components/Toast";

// Translate a structured validation result into a short, unambiguous UI
// message. The backend already chooses a ``category`` and a safe upstream
// message; we decide the heading the operator sees so "200 but error" can
// never happen again.
function headlineFor(category: string): string {
  switch (category) {
    case "ok":
      return "Credential valid";
    case "auth_error":
      return "Credential invalid";
    case "model_error":
      return "Configured but selected model unavailable";
    case "network_error":
      return "Network / provider unreachable";
    case "config_error":
      return "Configuration incomplete";
    case "provider_error":
    default:
      return "Provider error";
  }
}

function validationHeadline(
  res: { category: string }
): string {
  return headlineFor(res.category);
}

function validationToastKind(
  category: ValidationCategory
): "good" | "bad" | "warn" {
  if (category === "ok") return "good";
  if (category === "model_error" || category === "config_error") return "warn";
  return "bad";
}

function formatValidation(res: ProviderValidationResult): string {
  const parts: string[] = [validationHeadline(res) + "."];
  if (res.category === "ok") {
    parts.push(`${res.provider}/${res.actual_model ?? res.requested_model ?? "?"} (${res.latency_ms}ms)`);
  } else if (res.category === "model_error") {
    const shown = res.actual_model ?? res.requested_model ?? "?";
    parts.push(`Model '${shown}' is not available on ${res.provider}.`);
    if (res.http_status) parts.push(`(http ${res.http_status})`);
    parts.push("The key itself is fine — switch the test model or the stored default.");
  } else if (res.category === "auth_error") {
    parts.push(`${res.provider} rejected the key.`);
    if (res.http_status) parts.push(`(http ${res.http_status})`);
  } else if (res.category === "network_error") {
    parts.push(res.message);
  } else if (res.category === "config_error") {
    parts.push(res.message);
  } else {
    parts.push(res.message);
    if (res.http_status) parts.push(`(http ${res.http_status})`);
  }
  return parts.join(" ");
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const creds = useQuery({
    queryKey: ["providers"],
    queryFn: api.listProviders,
  });
  const [showModal, setShowModal] = useState(false);
  const [toast, setToast] = useState<{
    m: string;
    k: "good" | "bad" | "warn";
  } | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  // Persistent validation history, loaded from the backend so a refresh does
  // not wipe the operator's last-validation cell. Refetched on every test.
  const history = useQuery({
    queryKey: ["providers-validation-history"],
    queryFn: () => api.validationHistory({ limit: 100 }),
    refetchInterval: 30_000,
  });
  const latestByCredential = ((): Record<string, ProviderValidationLog> => {
    const out: Record<string, ProviderValidationLog> = {};
    for (const row of history.data || []) {
      if (!row.credential_id) continue;
      if (!out[row.credential_id]) out[row.credential_id] = row;
    }
    return out;
  })();

  const addMut = useMutation({
    mutationFn: (body: ProviderCredentialInput) => api.addProvider(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["providers"] });
      setShowModal(false);
      setToast({ m: "Credential added", k: "good" });
    },
    onError: (e: Error) => setToast({ m: e.message, k: "bad" }),
  });

  const delMut = useMutation({
    mutationFn: (id: string) => api.deleteProvider(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["providers"] });
      setToast({ m: "Credential deleted", k: "good" });
    },
  });

  const runTest = async (id: string) => {
    setTesting(id);
    try {
      const res = await api.testProvider(id);
      // The backend persisted this result — refetch the history so every tab
      // / reload sees it.
      qc.invalidateQueries({ queryKey: ["providers-validation-history"] });
      setToast({
        m: formatValidation(res),
        k: validationToastKind(res.category),
      });
    } catch (e) {
      setToast({ m: (e as Error).message, k: "bad" });
    } finally {
      setTesting(null);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h2>Providers & Settings</h2>
        <div className="actions">
          <button className="primary" onClick={() => setShowModal(true)}>
            + Add Provider Credential
          </button>
        </div>
      </div>

      <div className="warning-banner">
        API keys are submitted directly to the backend over localhost and
        encrypted at rest under <span className="mono">var/secrets/</span>. The
        frontend never persists them.
      </div>

      <div className="card" style={{ background: "#f6f7fb" }}>
        <strong>Runtime is headless.</strong>{" "}
        Every provider call goes through HTTP adapters in{" "}
        <span className="mono">app/providers/</span>. The worker options below
        (<em>builder</em> and <em>reviewer</em>) are logical roles — they do{" "}
        <strong>not</strong> open a Claude Code or Codex terminal, and the
        pipeline does not require an interactive session to be running anywhere.
        <span style={{ marginLeft: 8 }} className="badge">
          execution_mode: headless_api
        </span>
      </div>

      <SmokePanel onToast={setToast} />

      <div className="card">
        <h3>Stored credentials</h3>
        <div style={{ fontSize: 12, color: "#666", marginBottom: 8 }}>
          <strong>Test</strong> validates the stored key against a dedicated
          cheap test model (<span className="mono">gpt-4.1-mini</span> for
          OpenAI, <span className="mono">claude-sonnet-4-6</span> for
          Anthropic). That is independent of the production policy, so
          validation stays meaningful even when a policy model id is not yet
          live. Use <strong>Smoke mode &rarr; Ping</strong> below to exercise
          the actual runtime policy path.
        </div>
        {creds.data && creds.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Label</th>
                <th>Masked</th>
                <th>Default model</th>
                <th>Default for</th>
                <th>Default</th>
                <th>Last validation</th>
                <th style={{ width: 180 }}></th>
              </tr>
            </thead>
            <tbody>
              {creds.data.map((c) => {
                const last = latestByCredential[c.id];
                return (
                  <tr key={c.id}>
                    <td>
                      <span className="badge">{c.provider}</span>
                    </td>
                    <td>{c.label}</td>
                    <td className="mono">{c.masked_preview}</td>
                    <td>{c.default_model || "-"}</td>
                    <td>
                      {c.default_for?.length ? c.default_for.join(", ") : "-"}
                    </td>
                    <td>{c.is_default ? "✓" : ""}</td>
                    <td>
                      {last ? (
                        <div style={{ maxWidth: 260 }}>
                          <span
                            className={`badge ${
                              last.category === "ok"
                                ? "good"
                                : last.category === "auth_error" ||
                                  last.category === "network_error" ||
                                  last.category === "provider_error"
                                ? "bad"
                                : ""
                            }`}
                          >
                            {validationHeadline(last)}
                          </span>
                          <div
                            className="mono"
                            style={{ fontSize: 10, color: "#666", marginTop: 2 }}
                          >
                            {last.requested_model
                              ? `req ${last.requested_model}`
                              : null}
                            {last.actual_model &&
                            last.actual_model !== last.requested_model
                              ? ` / sent ${last.actual_model}`
                              : null}
                            {last.http_status ? ` · http ${last.http_status}` : null}
                            {` · ${last.latency_ms}ms`}
                          </div>
                        </div>
                      ) : (
                        <span style={{ color: "#888", fontSize: 11 }}>—</span>
                      )}
                    </td>
                    <td>
                      <button
                        className="small"
                        disabled={testing === c.id}
                        onClick={() => runTest(c.id)}
                      >
                        {testing === c.id ? "…" : "Test"}
                      </button>{" "}
                      <button
                        className="small danger"
                        onClick={() => {
                          if (confirm(`Delete ${c.provider} credential?`))
                            delMut.mutate(c.id);
                        }}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="empty">
            No credentials yet. Add one to enable real provider calls. (Mock mode
            also works without a key.)
          </div>
        )}
      </div>

      {showModal && (
        <AddCredentialModal
          onClose={() => setShowModal(false)}
          onSubmit={(body) => addMut.mutate(body)}
          submitting={addMut.isPending}
        />
      )}

      <Toast
        message={toast?.m || null}
        kind={toast?.k}
        onClose={() => setToast(null)}
      />
    </div>
  );
}

function AddCredentialModal({
  onClose,
  onSubmit,
  submitting,
}: {
  onClose: () => void;
  onSubmit: (body: ProviderCredentialInput) => void;
  submitting: boolean;
}) {
  const [form, setForm] = useState<ProviderCredentialInput>({
    provider: "mock",
    label: "default",
    api_key: "",
    default_model: "",
    default_for: [],
    is_default: true,
    base_url: "",
    notes: "",
  });

  const update = <K extends keyof ProviderCredentialInput>(
    k: K,
    v: ProviderCredentialInput[K]
  ) => setForm((s) => ({ ...s, [k]: v }));

  const canSubmit = form.api_key.trim().length >= 3;

  return (
    <Modal
      title="Add Provider Credential"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose} disabled={submitting}>Cancel</button>
          <button
            className="primary"
            disabled={!canSubmit || submitting}
            onClick={() => {
              // Submit then immediately wipe the form - avoid re-render retention
              const body = { ...form };
              setForm((s) => ({ ...s, api_key: "" }));
              onSubmit(body);
            }}
          >
            {submitting ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <div className="warning-banner">
        Your key is sent straight to the backend and cleared from this form on
        success. It is never stored in browser storage.
      </div>

      <div className="grid two">
        <div className="field">
          <label>Provider</label>
          <select
            value={form.provider}
            onChange={(e) => update("provider", e.target.value as any)}
          >
            <option value="mock">mock</option>
            <option value="openai">openai</option>
            <option value="anthropic">anthropic</option>
          </select>
        </div>
        <div className="field">
          <label>Label</label>
          <input
            type="text"
            value={form.label}
            onChange={(e) => update("label", e.target.value)}
          />
        </div>
        <div className="field" style={{ gridColumn: "1/-1" }}>
          <label>API key {form.provider === "mock" && "(any non-empty string)"}</label>
          <input
            type="password"
            autoComplete="off"
            value={form.api_key}
            onChange={(e) => update("api_key", e.target.value)}
            placeholder={
              form.provider === "openai"
                ? "sk-..."
                : form.provider === "anthropic"
                ? "sk-ant-..."
                : "mock"
            }
          />
          <span className="help">
            Posted to <span className="mono">/api/providers</span>, encrypted
            on the backend.
          </span>
        </div>
        <div className="field">
          <label>Default model (optional)</label>
          <input
            type="text"
            value={form.default_model || ""}
            onChange={(e) => update("default_model", e.target.value)}
            placeholder={
              form.provider === "openai"
                ? "gpt-4.1-mini"
                : form.provider === "anthropic"
                ? "claude-sonnet-4-6"
                : "mock-1"
            }
          />
        </div>
        <div className="field">
          <label>Is default</label>
          <select
            value={form.is_default ? "yes" : "no"}
            onChange={(e) => update("is_default", e.target.value === "yes")}
          >
            <option value="yes">yes</option>
            <option value="no">no</option>
          </select>
        </div>
        <div className="field" style={{ gridColumn: "1/-1" }}>
          <label>Base URL override (optional)</label>
          <input
            type="text"
            value={form.base_url || ""}
            onChange={(e) => update("base_url", e.target.value)}
            placeholder="https://api.openai.com"
          />
        </div>
      </div>
    </Modal>
  );
}

function SmokePanel({
  onToast,
}: {
  onToast: (t: { m: string; k: "good" | "bad" | "warn" }) => void;
}) {
  const qc = useQueryClient();
  const health = useQuery({
    queryKey: ["smoke-health"],
    queryFn: api.smokeHealth,
    refetchInterval: 30_000,
  });
  const [pinging, setPinging] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<null | Awaited<ReturnType<typeof api.smokeRun>>>(null);
  const [lastPing, setLastPing] = useState<
    Record<string, ProviderValidationResult>
  >({});

  const ping = async (provider: "openai" | "anthropic") => {
    setPinging(provider);
    try {
      const res = await api.smokePing(provider);
      setLastPing((m) => ({ ...m, [provider]: res }));
      onToast({
        m: formatValidation(res),
        k: validationToastKind(res.category),
      });
    } catch (e) {
      onToast({ m: (e as Error).message, k: "bad" });
    } finally {
      setPinging(null);
    }
  };

  const smoke = async () => {
    setRunning(true);
    try {
      const res = await api.smokeRun({ idea_count: 2, worker: "two_step" });
      setLastRun(res);
      qc.invalidateQueries({ queryKey: ["packages"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      onToast({
        m: `smoke: ${res.batch.length} ideas, pkg=${res.package_id || "-"}`,
        k: "good",
      });
    } catch (e) {
      onToast({ m: (e as Error).message, k: "bad" });
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="card">
      <h3>Smoke mode</h3>
      <div style={{ fontSize: 12, color: "#666", marginBottom: 8 }}>
        <strong>Ping</strong> exercises the <em>runtime policy path</em> — same
        adapter, same model id (after alias), same reasoning effort a real run
        would use. Use this to answer "will the current runtime route work?".
        For "does this key itself work?" use the <strong>Test</strong> button
        on the credential above.
      </div>
      <div className="row" style={{ gap: 18, flexWrap: "wrap" }}>
        <div>
          <div style={{ fontSize: 12, color: "#666" }}>Run mode</div>
          <div>
            <span className="badge">
              {health.data?.run_mode || "?"}
            </span>
          </div>
        </div>
        <div>
          <div style={{ fontSize: 12, color: "#666" }}>Smoke mode</div>
          <div>
            <span
              className={`badge ${health.data?.smoke_mode ? "accent" : ""}`}
            >
              {health.data?.smoke_mode ? "ON" : "off"}
            </span>
          </div>
        </div>
        <div>
          <div style={{ fontSize: 12, color: "#666" }}>Execution</div>
          <div>
            <span className="badge">
              {health.data?.execution_mode || "headless_api"}
            </span>
          </div>
        </div>
        <div>
          <div style={{ fontSize: 12, color: "#666" }}>OpenAI</div>
          <div>
            <span
              className={`badge ${health.data?.openai_ready ? "good" : "bad"}`}
            >
              {health.data?.openai_ready ? "configured" : "missing"}
            </span>
          </div>
        </div>
        <div>
          <div style={{ fontSize: 12, color: "#666" }}>Anthropic</div>
          <div>
            <span
              className={`badge ${health.data?.anthropic_ready ? "good" : "bad"}`}
            >
              {health.data?.anthropic_ready ? "configured" : "missing"}
            </span>
          </div>
        </div>
        {health.data?.settings_snapshot && (
          <div style={{ fontSize: 12, color: "#666", maxWidth: 520 }}>
            ideas/run: {String(health.data.settings_snapshot.max_ideas_per_run)} ·
            concurrency: {String(health.data.settings_snapshot.concurrency_per_batch)} ·
            max tokens: {String(health.data.settings_snapshot.smoke_max_tokens)} ·
            openai: {String(health.data.settings_snapshot.openai_smoke_model)} ·
            anthropic: {String(health.data.settings_snapshot.anthropic_smoke_model)}
          </div>
        )}
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button
          className="small"
          disabled={!health.data?.openai_ready || pinging === "openai"}
          onClick={() => ping("openai")}
        >
          Ping OpenAI
        </button>
        <button
          className="small"
          disabled={!health.data?.anthropic_ready || pinging === "anthropic"}
          onClick={() => ping("anthropic")}
        >
          Ping Anthropic
        </button>
        <button
          className="primary"
          disabled={running || !(health.data?.openai_ready && health.data?.anthropic_ready)}
          onClick={smoke}
        >
          {running ? "Running smoke…" : "Run 2-idea smoke"}
        </button>
        <button
          className="small ghost"
          onClick={() => health.refetch()}
        >
          refresh
        </button>
      </div>
      {Object.keys(lastPing).length > 0 && (
        <div style={{ marginTop: 12, fontSize: 12 }}>
          <strong>Last ping (runtime path)</strong>
          <ul style={{ margin: "6px 0 0 16px", listStyle: "none", padding: 0 }}>
            {Object.entries(lastPing).map(([p, r]) => (
              <li key={p} style={{ marginBottom: 4 }}>
                <span
                  className={`badge ${
                    r.category === "ok"
                      ? "good"
                      : r.category === "auth_error" ||
                        r.category === "network_error" ||
                        r.category === "provider_error"
                      ? "bad"
                      : ""
                  }`}
                >
                  {p}: {validationHeadline(r)}
                </span>{" "}
                <span className="mono" style={{ fontSize: 11, color: "#666" }}>
                  req {r.requested_model ?? "?"}
                  {r.actual_model && r.actual_model !== r.requested_model
                    ? ` / sent ${r.actual_model}`
                    : ""}
                  {r.http_status ? ` · http ${r.http_status}` : ""}
                  {` · ${r.latency_ms}ms`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {lastRun && (
        <div style={{ marginTop: 12, fontSize: 12 }}>
          <div>
            <strong>Last smoke result</strong> — project{" "}
            <span className="mono">{lastRun.project_id}</span>
            {lastRun.package_id && (
              <>
                {" · "}
                package <span className="mono">{lastRun.package_id}</span>
              </>
            )}
          </div>
          <ul style={{ margin: "6px 0 0 16px" }}>
            {lastRun.messages.map((m, i) => (
              <li key={i}>{m}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
