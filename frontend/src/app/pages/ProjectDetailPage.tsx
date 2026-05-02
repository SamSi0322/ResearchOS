import { NavLink, Navigate, Route, Routes, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StatusBadge from "../components/StatusBadge";
import BriefTab from "../../features/projects/BriefTab";
import IdeasTab from "../../features/ideas/IdeasTab";
import FunnelTab from "../../features/funnel/FunnelTab";
import SpecsTab from "../../features/specs/SpecsTab";
import RunsTab from "../../features/runs/RunsTab";
import DraftsTab from "../../features/drafts/DraftsTab";
import ReviewsTab from "../../features/reviews/ReviewsTab";
import SessionsTab from "../../features/sessions/SessionsTab";
import PackagesTab from "../../features/packages/PackagesTab";
import AuditTab from "../../features/audit/AuditTab";
import ApprovalsTab from "../../features/approvals/ApprovalsTab";

export default function ProjectDetailPage() {
  const { projectId = "" } = useParams();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
    enabled: !!projectId,
  });

  if (!projectId) return <Navigate to="/" replace />;
  if (project.isLoading) return <div>Loading…</div>;
  if (project.error) return <div className="empty">Project not found.</div>;
  const p = project.data!;

  return (
    <div>
      <div className="page-header">
        <div>
          <h2>{p.title}</h2>
          <div style={{ color: "#666", fontSize: 13 }}>
            {p.student_name} &middot; mentor {p.mentor_name}
            {p.advisor_name ? ` · advisor ${p.advisor_name}` : ""} &middot;{" "}
            <span className="mono">{p.id}</span>
          </div>
        </div>
        <div className="actions">
          <StatusBadge value={p.status} />
        </div>
      </div>

      <div className="tabs">
        <NavLink to="brief" end>Brief</NavLink>
        <NavLink to="ideas">Ideas</NavLink>
        <NavLink to="funnel">Funnel</NavLink>
        <NavLink to="specs">Specs</NavLink>
        <NavLink to="runs">Runs</NavLink>
        <NavLink to="drafts">Drafts</NavLink>
        <NavLink to="reviews">Reviews</NavLink>
        <NavLink to="approvals">Approvals</NavLink>
        <NavLink to="sessions">Sessions</NavLink>
        <NavLink to="packages">Package</NavLink>
        <NavLink to="audit">Audit</NavLink>
      </div>

      <Routes>
        <Route path="" element={<Navigate to="brief" replace />} />
        <Route path="brief" element={<BriefTab projectId={projectId} />} />
        <Route path="ideas" element={<IdeasTab projectId={projectId} />} />
        <Route path="funnel" element={<FunnelTab projectId={projectId} />} />
        <Route path="specs" element={<SpecsTab projectId={projectId} />} />
        <Route path="runs" element={<RunsTab projectId={projectId} />} />
        <Route path="drafts" element={<DraftsTab projectId={projectId} />} />
        <Route path="reviews" element={<ReviewsTab projectId={projectId} />} />
        <Route path="approvals" element={<ApprovalsTab projectId={projectId} />} />
        <Route path="sessions" element={<SessionsTab projectId={projectId} />} />
        <Route path="packages" element={<PackagesTab projectId={projectId} />} />
        <Route path="audit" element={<AuditTab projectId={projectId} />} />
      </Routes>
    </div>
  );
}
