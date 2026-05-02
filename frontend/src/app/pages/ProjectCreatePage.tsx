import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ProjectCreateInput } from "../../lib/api";
import Toast from "../components/Toast";

const DEFAULT_GATES = [
  "post_shortlist",
  "post_pilot_evidence",
  "pre_package_freeze",
];

const BUNDLE_MAX_MB = 512;

export default function ProjectCreatePage() {
  const nav = useNavigate();
  const [form, setForm] = useState<ProjectCreateInput>({
    title: "",
    student_name: "",
    student_ref: "",
    mentor_name: "",
    advisor_name: "",
    research_direction: "",
    target_venues: [],
    constraints: "",
    exploration_strategy: "breadth-first",
    provider_profile: "default",
    budget_usd: 50,
    notes: "",
    human_in_loop_enabled: false,
    primary_approver_email: "",
    cc_emails: [],
    approval_timeout_hours: 72,
    reminder_interval_hours: 24,
    approval_gates: DEFAULT_GATES,
  });
  const [venuesRaw, setVenuesRaw] = useState("");
  const [ccRaw, setCcRaw] = useState("");
  const [bundleFile, setBundleFile] = useState<File | null>(null);
  const [bundleError, setBundleError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const venues = venuesRaw.split(/[,;\n]/).map((v) => v.trim()).filter(Boolean);
      const ccs = ccRaw.split(/[,;\n]/).map((v) => v.trim()).filter(Boolean);
      const project = await api.createProject({
        ...form,
        target_venues: venues,
        cc_emails: ccs,
      });
      if (bundleFile) {
        try {
          await api.uploadContextBundle(project.id, bundleFile);
        } catch (e) {
          // Don't fail the whole project creation - the project exists,
          // the operator can retry the upload from the project page.
          setBundleError((e as Error).message);
        }
      }
      nav(`/projects/${project.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const pickBundle = (f: File | null) => {
    setBundleError(null);
    if (!f) {
      setBundleFile(null);
      return;
    }
    if (!f.name.toLowerCase().endsWith(".zip")) {
      setBundleError("Only .zip files are accepted.");
      return;
    }
    if (f.size > BUNDLE_MAX_MB * 1024 * 1024) {
      setBundleError(`File is larger than ${BUNDLE_MAX_MB} MB.`);
      return;
    }
    setBundleFile(f);
  };

  const update = <K extends keyof ProjectCreateInput>(key: K, value: ProjectCreateInput[K]) =>
    setForm((s) => ({ ...s, [key]: value }));

  return (
    <div>
      <div className="page-header">
        <h2>New project</h2>
      </div>

      <div className="card" style={{ maxWidth: 720 }}>
        <div className="grid two">
          <div className="field">
            <label>Title *</label>
            <input
              type="text"
              value={form.title}
              onChange={(e) => update("title", e.target.value)}
            />
          </div>
          <div className="field">
            <label>Exploration strategy</label>
            <select
              value={form.exploration_strategy || ""}
              onChange={(e) => update("exploration_strategy", e.target.value)}
            >
              <option value="breadth-first">breadth-first</option>
              <option value="depth-first">depth-first</option>
              <option value="focused">focused</option>
            </select>
          </div>
          <div className="field">
            <label>Owner *</label>
            <input
              type="text"
              value={form.student_name}
              onChange={(e) => update("student_name", e.target.value)}
            />
          </div>
          <div className="field">
            <label>Internal owner reference</label>
            <input
              type="text"
              value={form.student_ref || ""}
              onChange={(e) => update("student_ref", e.target.value)}
            />
          </div>
          <div className="field">
            <label>Reviewer *</label>
            <input
              type="text"
              value={form.mentor_name}
              onChange={(e) => update("mentor_name", e.target.value)}
            />
          </div>
          <div className="field">
            <label>Advisor</label>
            <input
              type="text"
              value={form.advisor_name || ""}
              onChange={(e) => update("advisor_name", e.target.value)}
            />
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>Research direction *</label>
            <textarea
              rows={4}
              value={form.research_direction}
              onChange={(e) => update("research_direction", e.target.value)}
            />
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>Target venues (comma or newline separated)</label>
            <textarea
              rows={2}
              value={venuesRaw}
              onChange={(e) => setVenuesRaw(e.target.value)}
              placeholder="NeurIPS Workshop, ICML Workshop"
            />
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>Constraints</label>
            <textarea
              rows={2}
              value={form.constraints || ""}
              onChange={(e) => update("constraints", e.target.value)}
              placeholder="<=2 GPUs, <=48h compute, no proprietary data"
            />
          </div>
          <div className="field">
            <label>Budget (USD)</label>
            <input
              type="number"
              value={form.budget_usd || 0}
              onChange={(e) => update("budget_usd", Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label>Provider profile</label>
            <input
              type="text"
              value={form.provider_profile || ""}
              onChange={(e) => update("provider_profile", e.target.value)}
            />
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>Notes</label>
            <textarea
              rows={2}
              value={form.notes || ""}
              onChange={(e) => update("notes", e.target.value)}
            />
          </div>
        </div>

        {/* Human-in-the-loop */}
        <h4 style={{ marginTop: 20 }}>Human-in-the-loop</h4>
        <div className="grid two">
          <div className="field">
            <label>Enable approval checkpoints</label>
            <select
              value={form.human_in_loop_enabled ? "yes" : "no"}
              onChange={(e) =>
                update("human_in_loop_enabled", e.target.value === "yes")
              }
            >
              <option value="no">no (pipeline runs automatically)</option>
              <option value="yes">yes (pause at gates for approval)</option>
            </select>
            <span className="help">
              When ON, batches and package freezes wait for a human approver
              to click through an emailed link (or use the internal
              Approvals tab).
            </span>
          </div>
          <div className="field">
            <label>Primary approver email</label>
            <input
              type="email"
              value={form.primary_approver_email || ""}
              onChange={(e) =>
                update("primary_approver_email", e.target.value)
              }
              placeholder="mentor@example.com"
              disabled={!form.human_in_loop_enabled}
            />
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>CC emails (comma or newline separated)</label>
            <textarea
              rows={2}
              value={ccRaw}
              onChange={(e) => setCcRaw(e.target.value)}
              disabled={!form.human_in_loop_enabled}
            />
          </div>
          <div className="field">
            <label>Approval timeout (hours)</label>
            <input
              type="number"
              value={form.approval_timeout_hours || 72}
              onChange={(e) =>
                update("approval_timeout_hours", Number(e.target.value))
              }
              disabled={!form.human_in_loop_enabled}
            />
          </div>
          <div className="field">
            <label>Reminder interval (hours)</label>
            <input
              type="number"
              value={form.reminder_interval_hours || 24}
              onChange={(e) =>
                update("reminder_interval_hours", Number(e.target.value))
              }
              disabled={!form.human_in_loop_enabled}
            />
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>Active gates</label>
            <div className="row" style={{ gap: 18 }}>
              {DEFAULT_GATES.map((g) => (
                <label key={g} style={{ display: "flex", gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={(form.approval_gates || []).includes(g)}
                    disabled={!form.human_in_loop_enabled}
                    onChange={(e) => {
                      const current = new Set(form.approval_gates || []);
                      if (e.target.checked) current.add(g);
                      else current.delete(g);
                      update("approval_gates", Array.from(current));
                    }}
                  />
                  <span style={{ fontSize: 12 }}>{g.replace(/_/g, " ")}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* Background context bundle */}
        <h4 style={{ marginTop: 20 }}>
          Background materials <span style={{ fontWeight: 400, color: "#666" }}>
            (optional, highly recommended)
          </span>
        </h4>
        <div className="help" style={{ fontSize: 12, color: "#666", marginBottom: 8 }}>
          Upload a single ZIP (&le; {BUNDLE_MAX_MB} MB) containing background
          notes, prior drafts, datasets, or other context. Text-like files
          (.md, .txt, .json, .yaml, .csv) are indexed and used to ground idea
          generation and downstream drafting. Non-text files are preserved
          for retrieval.
        </div>
        <div className="row">
          <input
            type="file"
            accept=".zip,application/zip"
            onChange={(e) => pickBundle(e.target.files?.[0] || null)}
          />
          {bundleFile && (
            <span style={{ fontSize: 12, color: "#555" }}>
              {bundleFile.name} ({Math.round(bundleFile.size / 1024)} KB)
            </span>
          )}
        </div>
        {bundleError && (
          <div className="warning-banner" style={{ marginTop: 8 }}>
            {bundleError}
          </div>
        )}

        <div className="row" style={{ justifyContent: "flex-end", marginTop: 16 }}>
          <button onClick={() => nav(-1)} disabled={submitting}>Cancel</button>
          <button
            className="primary"
            disabled={submitting || !form.title || !form.student_name || !form.mentor_name || !form.research_direction}
            onClick={submit}
          >
            {submitting ? "Creating…" : "Create project"}
          </button>
        </div>
      </div>

      <Toast message={error} kind="bad" onClose={() => setError(null)} />
    </div>
  );
}
