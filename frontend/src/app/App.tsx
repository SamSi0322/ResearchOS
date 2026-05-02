import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import DashboardPage from "./pages/DashboardPage";
import ProjectCreatePage from "./pages/ProjectCreatePage";
import ProjectDetailPage from "./pages/ProjectDetailPage";
import SettingsPage from "./pages/SettingsPage";
import AuditPage from "./pages/AuditPage";

export default function App() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 30_000,
  });

  return (
    <div className="shell">
      <aside className="sidebar">
        <h1>ResearchOS</h1>
        <nav>
          <NavLink to="/" end>
            Dashboard
          </NavLink>
          <NavLink to="/projects/new">+ New Project</NavLink>
          <NavLink to="/audit">Audit</NavLink>
          <NavLink to="/settings">Settings / Providers</NavLink>
        </nav>
        <div style={{ marginTop: "auto", paddingTop: 20, fontSize: 11, color: "#8a8a8a" }}>
          <div>
            backend:{" "}
            <span className={`badge ${health.data ? "good" : health.isError ? "bad" : ""}`}>
              {health.data ? "ok" : health.isError ? "offline" : "..."}
            </span>
          </div>
          <div style={{ marginTop: 4 }}>
            internal console &middot; localhost only
          </div>
        </div>
      </aside>
      <main className="main">
        <div
          className="warning-banner"
          title="Drafts require human validation before submission."
          style={{
            marginBottom: 12,
            background: "#eef3fb",
            borderLeft: "4px solid var(--accent, #3366cc)",
          }}
        >
          <strong>Decision support, not auto-submission.</strong>{" "}
          This system provides research decision support, not automatic paper
          submission. Every draft requires human validation before it leaves
          the console.
        </div>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/projects/new" element={<ProjectCreatePage />} />
          <Route
            path="/projects/:projectId/*"
            element={<ProjectDetailPage />}
          />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
