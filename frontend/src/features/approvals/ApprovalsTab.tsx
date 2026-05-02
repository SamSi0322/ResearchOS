import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApprovalRequest } from "../../lib/api";
import StatusBadge from "../../app/components/StatusBadge";
import Toast from "../../app/components/Toast";

const TIER_CLASS: Record<string, string> = {
  approved: "good",
  pending: "accent",
  rejected: "bad",
  clarification_requested: "warn",
  expired: "",
  canceled: "",
};

export default function ApprovalsTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const approvals = useQuery({
    queryKey: ["approvals", projectId],
    queryFn: () => api.listProjectApprovals(projectId),
    refetchInterval: 10_000,
  });
  const [toast, setToast] = useState<{ m: string; k: "good" | "bad" } | null>(
    null
  );
  const [noteFor, setNoteFor] = useState<Record<string, string>>({});

  const decide = useMutation({
    mutationFn: (args: { id: string; decision: string; note?: string }) =>
      api.decideApproval(args.id, { decision: args.decision, note: args.note }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["approvals", projectId] });
      setToast({ m: `${res.stage_key} -> ${res.decision}`, k: "good" });
    },
    onError: (e: Error) => setToast({ m: e.message, k: "bad" }),
  });

  const remind = useMutation({
    mutationFn: api.scanReminders,
    onSuccess: (res) =>
      setToast({
        m: `Reminders sent: ${res.count}`,
        k: res.count > 0 ? "good" : "bad",
      }),
  });
  const expire = useMutation({
    mutationFn: api.scanExpirations,
    onSuccess: (res) =>
      setToast({
        m: `Expired: ${res.count}`,
        k: res.count > 0 ? "good" : "bad",
      }),
  });

  return (
    <div>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>Approvals</h3>
          <div className="row">
            <button
              className="small"
              disabled={remind.isPending}
              onClick={() => remind.mutate()}
            >
              scan reminders
            </button>
            <button
              className="small"
              disabled={expire.isPending}
              onClick={() => expire.mutate()}
            >
              scan expirations
            </button>
          </div>
        </div>
        <div style={{ fontSize: 12, color: "#666", marginTop: 6 }}>
          Approvals appear when the pipeline reaches an enabled gate. Clicking
          Approve / Reject / Request changes resumes or stops the workflow
          accordingly. Confirmation emails land in your SMTP relay if
          configured, otherwise in <span className="mono">var/outbox/</span>.
        </div>
      </div>

      <div className="card">
        {approvals.data && approvals.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Gate</th>
                <th>Status</th>
                <th>Approver</th>
                <th>Deadline</th>
                <th>Reminders</th>
                <th>Context</th>
                <th style={{ width: 320 }}></th>
              </tr>
            </thead>
            <tbody>
              {approvals.data.map((a: ApprovalRequest) => (
                <tr key={a.id}>
                  <td>
                    <span className="badge accent">{a.stage_key}</span>
                  </td>
                  <td>
                    <span className={`badge ${TIER_CLASS[a.status] ?? ""}`}>
                      {a.status}
                    </span>
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {a.approver_email}
                    {a.cc_emails?.length ? (
                      <div style={{ color: "#888" }}>
                        cc: {a.cc_emails.join(", ")}
                      </div>
                    ) : null}
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {a.timeout_at.slice(0, 16).replace("T", " ")}
                  </td>
                  <td>{a.reminder_count}</td>
                  <td style={{ fontSize: 11 }}>
                    {a.decision_note ? (
                      <div>
                        <strong>decision:</strong> {a.decision} · {a.decision_note}
                      </div>
                    ) : null}
                    {Object.entries(a.context_snapshot || {}).slice(0, 4).map(([k, v]) => (
                      <div key={k}>
                        {k}: <span className="mono">{String(v).slice(0, 60)}</span>
                      </div>
                    ))}
                    {a.outbox_path && (
                      <div style={{ color: "#888" }}>
                        outbox: <span className="mono">{a.outbox_path.slice(-50)}</span>
                      </div>
                    )}
                  </td>
                  <td>
                    {a.status === "pending" ? (
                      <div className="stack">
                        <input
                          type="text"
                          placeholder="optional note"
                          value={noteFor[a.id] || ""}
                          onChange={(e) =>
                            setNoteFor({ ...noteFor, [a.id]: e.target.value })
                          }
                        />
                        <div className="row">
                          <button
                            className="small primary"
                            onClick={() =>
                              decide.mutate({
                                id: a.id,
                                decision: "approve",
                                note: noteFor[a.id],
                              })
                            }
                          >
                            approve
                          </button>
                          <button
                            className="small"
                            onClick={() =>
                              decide.mutate({
                                id: a.id,
                                decision: "request_changes",
                                note: noteFor[a.id],
                              })
                            }
                          >
                            request changes
                          </button>
                          <button
                            className="small danger"
                            onClick={() =>
                              decide.mutate({
                                id: a.id,
                                decision: "reject",
                                note: noteFor[a.id],
                              })
                            }
                          >
                            reject
                          </button>
                        </div>
                      </div>
                    ) : (
                      <span style={{ color: "#888", fontSize: 11 }}>
                        {a.resolved_at
                          ? `resolved ${a.resolved_at.slice(0, 16).replace("T", " ")}`
                          : "—"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">
            No approval requests yet. Approvals are created automatically when
            an enabled pipeline gate is reached.
          </div>
        )}
      </div>

      <Toast
        message={toast?.m || null}
        kind={toast?.k}
        onClose={() => setToast(null)}
      />
    </div>
  );
}
