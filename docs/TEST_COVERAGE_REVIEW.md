# TEST_COVERAGE_REVIEW.md

## Headline numbers

- **714 tests passing, 0 failing** (566 pre-M4.10 + 148 from M4.10's own suite), confirmed by running `pytest tests/ -q` in this pass.
- 4 test files, one per milestone slice present (`test_m47_plugin_sdk.py`, `test_m48_workflow_designer.py`, `test_m49_production_readiness.py`, `test_m4_10_final_release.py`).

## Coverage by layer (qualitative — no coverage.py run in this pass, see gap below)

Every package's pure-model layer (`*_models.py`) has direct
property/computed-field tests (verified by sampling: `ReleaseScore.
grade`, `EnvironmentAuditReport.overall_status`, `QAReport.passed` all
have dedicated tests). Every CLI entrypoint has at least one
exit-code-checking test. Every API router has at least one `TestClient`
round-trip per endpoint (confirmed for `api/release.py`: all 12
endpoints have a corresponding test).

**Thinner spots, found by inspection, not fixed in this pass** (per
audit brief: "never... reduce test coverage" is about not removing
existing coverage; adding net-new tests is itself feature-adjacent
work the brief asks to defer to a deliberate follow-up, not bundle
into an audit-and-cleanup pass):

- `scripts/install/install.py`'s `main()` (the actual installer
  entrypoint, as opposed to `verify.py`'s checks which are well
  tested) has no direct test — only `verify.py` and `doctor.py` are
  exercised. `install.py`'s `apply_fixes()`/`run_migrations()` invoke
  real `subprocess.run` calls (`pip install`, `alembic upgrade head`)
  that a test would need to mock; nothing in the current suite mocks
  them.
- `release_export.py`'s fallback path (when M4.9's `production_export`
  is *not* importable) is untested — every existing test runs in an
  environment where M4.9's package *is* importable, so the `except
  Exception` fallback branch (JSON-only, `_m49_export_report = None`)
  has never actually executed under test.
- `documentation_generator.py`'s per-generator functions
  (`generate_architecture`, `generate_api`, etc.) are covered
  collectively via `generate_all()` but not individually asserted
  against specific expected content beyond "contains this substring" —
  a generator that started returning technically-non-empty but wrong
  content (e.g. the wrong section header) wouldn't be caught.

## Weak assertions

Scanned for tests whose only assertion is a truthy/non-None check with
no value verification (a common "coverage theater" pattern). Sampled
20 tests across the M4.10 suite at random — all had specific value
assertions (`== "1.0.0-rc1"`, `.grade == "release_candidate"`, exact
counts), not bare truthiness. No systematic weak-assertion pattern
found.

## Duplicate tests

None found — each test function in `test_m4_10_final_release.py`
targets a distinct code path (verified: no two test names differ only
by a trivial rename with identical bodies).

## Flaky tests

None observed across 3 consecutive full-suite runs in this session (a
crude but real flakiness check, not a substitute for a proper CI
flake-detection job). The suite has no `time.sleep`-based
synchronization, no wall-clock-dependent assertions beyond checking
`generated_at` is a non-empty string, and no test-order dependency
(confirmed by running `pytest tests/ -q -p no:randomly`-equivalent —
this repo doesn't have `pytest-randomly` installed, so true
order-independence wasn't verified with a randomized run in this pass;
flagged as a gap).

## Mock overuse

Reviewed for tests that mock so much of the system under test that
they no longer test real behavior. **Not found** — the M4.10 suite
deliberately favors real integration over mocking (e.g. the plugin
tests in Layer 17 use the real `plugin_loader.discover_plugins` +
`plugin_runtime.dispatch_hook` against real files on disk, not a
mocked loader). `monkeypatch` is used exactly twice (both in
`scripts/install/verify.py`'s test layer, to simulate an old Python
version and a missing `docker` binary) — both are legitimate uses
(can't actually change the interpreter version or uninstall Docker to
test the failure path).

## Coverage tooling gap

`coverage.py`/`pytest-cov` is not installed or run in this pass or in
`.github/workflows/ci.yml` — line/branch coverage percentages in this
report are qualitative (by code inspection), not measured.
**Recommend adding `pytest --cov` to the CI `unit-tests` job** so
future coverage claims in a report like this one can cite an actual
percentage instead of "found by inspection." Not added in this pass to
avoid mixing a CI-config change into an audit-and-cleanup response
without the project owner's sign-off on the added CI runtime cost.
