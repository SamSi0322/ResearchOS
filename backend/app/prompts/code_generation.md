# Code generation / review for toy experiments

Produce a small Python project that:
- runs with `python train.py` from the code root
- is deterministic given the seed in the spec
- writes `../outputs/metrics.json` and any supporting artifacts under `../outputs/`
  relative to `train.py` (the run workspace output directory, not `code/outputs/`)
- exits 0 whenever the experiment completed and wrote metrics, even if the
  hypothesis failed; use nonzero exit codes only for runtime/setup errors

## Available imports

You may import:

1. Anything in the Python 3.11 **standard library** (e.g. `math`, `random`,
   `json`, `statistics`, `pathlib`, `urllib.request`, `concurrent.futures`).
2. A minimal third-party package only when it is genuinely needed.

If you use ANY third-party package:
- include a `requirements.txt` file that lists it explicitly
- include an `ENVIRONMENT.md` file with a short note on why it is needed
- prefer a small, stable dependency set

You may NOT import:
- `researchos`, `experiment`, `toolkit`, or any package named after the
  project or spec. These do not exist.
- Any ML framework (`torch`, `tensorflow`, `sklearn`, `pandas`) unless the
  spec's `dependency_constraints` lists it explicitly.
- OpenAI, Anthropic, or any provider SDK. If the experiment needs a
  provider call, use `urllib.request` or `requests` against a documented
  HTTP endpoint.

If a spec field mentions any package you're not sure about, prefer
stdlib and note the substitution in a code
comment. DO NOT invent an import just because the spec hints at it.

## Other rules

Keep it small. A few files at most. Prefer readable code over clever code.
Do not write to the network unless the experiment explicitly requires it
(then use `requests`). Do not call `time.sleep` with long delays. Do not
generate any code that requires GPU unless the spec explicitly asks.

If your project is stdlib-only, you may omit `requirements.txt`, but you
should still keep the code runnable with `python train.py`.

For review passes, return full-file rewrites in `patches[]` (do not emit
diffs). Every patch must keep the metrics.json writing behavior.
