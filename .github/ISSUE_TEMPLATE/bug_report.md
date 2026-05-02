---
name: Bug report
about: Report a bug in ResearchOS
title: ''
labels: bug
assignees: ''
---

## Description

A clear description of the bug.

## Reproduction

Steps to reproduce — ideally a minimal `curl` against `http://localhost:8000/api/...` or a console click path.

## Environment

- Run mode: `mock` / `smoke` / `production`
- Relevant `RESEARCHOS_*` env vars (redact secrets):
- Commit hash (`git rev-parse HEAD`):
- Python version:
- Node version (only if the frontend is involved):
- OS:

## Expected vs actual

What you expected to happen, and what actually happened.

## Additional context

Logs, the audit timeline (`GET /api/audit?...`), or the `manifest.json` from a frozen package often help triage.

> For security vulnerabilities, **do not open a public issue** — see [SECURITY.md](../../SECURITY.md).
