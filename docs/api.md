# API quick-reference

All routes are mounted under `/api`. Full schema is at `/docs` (Swagger UI).

## Health

| Method | Path            | Purpose                      |
| ------ | --------------- | ---------------------------- |
| GET    | /api/health     | basic liveness + version     |

## Providers (secret-safe)

| Method | Path                       | Purpose                                          |
| ------ | -------------------------- | ------------------------------------------------ |
| GET    | /api/providers             | List masked credentials                          |
| POST   | /api/providers             | Add credential (api_key posted once, never echoed) |
| PUT    | /api/providers/{id}        | Update (optionally rotate api_key)              |
| DELETE | /api/providers/{id}        | Delete credential + ciphertext                   |
| POST   | /api/providers/test        | Validate a credential against the per-provider credential-test model. Returns a canonical `ProviderValidationResult` with a `category` field (`ok` / `auth_error` / `model_error` / `network_error` / `config_error` / `provider_error`). |

`/api/providers/test` uses a dedicated cheap test model
(`gpt-4.1-mini` / `claude-sonnet-4-6` by default) independent from the
production policy. For validating that the **runtime policy path** can
reach the provider, use `/api/smoke/ping` — it returns the same canonical
shape but routes through `resolve_model_policy` + the model alias layer.

## Projects

| Method | Path                                     | Purpose                       |
| ------ | ---------------------------------------- | ----------------------------- |
| GET    | /api/projects                            | List projects                 |
| POST   | /api/projects                            | Create project                |
| GET    | /api/projects/{id}                       | Get project (with brief)      |
| PUT    | /api/projects/{id}                       | Partial update                |
| PUT    | /api/projects/{id}/brief                 | Replace brief                 |
| GET    | /api/projects/{id}/funnel/summary        | Stage counts                  |

## Ideas

| Method | Path                                                | Purpose                                   |
| ------ | --------------------------------------------------- | ----------------------------------------- |
| GET    | /api/projects/{pid}/ideas?stage=S0                  | List ideas                                |
| POST   | /api/projects/{pid}/ideas/generate                  | Generate a batch via provider             |
| POST   | /api/projects/{pid}/ideas/score?stage=S0            | Score all ideas at a stage (structured_screening) |
| POST   | /api/projects/{pid}/ideas/advance                   | Promote top N ideas to next stage         |
| PUT    | /api/projects/{pid}/ideas/{id}/decision             | Keep/reject/promote/waive a single idea   |

## Specs / Runs / Analysis

| Method | Path                                                    | Purpose                      |
| ------ | ------------------------------------------------------- | ---------------------------- |
| GET    | /api/projects/{pid}/specs                               | List specs                   |
| POST   | /api/projects/{pid}/specs/generate                      | Generate spec for an idea    |
| GET    | /api/projects/{pid}/runs                                | List runs                    |
| POST   | /api/projects/{pid}/runs/start                          | Generate code + run          |
| GET    | /api/projects/{pid}/runs/{id}                           | Get run + artifacts + logs   |
| POST   | /api/projects/{pid}/runs/{id}/analyze                   | Analyze result → claims      |

## Drafts, Reviews, Packages, Sessions, Audit

| Method | Path                                                    | Purpose                       |
| ------ | ------------------------------------------------------- | ----------------------------- |
| GET    | /api/projects/{pid}/drafts                              | List manuscripts + drafts     |
| POST   | /api/projects/{pid}/drafts/generate                     | Evidence-first draft          |
| GET    | /api/projects/{pid}/drafts/claims/all                   | All claims                    |
| GET    | /api/projects/{pid}/reviews                             | List issues                   |
| POST   | /api/projects/{pid}/reviews/run                         | Run adversarial reviewers     |
| PUT    | /api/projects/{pid}/reviews/{id}                        | Update issue state / severity |
| GET    | /api/projects/{pid}/packages                            | List packages                 |
| POST   | /api/projects/{pid}/packages/build                      | Build new frozen package      |
| GET    | /api/projects/{pid}/packages/{id}                       | Package metadata              |
| GET    | /api/projects/{pid}/packages/{id}/download              | Download ZIP                  |
| GET    | /api/projects/{pid}/sessions                            | List sessions                 |
| POST   | /api/projects/{pid}/sessions                            | Log session                   |
| PUT    | /api/projects/{pid}/sessions/{id}                       | Update session                |
| DELETE | /api/projects/{pid}/sessions/{id}                       | Delete session                |
| GET    | /api/audit?project_id=...                               | Audit timeline                |

## Error handling

* 400 – validation error from Pydantic or business-rule failure
* 404 – record not found
* 409 – package missing zip path
* 410 – package zip was removed from disk
* 422 – Pydantic schema mismatch
