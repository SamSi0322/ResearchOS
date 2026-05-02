import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StatusBadge from "../../app/components/StatusBadge";
import Toast from "../../app/components/Toast";

export default function DraftsTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const manuscripts = useQuery({
    queryKey: ["manuscripts", projectId],
    queryFn: () => api.listManuscripts(projectId),
  });
  const claims = useQuery({
    queryKey: ["claims", projectId],
    queryFn: () => api.listClaims(projectId),
  });
  const [title, setTitle] = useState("");
  const [venue, setVenue] = useState("");
  const [toast, setToast] = useState<string | null>(null);

  const gen = useMutation({
    mutationFn: () =>
      api.generateDraft(projectId, {
        manuscript_title: title || undefined,
        target_venue: venue || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["manuscripts", projectId] });
      qc.invalidateQueries({ queryKey: ["claims", projectId] });
      setToast("Draft generated");
    },
    onError: (e: Error) => setToast(e.message),
  });

  return (
    <div>
      <div className="card">
        <h3>Generate research report &amp; structured draft</h3>
        <div className="row">
          <div className="field" style={{ flex: 1, minWidth: 220 }}>
            <label>Title</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Leave blank to use project title"
            />
          </div>
          <div className="field" style={{ flex: 1, minWidth: 200 }}>
            <label>Target venue</label>
            <input
              type="text"
              value={venue}
              onChange={(e) => setVenue(e.target.value)}
            />
          </div>
          <div style={{ alignSelf: "end", paddingBottom: 12 }}>
            <button
              className="primary"
              title="Drafts require human validation before submission."
              onClick={() => gen.mutate()}
              disabled={gen.isPending}
            >
              {gen.isPending
                ? "Generating…"
                : "Generate research report & structured draft"}
            </button>
          </div>
        </div>
        <div className="help" style={{ fontSize: 12, color: "#666" }}>
          Evidence-first: every numeric claim in the draft must reference one of
          the {claims.data?.length ?? 0} existing Claim rows. Runs and claims
          tagged MOCK produce a MOCK-tagged draft.{" "}
          <strong>
            Drafts require human validation before submission — this system
            produces research decision support, not auto-submitted papers.
          </strong>
        </div>
      </div>

      <QualityCard projectId={projectId} />

      {manuscripts.data?.map((m) => (
        <div key={m.id} className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h3 style={{ marginBottom: 4 }}>{m.title}</h3>
              <div style={{ color: "#666", fontSize: 12 }}>
                target: {m.target_venue || "-"}
              </div>
            </div>
            <div className="row">
              <a
                href={api.downloadLatestManuscriptPdf(projectId)}
                target="_blank"
                rel="noreferrer"
              >
                <button className="small primary">⤓ Latest PDF</button>
              </a>
              <StatusBadge value={m.status} />
            </div>
          </div>

          {m.drafts.map((d) => (
            <div key={d.id} className="card" style={{ marginTop: 12 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h4 style={{ margin: 0 }}>
                  Draft v{d.version}{" "}
                  <StatusBadge value={d.status} mock={d.mock} />
                </h4>
                <div style={{ color: "#666", fontSize: 12 }}>
                  {d.claim_ids.length} claim refs
                </div>
              </div>
              {d.notes && <div className="warning-banner">{d.notes}</div>}
              {d.sections.map((s) => (
                <div key={s.id} style={{ marginTop: 10 }}>
                  <h5 style={{ margin: "6px 0" }}>{s.title}</h5>
                  <div style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
                    {s.content}
                  </div>
                  {s.claim_refs.length > 0 && (
                    <div
                      className="mono"
                      style={{ fontSize: 11, color: "#888", marginTop: 4 }}
                    >
                      claims: {s.claim_refs.join(", ")}
                    </div>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}

      <div className="card">
        <h3>All claims</h3>
        {claims.data && claims.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Claim</th>
                <th>Kind</th>
                <th>Value</th>
                <th>Mock</th>
              </tr>
            </thead>
            <tbody>
              {claims.data.map((c) => (
                <tr key={c.id}>
                  <td>
                    <div style={{ fontSize: 13 }}>{c.text}</div>
                    <div className="mono" style={{ fontSize: 10, color: "#888" }}>
                      {c.id}
                    </div>
                  </td>
                  <td>{c.kind}</td>
                  <td>{c.value || "-"}</td>
                  <td>
                    {c.mock ? <span className="badge mock">MOCK</span> : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">No claims yet — analyze a run first.</div>
        )}
      </div>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}

function QualityCard({ projectId }: { projectId: string }) {
  const q = useQuery({
    queryKey: ["quality", projectId],
    queryFn: () => api.latestQuality(projectId),
  });
  const r = useQuery({
    queryKey: ["readiness", projectId],
    queryFn: () => api.readiness(projectId),
  });
  if (q.isLoading && r.isLoading) {
    return <div className="card">Loading quality diagnostics…</div>;
  }
  const hasQuality =
    q.data && Object.keys(q.data || {}).length > 0;
  const tier = (r.data?.tier || "no_draft") as string;
  const reasons = r.data?.reasons || [];
  const quality = (q.data || {}) as Record<string, unknown>;
  const pct = (v: unknown) =>
    typeof v === "number"
      ? `${(v * 100).toFixed(0)}%`
      : "-";
  return (
    <div className="card" style={{ borderLeft: "4px solid var(--accent)" }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>Draft quality</h3>
        <span
          className={`badge ${
            tier === "mentor_signoff_required"
              ? "good"
              : tier === "ready_for_mentor_review"
              ? "accent"
              : tier === "needs_revision"
              ? "warn"
              : tier === "no_draft"
              ? ""
              : "mock"
          }`}
        >
          readiness: {tier.replace(/_/g, " ")}
        </span>
      </div>
      {!hasQuality ? (
        <div className="empty">No draft generated yet.</div>
      ) : (
        <>
          <div className="grid four" style={{ marginTop: 10 }}>
            <MiniStat
              label="Completeness"
              value={pct(quality.draft_completeness_score)}
            />
            <MiniStat
              label="Evidence coverage"
              value={pct(quality.evidence_coverage_ratio)}
            />
            <MiniStat
              label="Placeholders"
              value={String(quality.placeholder_count ?? "-")}
            />
            <MiniStat
              label="Unsupported refs"
              value={String(quality.unsupported_claim_reference_count ?? "-")}
            />
          </div>
          {Array.isArray((quality as { notes?: string[] }).notes) &&
            ((quality as { notes?: string[] }).notes!.length ?? 0) > 0 && (
              <ul style={{ marginTop: 10, fontSize: 12, color: "#555" }}>
                {(quality as { notes?: string[] }).notes!.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            )}
        </>
      )}
      {reasons.length > 0 && (
        <div
          style={{
            marginTop: 10,
            fontSize: 12,
            color: "#555",
            borderTop: "1px solid var(--border)",
            paddingTop: 8,
          }}
        >
          <strong>Why this tier:</strong>
          <ul style={{ margin: "6px 0 0 16px" }}>
            {reasons.map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card" style={{ margin: 0 }}>
      <div style={{ fontSize: 11, color: "#666", textTransform: "uppercase" }}>
        {label}
      </div>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{value}</div>
    </div>
  );
}
