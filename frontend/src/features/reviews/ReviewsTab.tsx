import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StatusBadge from "../../app/components/StatusBadge";
import Toast from "../../app/components/Toast";

export default function ReviewsTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const issues = useQuery({
    queryKey: ["issues", projectId],
    queryFn: () => api.listIssues(projectId),
  });
  const manuscripts = useQuery({
    queryKey: ["manuscripts", projectId],
    queryFn: () => api.listManuscripts(projectId),
  });
  const latestDraftId =
    manuscripts.data?.[0]?.drafts?.sort((a, b) => b.version - a.version)?.[0]?.id;
  const [toast, setToast] = useState<string | null>(null);

  const runMut = useMutation({
    mutationFn: () => api.runReviewers(projectId, latestDraftId || undefined),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["issues", projectId] });
      setToast(`${res.length} issues generated`);
    },
    onError: (e: Error) => setToast(e.message),
  });

  const updateMut = useMutation({
    mutationFn: (args: { id: string; state: string }) =>
      api.updateIssue(projectId, args.id, { state: args.state }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["issues", projectId] }),
  });

  return (
    <div>
      <div className="card">
        <h3>Run adversarial reviewers</h3>
        <div className="row">
          <button
            className="primary"
            onClick={() => runMut.mutate()}
            disabled={runMut.isPending}
          >
            {runMut.isPending ? "Reviewing…" : "Run all reviewers on latest draft"}
          </button>
          <div style={{ color: "#666", fontSize: 12 }}>
            latest draft: {latestDraftId ? <span className="mono">{latestDraftId}</span> : "—"}
          </div>
        </div>
      </div>

      <div className="card">
        <h3>Issues</h3>
        {issues.data && issues.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Reviewer</th>
                <th>Severity</th>
                <th>State</th>
                <th>Subject</th>
                <th>Description</th>
                <th style={{ width: 220 }}></th>
              </tr>
            </thead>
            <tbody>
              {issues.data.map((i) => (
                <tr key={i.id}>
                  <td>
                    <span className="badge">{i.reviewer_class}</span>
                  </td>
                  <td>
                    <span
                      className={`badge ${
                        i.severity === "P0" || i.severity === "P1"
                          ? "bad"
                          : i.severity === "P2"
                          ? "warn"
                          : ""
                      }`}
                    >
                      {i.severity}
                    </span>
                  </td>
                  <td>
                    <StatusBadge value={i.state} />
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {i.subject_kind}/{i.subject_id.slice(0, 8)}
                  </td>
                  <td>
                    <div>{i.description}</div>
                    {i.suggested_remediation && (
                      <div style={{ color: "#666", fontSize: 12, marginTop: 4 }}>
                        fix: {i.suggested_remediation}
                      </div>
                    )}
                  </td>
                  <td>
                    <button
                      className="small"
                      onClick={() => updateMut.mutate({ id: i.id, state: "resolved" })}
                    >
                      resolve
                    </button>{" "}
                    <button
                      className="small"
                      onClick={() => updateMut.mutate({ id: i.id, state: "waived" })}
                    >
                      waive
                    </button>{" "}
                    <button
                      className="small"
                      onClick={() => updateMut.mutate({ id: i.id, state: "reopened" })}
                    >
                      reopen
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">No review issues yet.</div>
        )}
      </div>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
