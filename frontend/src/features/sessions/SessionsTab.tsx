import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StatusBadge from "../../app/components/StatusBadge";
import Toast from "../../app/components/Toast";

export default function SessionsTab({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const sessions = useQuery({
    queryKey: ["sessions", projectId],
    queryFn: () => api.listSessions(projectId),
  });
  const [toast, setToast] = useState<string | null>(null);
  const [form, setForm] = useState({
    scheduled_at: new Date().toISOString().slice(0, 16),
    mentor_name: "",
    status: "scheduled",
    notes: "",
    student_participation_notes: "",
    next_actions: "",
    unresolved_blockers: "",
    student_must_understand: "",
  });

  const createMut = useMutation({
    mutationFn: () =>
      api.createSession(projectId, {
        scheduled_at: new Date(form.scheduled_at).toISOString(),
        mentor_name: form.mentor_name || "Mentor",
        status: form.status,
        notes: form.notes,
        student_participation_notes: form.student_participation_notes,
        next_actions: split(form.next_actions),
        unresolved_blockers: split(form.unresolved_blockers),
        student_must_understand: split(form.student_must_understand),
      } as any),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sessions", projectId] });
      setToast("Session logged");
    },
    onError: (e: Error) => setToast(e.message),
  });

  const delMut = useMutation({
    mutationFn: (id: string) => api.deleteSession(projectId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions", projectId] }),
  });

  return (
    <div>
      <div className="card">
        <h3>Log mentorship session</h3>
        <div className="grid two">
          <div className="field">
            <label>Scheduled at</label>
            <input
              type="datetime-local"
              value={form.scheduled_at}
              onChange={(e) => setForm({ ...form, scheduled_at: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Mentor</label>
            <input
              type="text"
              value={form.mentor_name}
              onChange={(e) => setForm({ ...form, mentor_name: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Status</label>
            <select
              value={form.status}
              onChange={(e) => setForm({ ...form, status: e.target.value })}
            >
              <option value="scheduled">scheduled</option>
              <option value="completed">completed</option>
              <option value="canceled">canceled</option>
            </select>
          </div>
          <div className="field">
            <label>Next actions (one per line)</label>
            <textarea
              rows={2}
              value={form.next_actions}
              onChange={(e) => setForm({ ...form, next_actions: e.target.value })}
            />
          </div>
          <div className="field" style={{ gridColumn: "1/-1" }}>
            <label>Notes</label>
            <textarea
              rows={3}
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Student participation</label>
            <textarea
              rows={2}
              value={form.student_participation_notes}
              onChange={(e) =>
                setForm({ ...form, student_participation_notes: e.target.value })
              }
            />
          </div>
          <div className="field">
            <label>Student must understand</label>
            <textarea
              rows={2}
              value={form.student_must_understand}
              onChange={(e) =>
                setForm({ ...form, student_must_understand: e.target.value })
              }
            />
          </div>
          <div className="field">
            <label>Unresolved blockers</label>
            <textarea
              rows={2}
              value={form.unresolved_blockers}
              onChange={(e) =>
                setForm({ ...form, unresolved_blockers: e.target.value })
              }
            />
          </div>
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button
            className="primary"
            disabled={createMut.isPending || !form.mentor_name}
            onClick={() => createMut.mutate()}
          >
            {createMut.isPending ? "Saving…" : "Log session"}
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Sessions</h3>
        {sessions.data && sessions.data.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Mentor</th>
                <th>Status</th>
                <th>Notes</th>
                <th>Next actions</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sessions.data.map((s) => (
                <tr key={s.id}>
                  <td className="mono">{s.scheduled_at.slice(0, 16).replace("T", " ")}</td>
                  <td>{s.mentor_name}</td>
                  <td><StatusBadge value={s.status} /></td>
                  <td style={{ fontSize: 12, maxWidth: 260 }}>{s.notes}</td>
                  <td style={{ fontSize: 12, maxWidth: 220 }}>
                    {s.next_actions?.map((a, i) => (
                      <div key={i}>• {a}</div>
                    ))}
                  </td>
                  <td>
                    <button
                      className="small danger"
                      onClick={() => {
                        if (confirm("Delete session?")) delMut.mutate(s.id);
                      }}
                    >
                      delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">No sessions logged yet.</div>
        )}
      </div>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}

function split(s: string): string[] {
  return s.split(/\n+/).map((v) => v.trim()).filter(Boolean);
}
