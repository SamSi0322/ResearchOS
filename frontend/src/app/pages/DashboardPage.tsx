import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StatusBadge from "../components/StatusBadge";

export default function DashboardPage() {
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api.listAudit() });
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.listProviders });
  const noProviders = providers.data && providers.data.length === 0;

  return (
    <div>
      <div className="page-header">
        <h2>Research console</h2>
        <div className="actions">
          <Link to="/projects/new">
            <button className="primary">+ New project</button>
          </Link>
        </div>
      </div>

      {noProviders && (
        <div className="warning-banner">
          No provider credentials configured yet. Open{" "}
          <Link to="/settings">Settings / Providers</Link> and add one —
          choose <strong>mock</strong> to exercise the pipeline without a real
          API key.
        </div>
      )}

      {projects.isLoading && <div className="empty">Loading projects…</div>}

      {projects.data && projects.data.length === 0 && (
        <div className="empty">
          No projects yet. Click <strong>+ New project</strong> to bootstrap one.
        </div>
      )}

      {projects.data && projects.data.length > 0 && (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Status</th>
                <th>Owner</th>
                <th>Reviewer</th>
                <th>Target venues</th>
              </tr>
            </thead>
            <tbody>
              {projects.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    <Link to={`/projects/${p.id}`}>{p.title}</Link>
                    <div className="mono" style={{ color: "#888" }}>
                      {p.id}
                    </div>
                  </td>
                  <td>
                    <StatusBadge value={p.status} />
                  </td>
                  <td>{p.student_name}</td>
                  <td>{p.mentor_name}</td>
                  <td>{p.target_venues?.join(", ") || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card">
        <h3>Recent activity</h3>
        {audit.data && audit.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>Actor</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {audit.data.slice(0, 15).map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.created_at.slice(0, 19)}</td>
                  <td>
                    <span className="badge">{a.kind}</span>
                  </td>
                  <td>{a.actor}</td>
                  <td>{a.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">Nothing logged yet.</div>
        )}
      </div>
    </div>
  );
}
