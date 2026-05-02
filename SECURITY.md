# Security policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in ResearchOS,
**please do not open a public issue**. Use one of the following private
channels:

1. **GitHub Private Vulnerability Reporting** (preferred). Open a draft
   advisory at
   <https://github.com/SamSi0322/ResearchOS/security/advisories/new>.
   The advisory stays private until we publish it; we can coordinate the
   fix with you in the same thread.

2. **Email**: <yuan.si@uwaterloo.ca>. Encrypt at your discretion. The
   mailbox is not formally monitored 24/7 — if the issue is time-sensitive,
   GitHub Private Vulnerability Reporting is the faster channel.

Please include:

- A clear description of the vulnerability and the impact (what an attacker
  can read, write, or do).
- Reproduction steps — ideally a minimal `curl` or Python snippet against a
  local install.
- The commit hash you reproduced against (`git rev-parse HEAD`).
- Your run mode (`mock`, `smoke`, or `production`) and the relevant
  `RESEARCHOS_*` env vars at the time, if relevant.

## Supported versions

ResearchOS is at the local-MVP stage. Only the latest commit on `main` is
supported; older tags or branches receive no security backports. If you are
running an older copy, please upgrade before reporting.

## Response expectations

This is a single-maintainer open-source project under AGPL-3.0. Response
is **best effort, no SLA**. You can typically expect:

- An initial acknowledgement within ~5 business days.
- A triage decision (severity, planned fix window, or rejection with
  reason) within ~14 days of acknowledgement.
- Coordinated disclosure once a fix or mitigation is in place.

If you do not hear back within those windows, please follow up — bounce
backs and missed pings are far more likely than malice.

## Scope

**In scope:**

- The FastAPI backend under `backend/app/` and its provider adapters
  (OpenAI, Anthropic, mock).
- The Vite/React operator console under `frontend/`.
- The encrypted secret store, model alias / capability gates, and HITL
  approval flow.
- Anything that could cause a stored API key to leak out of the local
  install.

**Out of scope:**

- Vulnerabilities in upstream dependencies — please report those upstream
  first; if a downstream mitigation makes sense in ResearchOS, file a
  follow-up here once the upstream advisory is public.
- Local denial-of-service from someone who already has shell access on
  the host (ResearchOS is single-tenant, single-host by design).
- Cosmetic or UX issues. File a regular issue for those.
