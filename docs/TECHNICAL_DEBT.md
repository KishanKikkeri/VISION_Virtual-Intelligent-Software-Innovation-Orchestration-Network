# TECHNICAL_DEBT.md

Debt items found, ranked by how much they'll cost to defer further.
("Debt" here means "a real cost incurred by *not* fixing it now" —
distinct from `DEAD_CODE_REPORT.md`'s findings, which cost nothing to
leave, and from `ARCHITECTURE_REVIEW.md`'s opinions, which are
preferences, not costs.)

## D1 — `deploy/docker/Dockerfile` references a `requirements.txt` that doesn't exist (High)

Confirmed: `Dockerfile:16` does `COPY requirements.txt .` /
`RUN pip install --no-cache-dir -r requirements.txt`; no such file
exists anywhere in this repository (`find . -iname "requirements*.txt"`
→ zero results). **Anyone following this repo's own documented build
path today gets a build failure.** This is the single highest-cost
item in this report because it blocks the exact "open-source
production release" scenario the audit brief is written for — a new
contributor's first `docker build` fails. Fixed in this pass — see
`REFACTORING_PLAN.md` P1 and the caveat there about what the
reconstructed file can and can't guarantee.

## D2 — No secondary database indexes (Medium)

See `PERFORMANCE_REPORT.md` for the specific columns. Cost compounds
with data volume — cheap to fix now (an additive migration), expensive
to fix later (an index build on a large production table is a real
operational event). Not fixed in this pass — schema changes are
explicitly out of scope for an automated pass per the audit brief's
"never change behavior unless objectively a bug" rule; recommended as
the top item for a deliberate follow-up migration.

## D3 — Plugin loader has no execution sandbox (Critical, by design, already flagged)

See `SECURITY_REPORT.md`'s dedicated section. Listed here too because
it is genuine technical debt in the "cost of deferral compounds" sense
— every additional real-world plugin author who onboards before this
is addressed is one more reason the eventual fix becomes a breaking
change instead of a clean addition. Not fixed in this pass (Large
effort, architectural decision).

## D4 — `final_qa.py`'s importability-probe imports lack `pyflakes` suppression (Low)

See `DEAD_CODE_REPORT.md`'s correction. Cosmetic/tooling-hygiene debt
only — a future contributor running `pyflakes` in CI would get a false
positive and might "fix" it by deleting the import, silently breaking
the check it's meant to perform. Fixed in this pass (one-line
annotation) precisely to prevent that future mistake.

## D5 — No dependency manifest at all, beyond the (now reconstructed) `requirements.txt` (Medium)

Related to D1 but broader: there's no `pyproject.toml`, no lockfile, no
pinned transitive-dependency list anywhere in this slice. Every
version in the reconstructed `requirements.txt` (see
`DEPENDENCY_REVIEW.md`) is a floor (`>=`), not a pin — fine for
development, risky for a reproducible production build. Flagged, not
resolved (pinning exact versions requires actually resolving and
testing against them, which is out of scope for a static audit pass).

## D6 — `api/release.py`'s per-process history lists have no cap (Low)

See `PERFORMANCE_REPORT.md`. Not fixed — no evidence this endpoint
sees sustained polling in practice, and adding an arbitrary cap now
without knowing the real usage pattern risks silently dropping data an
operator actually wanted.

## Debt NOT found (worth stating)

- No TODO/FIXME/XXX/HACK comments anywhere in application code
  (`grep -rn "TODO\|FIXME\|XXX\|HACK"` → zero matches in
  `services/scripts/benchmarks/examples/plugins`). Every deferred
  decision in this codebase is written as prose in a docstring with
  its rationale, not a bare `# TODO`, which is a better pattern (a
  `TODO` with no explanation is itself a form of debt).
- No commented-out code blocks found.
- No version-pinning conflicts (nothing to conflict — see D5).
