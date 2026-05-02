import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";

export default function BriefTab({ projectId }: { projectId: string }) {
  const p = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });
  const bundles = useQuery({
    queryKey: ["bundles", projectId],
    queryFn: () => api.listContextBundles(projectId),
  });
  if (p.isLoading) return <div>Loading…</div>;
  const project = p.data!;
  return (
    <div>
      <div className="grid two">
        <div className="card">
          <h3>Research brief</h3>
          <dl className="kv">
            <dt>Direction</dt>
            <dd>{project.research_direction}</dd>
            <dt>Constraints</dt>
            <dd>{project.constraints || "-"}</dd>
            <dt>Target venues</dt>
            <dd>{project.target_venues?.join(", ") || "-"}</dd>
            <dt>Exploration</dt>
            <dd>{project.exploration_strategy || "-"}</dd>
            <dt>Provider profile</dt>
            <dd>{project.provider_profile}</dd>
            <dt>Budget</dt>
            <dd>${project.brief?.budget_usd ?? "—"}</dd>
          </dl>
        </div>
        <div className="card">
          <h3>Project metadata</h3>
          <dl className="kv">
            <dt>Owner</dt>
            <dd>{project.student_name}</dd>
            <dt>Owner ref</dt>
            <dd className="mono">{project.student_ref || "-"}</dd>
            <dt>Reviewer</dt>
            <dd>{project.mentor_name}</dd>
            <dt>Advisor</dt>
            <dd>{project.advisor_name || "-"}</dd>
          </dl>
          <h3 style={{ marginTop: 20 }}>Notes</h3>
          <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>
            {project.notes || "(none)"}
          </pre>
        </div>
      </div>

      <div className="card">
        <h3>
          Human-in-the-loop{" "}
          <span
            className={`badge ${project.human_in_loop_enabled ? "accent" : ""}`}
          >
            {project.human_in_loop_enabled ? "ON" : "off"}
          </span>
        </h3>
        {project.human_in_loop_enabled ? (
          <dl className="kv">
            <dt>Approver</dt>
            <dd>{project.primary_approver_email || "(not set)"}</dd>
            <dt>CC</dt>
            <dd>{(project.cc_emails || []).join(", ") || "-"}</dd>
            <dt>Timeout (hours)</dt>
            <dd>{project.approval_timeout_hours ?? "-"}</dd>
            <dt>Reminder (hours)</dt>
            <dd>{project.reminder_interval_hours ?? "-"}</dd>
            <dt>Active gates</dt>
            <dd>{(project.approval_gates || []).join(", ") || "-"}</dd>
          </dl>
        ) : (
          <div style={{ color: "#666", fontSize: 13 }}>
            Pipeline runs automatically. Enable HITL on the project create
            page (or via <span className="mono">PUT /api/projects/{`{id}`}</span>)
            to pause at approval gates.
          </div>
        )}
      </div>

      <div className="card">
        <h3>Background context bundles</h3>
        {bundles.data && bundles.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Filename</th>
                <th>Size</th>
                <th>Extraction</th>
                <th>Text files</th>
                <th>Chars indexed</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {bundles.data.map((b) => (
                <tr key={b.id}>
                  <td>{b.filename}</td>
                  <td>{(b.size_bytes / 1024 / 1024).toFixed(2)} MB</td>
                  <td>
                    <span className="badge">{b.extraction_status}</span>
                  </td>
                  <td>{b.text_file_count}</td>
                  <td>{b.total_text_chars.toLocaleString()}</td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {b.created_at.slice(0, 19).replace("T", " ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">
            No background context uploaded. At intake you can upload a ZIP of
            notes, prior drafts, datasets, etc. (≤ 512 MB).
          </div>
        )}
      </div>
    </div>
  );
}
