import { ReactNode } from "react";

interface ModalProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}

export default function Modal({ title, onClose, children, footer, wide }: ModalProps) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        style={wide ? { width: "min(760px, 100%)" } : undefined}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>{title}</h3>
          <button className="ghost" onClick={onClose}>
            ×
          </button>
        </div>
        <div style={{ marginTop: 12 }}>{children}</div>
        {footer && <div className="footer">{footer}</div>}
      </div>
    </div>
  );
}
