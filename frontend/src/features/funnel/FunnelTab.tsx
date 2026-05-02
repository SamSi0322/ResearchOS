import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import Toast from "../../app/components/Toast";

const STAGES = ["S0", "S1", "S2", "S3", "S4"];

export default function FunnelTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const summary = useQuery({
    queryKey: ["funnel", projectId],
    queryFn: () => api.funnelSummary(projectId),
  });
  const [from, setFrom] = useState("S0");
  const [to, setTo] = useState("S1");
  const [keep, setKeep] = useState<number | "">(20);
  const [toast, setToast] = useState<string | null>(null);

  const advanceMut = useMutation({
    mutationFn: () =>
      api.advanceFunnel(projectId, from, to, typeof keep === "number" ? keep : undefined),
    onSuccess: (res) => {
      qc.invalidateQueries();
      setToast(
        `Promoted ${res.promoted.length} → ${res.to_stage}, rejected ${res.rejected.length}`
      );
    },
    onError: (e: Error) => setToast(e.message),
  });

  return (
    <div>
      <div className="card">
        <h3>Stage counts</h3>
        <div className="grid five" style={{ gridTemplateColumns: "repeat(5, 1fr)" }}>
          {STAGES.map((s) => {
            const count = summary.data?.by_stage?.[s] ?? 0;
            const target = summary.data?.targets?.[s] ?? 0;
            return (
              <div key={s} className="card" style={{ marginBottom: 0 }}>
                <div style={{ color: "#666", fontSize: 12 }}>Stage</div>
                <div style={{ fontSize: 20, fontWeight: 600 }}>{s}</div>
                <div style={{ marginTop: 6 }}>
                  <span style={{ fontSize: 24 }}>{count}</span>
                  <span style={{ color: "#999" }}> / target {target}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <h3>Advance stage</h3>
        <div className="row">
          <div className="field" style={{ minWidth: 120 }}>
            <label>From</label>
            <select value={from} onChange={(e) => setFrom(e.target.value)}>
              {STAGES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
          <div className="field" style={{ minWidth: 120 }}>
            <label>To</label>
            <select value={to} onChange={(e) => setTo(e.target.value)}>
              {STAGES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
          <div className="field" style={{ minWidth: 120 }}>
            <label>Keep top N</label>
            <input
              type="number"
              value={keep}
              onChange={(e) => setKeep(e.target.value === "" ? "" : Number(e.target.value))}
            />
          </div>
          <div style={{ alignSelf: "end", paddingBottom: 12 }}>
            <button
              className="primary"
              onClick={() => advanceMut.mutate()}
              disabled={advanceMut.isPending}
            >
              {advanceMut.isPending ? "Advancing…" : "Advance"}
            </button>
          </div>
        </div>
      </div>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
