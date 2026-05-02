import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import Toast from "../../app/components/Toast";

export default function SpecsTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const [search] = useSearchParams();
  const preselect = search.get("idea") || "";
  const [ideaId, setIdeaId] = useState(preselect);
  const [toast, setToast] = useState<string | null>(null);
  const [extra, setExtra] = useState("");

  const ideas = useQuery({ queryKey: ["ideas", projectId], queryFn: () => api.listIdeas(projectId) });
  const specs = useQuery({ queryKey: ["specs", projectId], queryFn: () => api.listSpecs(projectId) });

  const genMut = useMutation({
    mutationFn: () => api.generateSpec(projectId, ideaId, extra || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["specs", projectId] });
      setToast("Spec generated");
    },
    onError: (e: Error) => setToast(e.message),
  });

  const byIdea = useMemo(() => {
    const m: Record<string, string> = {};
    for (const i of ideas.data || []) m[i.id] = i.title;
    return m;
  }, [ideas.data]);

  return (
    <div>
      <div className="card">
        <h3>Generate experiment spec</h3>
        <div className="row">
          <div className="field" style={{ minWidth: 280 }}>
            <label>Idea</label>
            <select value={ideaId} onChange={(e) => setIdeaId(e.target.value)}>
              <option value="">— pick an idea —</option>
              {ideas.data?.map((i) => (
                <option key={i.id} value={i.id}>
                  [{i.stage}] {i.title}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ flex: 1, minWidth: 280 }}>
            <label>Extra instructions (optional)</label>
            <input
              type="text"
              value={extra}
              onChange={(e) => setExtra(e.target.value)}
              placeholder="e.g. use MNIST, 2 seeds"
            />
          </div>
          <div style={{ alignSelf: "end", paddingBottom: 12 }}>
            <button
              className="primary"
              disabled={!ideaId || genMut.isPending}
              onClick={() => genMut.mutate()}
            >
              {genMut.isPending ? "Generating…" : "Generate spec"}
            </button>
          </div>
        </div>
      </div>

      {specs.data && specs.data.length > 0 ? (
        specs.data.map((s) => (
          <div className="card" key={s.id}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3>
                v{s.version} ·{" "}
                <span style={{ fontWeight: 400 }}>{byIdea[s.idea_id] || s.idea_id}</span>
              </h3>
              <div style={{ color: "#666", fontSize: 12 }} className="mono">
                {s.id}
              </div>
            </div>
            <dl className="kv">
              <dt>Hypothesis</dt>
              <dd>{s.hypothesis}</dd>
              <dt>Framing</dt>
              <dd>{s.problem_framing}</dd>
              <dt>Target metrics</dt>
              <dd>{s.target_metrics?.join(", ") || "-"}</dd>
              <dt>Baseline</dt>
              <dd>{s.baseline}</dd>
              <dt>Plan</dt>
              <dd>{s.experiment_plan}</dd>
              <dt>Constraints</dt>
              <dd>{s.constraints || "-"}</dd>
              <dt>Success criteria</dt>
              <dd>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {s.success_criteria?.map((c, i) => <li key={i}>{c}</li>)}
                </ul>
              </dd>
              <dt>Stop criteria</dt>
              <dd>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {s.stop_criteria?.map((c, i) => <li key={i}>{c}</li>)}
                </ul>
              </dd>
              <dt>Dataset</dt>
              <dd>{s.dataset_assumptions}</dd>
            </dl>
          </div>
        ))
      ) : (
        <div className="empty">No specs yet.</div>
      )}

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
