interface Props {
  value: string | null | undefined;
  mock?: boolean;
}

const MAP: Record<string, string> = {
  succeeded: "good",
  succeeded_valid: "good",
  running: "accent",
  queued: "accent",
  failed: "bad",
  failed_terminal: "bad",
  failed_retriable: "warn",
  canceled: "warn",
  timed_out: "warn",
  succeeded_invalid: "warn",
  resolved: "good",
  waived: "warn",
  open: "bad",
  reopened: "warn",
  frozen: "good",
  draft: "accent",
  drafting: "accent",
  in_review: "warn",
  superseded: "",
  accepted: "good",
  active: "good",
  paused: "warn",
  archived: "",
  packaged: "good",
  scheduled: "accent",
  completed: "good",
};

export default function StatusBadge({ value, mock }: Props) {
  if (!value && !mock) return null;
  return (
    <>
      {value && <span className={`badge ${MAP[value] ?? ""}`}>{value}</span>}
      {mock && <span className="badge mock" style={{ marginLeft: 6 }}>MOCK</span>}
    </>
  );
}
