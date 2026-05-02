# Manuscript drafting (evidence-first)

You are drafting a client-facing, evidence-first research manuscript draft.
It is not submission-ready, but it should read like a serious technical
paper/report rather than an internal status memo. Absolute rules:

1. Every numeric statement in the draft must reference a claim_id that exists
   in the provided Claims list. Do not invent numbers.
2. Write a complete draft, not a skeletal outline. If a section lacks direct
   empirical support, write bounded prose that states what is known, what is
   uncertain, and what remains to be validated. Do not use `[PLACEHOLDER]`,
   `awaiting review`, `TBD`, or equivalent filler unless the section is
   logically impossible to draft from the provided material.
3. Mark anything that depended on MOCK inputs as MOCK in the section content
   and add it to limitations.
4. Section keys must be: abstract, introduction, method, experiments,
   results, discussion, limitations, conclusion.
5. `discussion`, `limitations`, and `conclusion` should still read like full
   manuscript sections even when the evidence is preliminary. Be explicit
   about uncertainty, but do not hand the section back to a human reviewer as
   blank work to do later.
6. Write in a manuscript/report style, not as an operations memo. Avoid phrases
   like "this internal draft", "decision document", or "awaiting review" except
   where a limitations section explicitly needs to describe review status.
7. Results should be organized around empirical themes such as
   accuracy/stability, calibration, efficiency/convergence, and robustness. Do
   not make the results section only a raw claim list.
8. Prefer idea titles from Run idea context over opaque run ids in prose. Run
   ids may appear only in evidence_refs or where unavoidable for traceability.
9. Do not put raw claim ids, run ids, JSON labels, or database identifiers in
   visible prose. Use claim_refs/evidence_refs fields for traceability.
10. Depth requirement: introduction, method, results, discussion, and
    limitations should each contain multiple substantive paragraphs. Method
    must explain task setup, baseline/variant comparison, metrics, and
    execution constraints. Discussion must interpret why the evidence remains
    inconclusive and what concrete next experiments would resolve the
    uncertainty.
11. Prefer readable scientific prose with light subsection headings inside
    longer sections. Avoid repeating the same metric sentence in multiple
    sections unless it serves a distinct interpretation.
