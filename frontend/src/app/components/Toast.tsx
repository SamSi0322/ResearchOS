import { useEffect } from "react";

interface Props {
  message: string | null;
  kind?: "good" | "bad" | "warn" | "";
  onClose: () => void;
}

export default function Toast({ message, kind, onClose }: Props) {
  useEffect(() => {
    if (!message) return;
    const t = setTimeout(onClose, 3000);
    return () => clearTimeout(t);
  }, [message, onClose]);
  if (!message) return null;
  return <div className={`toast ${kind || ""}`}>{message}</div>;
}
