"""
Shared library of project-memory presets.

These are curated, opinionated convention sets every developer on the team can
pick from. They are served read-only via GET /api/context/memory/presets and are
inserted (appended) into the Global Project Memory, which is injected into every
prompt for every session and every user.

Grounded in widely-adopted standards: OWASP Top 10 / ASVS, the 12-Factor App,
Conventional Commits, clean-code practice, and the dominant patterns observed
across 100+ public AI coding-rule files.
"""

MEMORY_PRESETS = [
    {
        "id": "security",
        "icon": "🔒",
        "title": "Security & InfoSec",
        "category": "infosec",
        "description": "OWASP-aligned secure-coding defaults",
        "content": """# Security & InfoSec

- Treat all input as untrusted: validate, sanitize, and enforce types/lengths at every boundary.
- Use parameterized queries / prepared statements — never build SQL or shell commands via string concatenation.
- Escape output for its context (HTML, attribute, URL, JS) to prevent XSS; rely on framework auto-escaping.
- Authenticate then authorize on every protected route; check object-level ownership (no IDOR).
- Store secrets in environment/secret managers — never in source, logs, or client code. Rotate on exposure.
- Hash passwords with bcrypt/argon2 (never MD5/SHA1); use constant-time comparison for tokens.
- Enforce HTTPS/TLS everywhere; set secure, HttpOnly, SameSite cookies and a strict CSP.
- Apply least privilege to DB users, API scopes, and cloud IAM roles.
- Never log secrets, tokens, PII, or full card/SSN numbers; redact sensitive fields.
- Keep dependencies patched; fail the build on known critical CVEs.
""",
    },
    {
        "id": "backend",
        "icon": "🛠️",
        "title": "Backend & API Design",
        "category": "backend",
        "description": "Robust, predictable services & REST APIs",
        "content": """# Backend & API Design

- Handle errors and edge cases first with guard clauses and early returns; keep the happy path last.
- Use specific, custom error types and structured error responses ({ code, message }) — not bare exceptions.
- Validate request bodies with a schema layer (Pydantic / Zod) before any business logic runs.
- Return correct HTTP status codes (400/401/403/404/409/422/500) and never leak stack traces to clients.
- Make write endpoints idempotent where possible; use idempotency keys for payments and retries.
- Paginate list endpoints (cursor or limit/offset); never return unbounded result sets.
- Keep controllers thin — push business logic into services; keep functions small and single-purpose.
- Wrap multi-step writes in transactions; ensure rollback on failure.
- Log with structured context (request id, user id) at appropriate levels; no secrets in logs.
- Version public APIs and avoid breaking changes without a deprecation path.
""",
    },
    {
        "id": "frontend",
        "icon": "🎨",
        "title": "Frontend (React / TypeScript)",
        "category": "frontend",
        "description": "Modern, accessible, type-safe UI",
        "content": """# Frontend (React / TypeScript)

- Use TypeScript everywhere; type props with interfaces and avoid `any`. Enable strict mode.
- Prefer functional components and hooks; keep components small and composable.
- Use functional, declarative patterns; favor iteration and composition over duplication.
- Name with intent — booleans read as predicates (isLoading, hasError, canSubmit).
- Lift state only as high as needed; keep derived data computed, not stored.
- Make every interactive element accessible: semantic HTML, labels, focus states, keyboard support, ARIA only when needed.
- Handle loading, empty, and error states explicitly for every async view.
- Optimize Core Web Vitals: code-split routes, lazy-load non-critical UI, use modern image formats.
- Never trust client state for authorization; re-check on the server.
- Keep side effects in hooks with correct dependency arrays; clean up subscriptions.
""",
    },
    {
        "id": "devops",
        "icon": "⚙️",
        "title": "DevOps & CI/CD",
        "category": "devops",
        "description": "12-Factor config, pipelines & reliability",
        "content": """# DevOps & CI/CD

- Follow 12-Factor: configuration via environment variables, never hardcoded per-environment values.
- Builds must be reproducible and immutable; pin dependency versions and lockfiles.
- CI must pass (lint + type-check + tests) before merge; protect the main branch.
- Run linters/formatters in pre-commit hooks so style never reaches review.
- Treat infrastructure as code; review infra changes like application code.
- Ship small, frequent changes behind feature flags; keep deploys reversible with fast rollback.
- Expose health/readiness checks; define resource limits and timeouts.
- Centralize logs, metrics, and traces; alert on SLOs, not on noise.
- Keep secrets in a secret manager injected at deploy time — never committed.
- Automate database migrations and make them backward-compatible (expand/contract).
""",
    },
    {
        "id": "best-practices",
        "icon": "✅",
        "title": "Engineering Best Practices",
        "category": "best practices",
        "description": "Clean-code fundamentals for any stack",
        "content": """# Engineering Best Practices

- Optimize for readability first; code is read far more often than it is written.
- Keep functions small and single-purpose; one reason to change each.
- Use descriptive, intention-revealing names; avoid abbreviations and magic numbers.
- Don't repeat yourself — extract shared logic, but avoid premature abstraction.
- Fail fast and clearly: validate early, raise meaningful errors, never swallow exceptions silently.
- Comment the *why*, not the *what*; let clear code explain the *how*.
- Delete dead code and commented-out blocks; rely on version control for history.
- Make illegal states unrepresentable; prefer immutability and pure functions where practical.
- Leave code cleaner than you found it (boy-scout rule), but keep refactors separate from features.
- Don't apologize for mistakes in output — just fix them; mark gaps with TODO comments.
""",
    },
    {
        "id": "testing",
        "icon": "🧪",
        "title": "Testing & QA",
        "category": "testing",
        "description": "Meaningful, automated test discipline",
        "content": """# Testing & QA

- Write meaningful automated tests; treat testing as part of the work, not an afterthought.
- Cover the happy path, edge cases, and error/failure cases for every unit of behavior.
- Structure tests as Arrange–Act–Assert; one logical assertion focus per test.
- Test behavior and public contracts, not private implementation details.
- Keep tests fast, deterministic, and isolated — no shared mutable state or real network calls.
- Use the test pyramid: many unit tests, fewer integration, fewest end-to-end.
- All tests must pass before merge; never disable a failing test to go green.
- Add a regression test for every bug fixed.
- Mock external services at the boundary; assert on inputs and outputs, not internals.
- Keep meaningful coverage, but prioritize critical paths over a coverage percentage.
""",
    },
    {
        "id": "python",
        "icon": "🐍",
        "title": "Python",
        "category": "backend",
        "description": "Idiomatic, typed, modern Python",
        "content": """# Python

- Follow PEP 8; format with black and lint with ruff/flake8.
- Add type hints to all function signatures; check with mypy/pyright.
- Validate external data with Pydantic models or dataclasses.
- Use f-strings for formatting and pathlib for filesystem paths.
- Prefer comprehensions and generators over manual loops where they read clearly.
- Use context managers (`with`) for files, locks, and connections.
- Catch specific exceptions, never bare `except:`; let unexpected errors propagate.
- Keep functions pure where possible; avoid mutable default arguments.
- Manage dependencies in a virtualenv with a pinned lockfile.
- Write docstrings for public modules, classes, and functions.
""",
    },
    {
        "id": "aws-lambda",
        "icon": "☁️",
        "title": "Cloud / AWS Lambda",
        "category": "devops",
        "description": "Serverless functions — secure, lean, resilient",
        "content": """# Cloud / AWS Lambda & Serverless

- Keep the handler thin: parse/validate the event, then delegate to a separate, testable business-logic module.
- Use `async` handlers with async/await — do not mix the legacy callback signature; return the response, never call `callback`.
- Initialize reusable resources (DB pools, SDK clients, secrets) OUTSIDE the handler so they are cached across warm invocations.
- Validate and type every incoming event (API Gateway, SQS, S3, EventBridge) before use; never trust the payload shape.
- Apply least-privilege IAM: scope each function's role to the exact actions/resources it needs — no wildcard `*` policies.
- Never hardcode secrets; load them from AWS Secrets Manager or SSM Parameter Store, and cache them outside the handler.
- Make handlers idempotent (use a dedup/idempotency key) — Lambda can deliver the same event more than once.
- For SQS/stream sources, report partial batch failures (`batchItemFailures`) so only failed records are retried.
- Set explicit timeout, memory, and reserved/maximum concurrency; configure a dead-letter queue or on-failure destination.
- Use structured JSON logging to CloudWatch with a request/correlation id; never log secrets, tokens, or PII.
- Minimize cold starts: trim the deploy package, lazy-load heavy deps, and keep the runtime current.
- Return the exact response contract the trigger expects (e.g. `{ statusCode, headers, body }` for API Gateway) with correct status codes.

## API security & quality baseline (applies to every endpoint this function exposes)
- AuthN then AuthZ on every route; enforce object-level ownership (no IDOR).
- Validate input against a schema; reject unknown fields; enforce types, lengths, and ranges.
- Return correct HTTP status codes and structured errors `{ code, message }`; never leak stack traces.
- Rate-limit and set sane request size limits; enable a strict CORS allowlist (no `*` with credentials).
- Parameterize all DB/queries; escape output; keep dependencies patched against known CVEs.
""",
    },
    {
        "id": "node-express",
        "icon": "🟢",
        "title": "Node / Express API",
        "category": "backend",
        "description": "Express APIs — async-correct, secure by default",
        "content": """# Node / Express API Creation

## Async correctness
- Use async/await everywhere; never block the event loop with sync APIs (`fs.readFileSync`, `crypto.*Sync`, heavy CPU) inside a request path.
- Run INDEPENDENT async work concurrently with `Promise.all` / `Promise.allSettled` — do not `await` in a sequential loop when the calls don't depend on each other.
  - Use `Promise.all` when all must succeed (it rejects on the first failure).
  - Use `Promise.allSettled` when you need every result regardless of individual failures.
  - Use `Promise.race`/`AbortController` to enforce timeouts on external calls.
- Wrap every async route handler so rejections reach Express: use `express-async-errors` or a `wrapAsync(fn)` helper — an unhandled promise rejection must never crash the process.
- Always handle promise rejections; never leave a floating promise. Add a process-level `unhandledRejection` guard as a backstop only.

## Structure & errors
- Keep routes thin → controllers → services; put business logic in services, not in route handlers.
- Define ONE centralized error-handling middleware (the 4-arg `(err, req, res, next)`) and forward all errors to it via `next(err)`.
- Use typed/custom error classes (e.g. `AppError` with a status code); return structured JSON `{ code, message }` and never expose stack traces in production.
- Return correct status codes (400/401/403/404/409/422/429/500); validate request bodies with zod/joi/celebrate BEFORE business logic runs.
- Paginate list endpoints; wrap multi-step writes in transactions; make writes idempotent where possible.
- Implement graceful shutdown (drain the server, close DB pools on SIGTERM).

## Security baseline (every endpoint, no exceptions)
- AuthN then AuthZ on each protected route; enforce object-level ownership (no IDOR).
- Add `helmet` for secure headers, `express-rate-limit` for abuse protection, and a strict CORS allowlist (never `*` with credentials).
- Treat all input as untrusted: validate/sanitize, enforce body-size limits, and use parameterized queries — never string-concatenate SQL/NoSQL.
- Store secrets in env/secret manager (never in source or logs); hash passwords with bcrypt/argon2; set Secure, HttpOnly, SameSite cookies.
- Never log secrets, tokens, or PII; keep dependencies patched and fail the build on critical CVEs.
- Use structured logging (pino/winston) with a request id; return generic messages to clients while logging detail server-side.
""",
    },
    {
        "id": "git",
        "icon": "📦",
        "title": "Git & Commits",
        "category": "best practices",
        "description": "Clean history & reviewable PRs",
        "content": """# Git & Commits

- Use Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- Write imperative, concise subject lines (~50 chars); explain the *why* in the body.
- Make atomic commits — one logical change each; don't mix refactors with features.
- Keep pull requests small and focused; large PRs hide bugs and stall review.
- Write clear PR descriptions with context, screenshots, and testing notes.
- Never commit secrets, large binaries, or commented-out code; use .gitignore.
- Rebase or squash to keep history readable; don't merge broken commits to main.
- Reference issue/ticket IDs in commits or PRs for traceability.
""",
    },
]
