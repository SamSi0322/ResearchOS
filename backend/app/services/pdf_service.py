"""Render a Draft + project context to a PDF manuscript.

Design notes
------------
* We use ``reportlab`` Platypus flowables. It is pure Python, cross-platform,
  and does not need any native GTK/Cairo dependency (so Windows just works).
* No watermark: instead we prepend a first-page *warning* block in a muted
  red panel that an operator cannot miss but that does not visually destroy
  the manuscript. The warning is repeated as a small header banner on every
  subsequent page.
* Evidence-first: the renderer reads whatever sections / claim refs the
  ``Draft`` already carries. It never adds empirical claims that are not in
  the draft. Mock / smoke / placeholder status is rendered prominently in
  the warning, **not** hidden.
* Output file path is returned as a Path; the caller decides where to persist
  the artifact (usually ``ArtifactStore``).

The module is self-contained; no code outside ``PDFService.build`` touches
reportlab.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.core.models import Claim, Draft, DraftSection, Manuscript, StudentProject
from app.utils import get_logger

logger = get_logger(__name__)


_WARNING_TITLE = "Draft status"
_WARNING_BODY = (
    "AI-assisted manuscript draft generated from ResearchOS evidence. Human "
    "verification is required before external sharing, submission, or any claim "
    "that this is a final research outcome."
)


# ---------------------------------------------------------------------------
# Input / output dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PDFBuildRequest:
    project: StudentProject
    manuscript: Manuscript
    draft: Draft
    sections: list[DraftSection]
    claims: list[Claim]
    run_summaries: list[dict[str, Any]]
    smoke_mode: bool
    quality_summary: dict[str, Any] | None = None


@dataclass
class PDFBuildResult:
    path: Path
    page_count: int
    size_bytes: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PDFService:
    def __init__(self) -> None:
        self._styles = self._build_styles()

    # --- public ----------------------------------------------------------

    def build(self, req: PDFBuildRequest, out_path: Path) -> PDFBuildResult:
        out_path.parent.mkdir(parents=True, exist_ok=True)

        doc = BaseDocTemplate(
            str(out_path),
            pagesize=LETTER,
            leftMargin=0.9 * inch,
            rightMargin=0.9 * inch,
            topMargin=1.0 * inch,
            bottomMargin=0.9 * inch,
            title=req.manuscript.title,
            author=req.project.mentor_name,
            subject=f"ResearchOS draft v{req.draft.version}",
        )

        frame = Frame(
            doc.leftMargin,
            doc.bottomMargin,
            doc.width,
            doc.height,
            id="body",
        )
        template = PageTemplate(
            id="default",
            frames=[frame],
            onPage=lambda canvas, doc_: self._draw_header_footer(canvas, doc_, req),
        )
        doc.addPageTemplates([template])

        story = list(self._build_flowables(req))
        doc.build(story)

        size = out_path.stat().st_size
        # Pages aren't directly known without a second render; use the
        # documented page count from the doc template (Platypus exposes it).
        try:
            page_count = int(getattr(doc, "page", 0))
        except Exception:  # noqa: BLE001
            page_count = 0
        logger.info(
            "pdf manuscript rendered",
            extra={
                "path": str(out_path),
                "size_bytes": size,
                "pages": page_count,
                "draft_id": req.draft.id,
                "smoke_mode": req.smoke_mode,
                "mock_draft": req.draft.mock,
            },
        )
        return PDFBuildResult(path=out_path, page_count=page_count, size_bytes=size)

    # --- flowables -------------------------------------------------------

    def _build_flowables(self, req: PDFBuildRequest) -> Iterable[Any]:
        styles = self._styles

        # Warning block on page 1.
        yield self._warning_block(req)
        yield Spacer(1, 0.25 * inch)

        # Title block.
        yield Paragraph(req.manuscript.title or req.project.title, styles["title"])
        yield Spacer(1, 0.1 * inch)
        yield Paragraph(
            f"Target venue: {req.manuscript.target_venue or '—'}", styles["small"]
        )
        yield Paragraph(
            f"Draft v{req.draft.version} · project <b>{req.project.title}</b>",
            styles["small"],
        )
        yield Paragraph(
            f"Owner: {req.project.student_name} · Reviewer: {req.project.mentor_name}",
            styles["small"],
        )
        yield Paragraph(
            f"Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
            styles["small"],
        )
        if req.quality_summary:
            q = req.quality_summary
            yield Spacer(1, 0.1 * inch)
            yield Paragraph(
                (
                    f"Completeness score: <b>{q.get('draft_completeness_score', 0):.2f}</b> · "
                    f"evidence coverage: {q.get('evidence_coverage_ratio', 0):.2f} · "
                    f"placeholders: {q.get('placeholder_count', 0)} · "
                    f"unsupported claim refs: {q.get('unsupported_claim_reference_count', 0)}"
                ),
                styles["small"],
            )

        yield Spacer(1, 0.25 * inch)

        # Sections.
        for sec in sorted(req.sections, key=lambda s: s.order_index):
            yield Paragraph(sec.title, styles["h1"])
            for block in _paragraphs(sec.content or ""):
                yield Paragraph(block, styles["body"])
                yield Spacer(1, 0.06 * inch)
            yield Spacer(1, 0.12 * inch)

        # Claim appendix.
        if req.claims:
            yield PageBreak()
            yield Paragraph("Appendix A — Evidence-backed claims", styles["h1"])
            rows = [["Claim id", "Kind", "Value", "Text", "Source run"]]
            for c in req.claims:
                rows.append(
                    [
                        Paragraph(c.id, styles["mono"]),
                        Paragraph(c.kind or "—", styles["small"]),
                        Paragraph((c.value or "—")[:32], styles["small"]),
                        Paragraph(_shorten(c.text, 360), styles["small"]),
                        Paragraph(c.run_id or "—", styles["mono"]),
                    ]
                )
            yield Table(
                rows,
                colWidths=[1.0 * inch, 0.8 * inch, 0.9 * inch, 3.1 * inch, 1.0 * inch],
                style=_table_style(),
                repeatRows=1,
            )

        # Run appendix.
        if req.run_summaries:
            yield Spacer(1, 0.25 * inch)
            yield Paragraph("Appendix B — Experiment runs", styles["h1"])
            rows = [["Run id", "Status", "Result", "Mock", "Metrics preview"]]
            for r in req.run_summaries:
                rows.append(
                    [
                        Paragraph(str(r.get("id", "—")), styles["mono"]),
                        Paragraph(str(r.get("status", "—")), styles["small"]),
                        Paragraph(str(r.get("result_class") or "—"), styles["small"]),
                        Paragraph("yes" if r.get("mock") else "no", styles["small"]),
                        Paragraph(_shorten(_metrics_preview(r.get("metrics")), 360), styles["small"]),
                    ]
                )
            yield Table(
                rows,
                colWidths=[1.1 * inch, 0.8 * inch, 1.0 * inch, 0.5 * inch, 3.4 * inch],
                style=_table_style(),
                repeatRows=1,
            )

    def _warning_block(self, req: PDFBuildRequest) -> KeepTogether:
        styles = self._styles
        extras = []
        if req.draft.mock:
            extras.append("This draft was generated from MOCK experimental evidence.")
        if req.smoke_mode:
            extras.append("This draft was produced in SMOKE mode (cheap budget).")
        if req.quality_summary and req.quality_summary.get("placeholder_count", 0) > 0:
            extras.append(
                f"Draft contains {int(req.quality_summary['placeholder_count'])} "
                "section placeholder(s) — those sections are not evidence-backed."
            )
        tag_line = " ".join(extras) or ""

        body_rows = [
            [Paragraph(_WARNING_TITLE, styles["warning_title"])],
            [Paragraph(_WARNING_BODY, styles["warning_body"])],
        ]
        if tag_line:
            body_rows.append([Paragraph(tag_line, styles["warning_tags"])])

        table = Table(
            body_rows,
            colWidths=[doc_width_inches() * inch],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7F8FA")),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#98A2B3")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            ),
        )
        return KeepTogether([table])

    # --- header/footer ---------------------------------------------------

    def _draw_header_footer(self, canvas, doc_, req: PDFBuildRequest) -> None:
        canvas.saveState()
        # Page header banner (thin) on pages >= 2 only.
        if doc_.page > 1:
            canvas.setFillColor(colors.HexColor("#475467"))
            canvas.setFont("Helvetica-Bold", 8)
            canvas.drawString(
                0.9 * inch,
                LETTER[1] - 0.55 * inch,
                "AI-assisted draft; human verification required",
            )
            canvas.setStrokeColor(colors.HexColor("#E0E0E0"))
            canvas.setLineWidth(0.4)
            canvas.line(
                0.9 * inch,
                LETTER[1] - 0.62 * inch,
                LETTER[0] - 0.9 * inch,
                LETTER[1] - 0.62 * inch,
            )

        # Footer: page number + draft id.
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.setFont("Helvetica", 8)
        canvas.drawString(
            0.9 * inch,
            0.5 * inch,
            f"{req.manuscript.title}  ·  draft v{req.draft.version}",
        )
        canvas.drawRightString(
            LETTER[0] - 0.9 * inch,
            0.5 * inch,
            f"page {doc_.page}",
        )
        canvas.restoreState()

    # --- styles ----------------------------------------------------------

    @staticmethod
    def _build_styles() -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()
        out: dict[str, ParagraphStyle] = {}
        out["title"] = ParagraphStyle(
            "title",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            spaceAfter=6,
            alignment=TA_LEFT,
        )
        out["h1"] = ParagraphStyle(
            "h1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            spaceBefore=10,
            spaceAfter=6,
            textColor=colors.HexColor("#1a1b1f"),
        )
        out["body"] = ParagraphStyle(
            "body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            spaceAfter=2,
        )
        out["small"] = ParagraphStyle(
            "small",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#555"),
        )
        out["mono"] = ParagraphStyle(
            "mono",
            parent=base["Code"],
            fontName="Courier",
            fontSize=8,
            leading=10,
        )
        out["refs"] = ParagraphStyle(
            "refs",
            parent=base["BodyText"],
            fontName="Helvetica-Oblique",
            fontSize=9,
            leading=11,
            textColor=colors.HexColor("#666"),
        )
        out["warning_title"] = ParagraphStyle(
            "warning_title",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#344054"),
            spaceAfter=4,
        )
        out["warning_body"] = ParagraphStyle(
            "warning_body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#344054"),
        )
        out["warning_tags"] = ParagraphStyle(
            "warning_tags",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#344054"),
            spaceBefore=4,
        )
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _paragraphs(text: str) -> list[str]:
    if not text:
        return []
    # Split on blank lines; each non-empty block becomes a paragraph. We also
    # escape HTML-relevant characters so Platypus rendering is stable.
    blocks: list[str] = []
    for raw in text.split("\n\n"):
        t = raw.strip()
        if not t:
            continue
        blocks.append(_inline_markdown(_escape_html(t)).replace("\n", "<br/>"))
    return blocks


def _inline_markdown(s: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _shorten(s: str, limit: int) -> str:
    s = (s or "").strip()
    return _escape_html(s if len(s) <= limit else s[: limit - 1] + "…")


def _metrics_preview(m: Any) -> str:
    if not isinstance(m, dict) or not m:
        return "—"
    try:
        import json

        return json.dumps(m, default=str)[:360]
    except Exception:  # noqa: BLE001
        return str(m)[:360]


def _table_style() -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F1F4")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1a1b1f")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D0D5DD")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )


def doc_width_inches() -> float:
    # 8.5 page - 2*0.9 inch margins = 6.7 inches of body width.
    return 6.7
