import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";

export default function AuditTab({ projectId }: { projectId: string }) {
  const audit = useQuery({
    queryKey: ["audit", projectId],
    queryFn: () => api.listAudit(projectId),
  });
  return (
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
        <div className="empty">No audit events for this project yet.</div>
      )}
    </div>
  );
}
