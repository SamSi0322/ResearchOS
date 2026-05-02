import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";

export default function AuditPage() {
  const [projectId, setProjectId] = useState<string>("");
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });
  const audit = useQuery({
    queryKey: ["audit", projectId],
    queryFn: () => api.listAudit(projectId || undefined),
  });

  return (
    <div>
      <div className="page-header">
        <h2>Audit timeline</h2>
        <div className="actions">
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">All projects</option>
            {projects.data?.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="card">
        {audit.data && audit.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>Actor</th>
                <th>Subject</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {audit.data.map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.created_at.slice(0, 19)}</td>
                  <td>
                    <span className="badge">{a.kind}</span>
                  </td>
                  <td>{a.actor}</td>
                  <td className="mono">
                    {a.subject_kind}
                    {a.subject_id ? `:${a.subject_id.slice(0, 8)}…` : ""}
                  </td>
                  <td>{a.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">No audit events yet.</div>
        )}
      </div>
    </div>
  );
}
