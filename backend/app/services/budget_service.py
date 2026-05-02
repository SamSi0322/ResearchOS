from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import AuditKind
from app.core.models import BudgetLedgerEntry, BudgetPolicy
from app.services.audit_service import AuditService
from app.utils import new_id


class BudgetService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    def record(
        self,
        project_id: str,
        *,
        amount_usd: float,
        kind: str,
        reference: str | None = None,
        meta: dict | None = None,
        commit: bool = True,
    ) -> BudgetLedgerEntry:
        entry = BudgetLedgerEntry(
            id=new_id("led"),
            project_id=project_id,
            kind=kind,
            amount_usd=amount_usd,
            reference=reference,
            meta=meta or {},
        )
        self.db.add(entry)
        self.audit.log(
            project_id=project_id,
            kind=AuditKind.budget_event,
            message=f"budget entry: +${amount_usd:.4f} ({kind})",
            payload={"kind": kind, "amount_usd": amount_usd, "reference": reference},
        )
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return entry

    def summary(self, project_id: str) -> dict:
        policy = (
            self.db.query(BudgetPolicy).filter(BudgetPolicy.project_id == project_id).first()
        )
        entries = (
            self.db.query(BudgetLedgerEntry)
            .filter(BudgetLedgerEntry.project_id == project_id)
            .all()
        )
        by_kind: dict[str, float] = {}
        entries_by_kind: dict[str, int] = {}
        for entry in entries:
            by_kind[entry.kind] = by_kind.get(entry.kind, 0.0) + float(entry.amount_usd or 0.0)
            entries_by_kind[entry.kind] = entries_by_kind.get(entry.kind, 0) + 1
        spent = sum(e.amount_usd for e in entries)
        ceiling = policy.ceiling_usd if policy else 0.0
        warn = (spent / ceiling) >= (policy.warn_ratio if policy else 0.8) if ceiling else False
        return {
            "ceiling_usd": ceiling,
            "spent_usd": spent,
            "remaining_usd": max(0.0, ceiling - spent),
            "warn": bool(warn),
            "entries": len(entries),
            "by_kind": {k: round(v, 6) for k, v in sorted(by_kind.items())},
            "entries_by_kind": dict(sorted(entries_by_kind.items())),
            "provider_call_spent_usd": round(by_kind.get("provider_call", 0.0), 6),
            "provider_call_entries": entries_by_kind.get("provider_call", 0),
        }
