import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, Idea } from "../../lib/api";
import StatusBadge from "../../app/components/StatusBadge";
import Toast from "../../app/components/Toast";

const STAGES = ["S0", "S1", "S2", "S3", "S4"];

export default function IdeasTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<string>("");
  const [count, setCount] = useState(12);
  const [toast, setToast] = useState<string | null>(null);

  const ideas = useQuery({
    queryKey: ["ideas", projectId, filter],
    queryFn: () => api.listIdeas(projectId, filter || undefined),
  });

  const genMut = useMutation({
    mutationFn: () => api.generateIdeas(projectId, count),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["ideas", projectId] });
      setToast(`Generated ${res.length} ideas`);
    },
    onError: (e: Error) => setToast(e.message),
  });

  const scoreMut = useMutation({
    mutationFn: () => api.scoreIdeas(projectId, filter || "S0"),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["ideas", projectId] });
      setToast(`Scored ${(res as unknown[]).length} ideas`);
    },
    onError: (e: Error) => setToast(e.message),
  });

  const decide = useMutation({
    mutationFn: (args: { idea_id: string; decision: string; rationale?: string }) =>
      api.ideaDecision(projectId, args.idea_id, args.decision, args.rationale),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ideas", projectId] }),
  });

  const grouped = useMemo(() => {
    const by: Record<string, Idea[]> = {};
    for (const i of ideas.data || []) {
      by[i.stage] ||= [];
      by[i.stage].push(i);
    }
    return by;
  }, [ideas.data]);

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <div className="row">
          <label className="mono" style={{ fontSize: 12 }}>Stage</label>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">all</option>
            {STAGES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
        <div className="row">
          <label className="mono" style={{ fontSize: 12 }}>Count</label>
          <input
            type="number"
            style={{ width: 80 }}
            value={count}
            onChange={(e) => setCount(Math.max(1, Number(e.target.value) || 1))}
          />
          <button
            className="primary"
            onClick={() => genMut.mutate()}
            disabled={genMut.isPending}
          >
            {genMut.isPending ? "Generating…" : "Generate ideas"}
          </button>
          <button
            onClick={() => scoreMut.mutate()}
            disabled={scoreMut.isPending || !(ideas.data?.length ?? 0)}
          >
            {scoreMut.isPending ? "Scoring…" : `Score ${filter || "S0"} ideas`}
          </button>
        </div>
      </div>

      {STAGES.filter((s) => (filter ? s === filter : true)).map((s) => (
        <div key={s} className="card">
          <h3>
            {s}{" "}
            <span style={{ color: "#888", fontWeight: 400 }}>
              ({grouped[s]?.length || 0})
            </span>
          </h3>
          {(grouped[s]?.length ?? 0) > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Cluster</th>
                  <th>Decision</th>
                  <th>Score</th>
                  <th style={{ width: 220 }}></th>
                </tr>
              </thead>
              <tbody>
                {grouped[s]?.map((i) => (
                  <tr key={i.id}>
                    <td>
                      <div>{i.title}</div>
                      <div style={{ color: "#666", fontSize: 12 }}>{i.summary}</div>
                      <div className="mono" style={{ color: "#888", fontSize: 11 }}>
                        {i.id}
                      </div>
                    </td>
                    <td>{i.cluster_tag || "-"}</td>
                    <td>
                      <StatusBadge value={i.decision} />
                    </td>
                    <td>{i.score?.toFixed(2) ?? "-"}</td>
                    <td>
                      <Link to={`../specs?idea=${i.id}`}>
                        <button className="small">spec</button>
                      </Link>{" "}
                      <button
                        className="small"
                        onClick={() => decide.mutate({ idea_id: i.id, decision: "keep" })}
                      >
                        keep
                      </button>{" "}
                      <button
                        className="small"
                        onClick={() => decide.mutate({ idea_id: i.id, decision: "reject" })}
                      >
                        reject
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty">No ideas in {s}.</div>
          )}
        </div>
      ))}

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
