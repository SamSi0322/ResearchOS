# Result analysis

Given a metrics JSON and run metadata, decide:
- verdict: promising | inconclusive | rejected
- delta: per-metric signed difference between variant and baseline
- claims: list of evidence-backed statements; every numeric claim must cite a
  value that appears in the metrics blob - do not invent numbers

Err on the side of "inconclusive" when deltas are within noise. Say "rejected"
when the run failed outright or variant clearly underperforms.
