import { ReactNode, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, DeliveryPackage } from "../../lib/api";
import StatusBadge from "../../app/components/StatusBadge";
import Toast from "../../app/components/Toast";
import Modal from "../../app/components/Modal";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

export default function PackagesTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const packages = useQuery({
    queryKey: ["packages", projectId],
    queryFn: () => api.listPackages(projectId),
  });
  const [includeMock, setIncludeMock] = useState(true);
  const [allowP2, setAllowP2] = useState(false);
  const [notes, setNotes] = useState("");
  const [toast, setToast] = useState<string | null>(null);

  const latest: DeliveryPackage | undefined = useMemo(() => {
    if (!packages.data || packages.data.length === 0) return undefined;
    return [...packages.data].sort((a, b) => b.version - a.version)[0];
  }, [packages.data]);

  const buildMut = useMutation({
    mutationFn: () =>
      api.buildPackage(projectId, {
        include_mock: includeMock,
        allow_with_waived_p2: allowP2,
        notes: notes || undefined,
      }),
    onSuccess: (pkg) => {
      qc.invalidateQueries({ queryKey: ["packages", projectId] });
      setToast(`Built package v${pkg.version} (${formatBytes(pkg.size_bytes)})`);
    },
    onError: (e: Error) => setToast(e.message),
  });

  return (
    <div>
      {latest ? (
        <div className="card" style={{ borderLeft: "4px solid var(--accent)" }}>
          <div
            className="row"
            style={{ justifyContent: "space-between", alignItems: "flex-start" }}
          >
            <div>
              <h3 style={{ margin: 0 }}>Latest package</h3>
              <div style={{ marginTop: 6 }}>
                <strong style={{ fontSize: 18 }}>v{latest.version}</strong>{" "}
                <StatusBadge value={latest.status} mock={latest.mock} />
              </div>
              <div style={{ color: "#555", fontSize: 13, marginTop: 6 }}>
                {formatBytes(latest.size_bytes)}
                {latest.summary ? ` · ${latest.summary}` : ""}
              </div>
              <div className="mono" style={{ fontSize: 11, color: "#888" }}>
                sha256: {latest.sha256?.slice(0, 32)}…
              </div>
              {latest.mock && (
                <div className="warning-banner" style={{ marginTop: 10 }}>
                  This package contains MOCK artifacts. It is fine for internal
                  walkthroughs; do <strong>not</strong> hand it to a student as
                  a real research outcome.
                </div>
              )}
            </div>
            <div className="row" style={{ flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
              <a
                href={api.downloadPackageManuscriptPdf(projectId, latest.id)}
                target="_blank"
                rel="noreferrer"
              >
                <button
                  className="primary"
                  style={{ fontSize: 14, padding: "10px 18px" }}
                >
                  ⤓ Manuscript PDF
                </button>
              </a>
              <a
                href={api.downloadPackage(projectId, latest.id)}
                target="_blank"
                rel="noreferrer"
              >
                <button className="small">⤓ Full ZIP</button>
              </a>
            </div>
          </div>
          <PackageDetailPanel projectId={projectId} pkg={latest} />
        </div>
      ) : (
        <div className="empty">
          No frozen package yet. Resolve / waive any P0/P1 review issues, then
          click <strong>Build package</strong> below.
        </div>
      )}

      <div className="card">
        <h3>Freeze & package</h3>
        <div className="grid two">
          <div className="field">
            <label>Include MOCK artifacts</label>
            <select
              value={includeMock ? "yes" : "no"}
              onChange={(e) => setIncludeMock(e.target.value === "yes")}
            >
              <option value="yes">yes (include, tag clearly)</option>
              <option value="no">no (drop mock artifacts)</option>
            </select>
          </div>
          <div className="field">
            <label>Allow freeze with open P2 issues</label>
            <select
              value={allowP2 ? "yes" : "no"}
              onChange={(e) => setAllowP2(e.target.value === "yes")}
            >
              <option value="no">no</option>
              <option value="yes">yes (explicit waiver)</option>
            </select>
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>Notes</label>
            <textarea
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
          </div>
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button
            className="primary"
            onClick={() => buildMut.mutate()}
            disabled={buildMut.isPending}
          >
            {buildMut.isPending ? "Freezing…" : "Build package"}
          </button>
        </div>
        <div style={{ fontSize: 12, color: "#666" }}>
          Freeze fails if any P0/P1 issue is open. P2 requires explicit waiver.
        </div>
      </div>

      <div className="card">
        <h3>Package history</h3>
        {packages.data && packages.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th>Status</th>
                <th>Size</th>
                <th>sha256</th>
                <th>Summary</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {packages.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    <strong>v{p.version}</strong>
                  </td>
                  <td>
                    <StatusBadge value={p.status} mock={p.mock} />
                  </td>
                  <td>{formatBytes(p.size_bytes)}</td>
                  <td className="mono" style={{ fontSize: 10 }}>
                    {p.sha256?.slice(0, 16)}…
                  </td>
                  <td>{p.summary}</td>
                  <td className="mono">{p.created_at.slice(0, 19)}</td>
                  <td>
                    <a
                      href={api.downloadPackage(projectId, p.id)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <button className="small primary">⤓ ZIP</button>
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">
            No packages yet. Packages are created by clicking{" "}
            <strong>Build package</strong> above.
          </div>
        )}
      </div>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}

function PackageDetailPanel({
  projectId,
  pkg,
}: {
  projectId: string;
  pkg: DeliveryPackage;
}) {
  const manifest = useQuery({
    queryKey: ["pkg-manifest", pkg.id],
    queryFn: () => api.packageManifest(projectId, pkg.id),
  });
  const readiness = useQuery({
    queryKey: ["readiness", projectId],
    queryFn: () => api.readiness(projectId),
  });
  const [showMarkdown, setShowMarkdown] = useState(false);
  const md = useQuery({
    queryKey: ["pkg-md", pkg.id],
    queryFn: () => api.packageManuscriptMarkdown(projectId, pkg.id),
    enabled: showMarkdown,
  });

  const m = (manifest.data || {}) as Record<string, any>;
  const ms = m.manuscript || {};
  const counts = m.counts || {};
  const tier = readiness.data?.tier as string | undefined;

  return (
    <div style={{ marginTop: 12 }}>
      <div className="row" style={{ gap: 18, flexWrap: "wrap" }}>
        <MiniTile label="readiness" value={tier?.replace(/_/g, " ") || "-"} />
        <MiniTile label="decision" value={m.package_decision || "-"} />
        <MiniTile label="ideas" value={counts.ideas ?? "-"} />
        <MiniTile label="runs" value={counts.runs ?? "-"} />
        <MiniTile label="claims" value={counts.claims ?? "-"} />
        <MiniTile label="drafts" value={counts.drafts ?? "-"} />
        <MiniTile label="has PDF" value={ms.has_pdf ? "yes" : "no"} />
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="small" onClick={() => setShowMarkdown(true)}>
          Preview manuscript.md
        </button>
        <a
          href={api.downloadPackage(projectId, pkg.id)}
          target="_blank"
          rel="noreferrer"
        >
          <button className="small ghost">⤓ ZIP</button>
        </a>
        {ms.has_pdf && (
          <a
            href={api.downloadPackageManuscriptPdf(projectId, pkg.id)}
            target="_blank"
            rel="noreferrer"
          >
            <button className="small ghost">⤓ PDF</button>
          </a>
        )}
      </div>
      {showMarkdown && (
        <Modal
          title={`manuscript/${ms.draft_version ? `draft_v${ms.draft_version}.md` : "markdown"}`}
          onClose={() => setShowMarkdown(false)}
          wide
          footer={
            <button onClick={() => setShowMarkdown(false)}>Close</button>
          }
        >
          {md.isLoading ? (
            <div>Loading…</div>
          ) : md.isError ? (
            <div style={{ color: "#b42318" }}>{(md.error as Error).message}</div>
          ) : (
            <pre
              style={{
                whiteSpace: "pre-wrap",
                fontFamily: "inherit",
                maxHeight: "70vh",
                overflowY: "auto",
              }}
            >
              {md.data || ""}
            </pre>
          )}
        </Modal>
      )}
    </div>
  );
}

function MiniTile({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "#666", textTransform: "uppercase" }}>
        {label}
      </div>
      <div style={{ fontSize: 14, fontWeight: 600 }}>{value}</div>
    </div>
  );
}
