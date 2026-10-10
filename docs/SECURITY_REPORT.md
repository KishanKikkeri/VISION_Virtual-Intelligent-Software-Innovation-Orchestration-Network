# SECURITY_REPORT.md

Tooling used: `bandit -r services scripts benchmarks examples plugins`
(7,475 LOC scanned) plus targeted `grep` sweeps for patterns bandit
doesn't catch (hardcoded secrets, `shell=True`, `pickle`, `yaml.load`).

## Summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 14 |

## Medium

**M1 — Hardcoded temp directory.** `services/integration/api/production.py:167`
defaults `destination_dir` to `/tmp/aasc_backups` when the caller
doesn't supply one. On a multi-tenant host this is a predictable,
world-writable-by-default path (CWE-377). Not exploitable on its own
(no data is written there without an explicit backup request), but
worth tightening — see `REFACTORING_PLAN.md` S1.

## Low (14 findings, all subprocess-related or false-positive)

- **12× subprocess usage** (`scripts/install/{install,verify}.py`,
  `release_validation/final_qa.py`) — bandit flags any `subprocess`
  import/call by default. Reviewed each call site by hand: every one
  invokes a fixed, hardcoded argv list (`["docker", "--version"]`,
  `["alembic", "upgrade", "head"]`, `[sys.executable, "-m",
  "py_compile", ...]`) — **no user input reaches any subprocess
  argument**, and `shell=True` is never used. No action needed.
- **2× "hardcoded_password_string" (B105)** on `CheckStatus.PASS =
  "pass"` in both `production/release_models.py` and
  `release_validation/release_validation_models.py`. **False
  positive** — bandit pattern-matches the substring `pass` in an enum
  member name; this is a test-status enum, not a credential.

## Findings from targeted patterns (not bandit)

- `eval`/`exec`: **0 occurrences.**
- `pickle`: **0 occurrences.**
- `yaml.load` (unsafe form): **0 occurrences** — no `yaml` import at all in this slice.
- `shell=True`: **0 occurrences.**
- Hardcoded secrets/API keys/tokens (grepped for `api_key\s*=\s*["']`,
  `password\s*=\s*["']`, `SECRET`, `token\s*=\s*["']` literals):
  **0 occurrences** of an actual assigned literal. `deploy/k8s/secret.yaml.example`
  is explicitly named `.example` and contains placeholder values only.
- SQL injection: all DB access in this slice goes through SQLAlchemy
  Core/ORM constructs (`insert()`, `select()`, ORM attribute
  assignment) — no raw string-interpolated SQL found (`grep -rn "f\""
  combined with `execute(` produced no hits).
- Directory traversal: `plugin_loader.py`'s zip-bundle path
  (`_load_manifest_from_zip`'s sibling, `import_entrypoint`'s
  `zip_bundle` branch, confirmed at line 199) called
  `zf.extractall(temp_dir)` on the uploaded zip with **no per-member
  path validation**. Note for anyone reaching for the obvious-looking
  fix: `zipfile.ZipFile.extractall` has **no `filter=` parameter** —
  that safety mechanism (PEP 706) was added only to
  `tarfile.TarFile.extractall` in Python 3.12; `zipfile`'s signature
  is unchanged and still just `(path=None, members=None, pwd=None)`
  (verified via `inspect.signature`). A malicious plugin zip with a
  crafted member name (`../../evil.py`, or an absolute path) could
  write outside `temp_dir`. **Fixed in this pass** — see
  `REFACTORING_PLAN.md` S2 for the actual mitigation implemented
  (manual per-member path validation via a new `_safe_extractall`
  helper, not a stdlib parameter).

## Plugin trust boundary — already self-documented, worth elevating

`plugin_loader.py`'s own module docstring states plainly: loading a
plugin executes its entrypoint module's top-level code, and this
module does **not** sandbox that execution — no subprocess isolation,
no restricted builtins. `plugin_validator.py`'s permission
declarations are described as a manifest-level contract a caller can
choose to enforce (via `plugin_runtime.context_for_plugin`), not a
runtime sandbox.

This audit confirms that description is accurate: `import_entrypoint`
does call `importlib.util.module_from_spec` +
`spec.loader.exec_module` on the plugin's file with no restricted
`__builtins__`, no resource limits, and no process isolation. **This
is the single most consequential security fact about this codebase.**
It is already documented (`docs/M4.7_Plugin_SDK_Handover.md` §3 is
cited as carrying "what a production deployment should add here"), so
this is not a new finding — but it belongs in this report because an
"open-source production release" (the audit brief's framing) is
exactly the point at which "we documented the gap" needs to become
"we closed the gap or gated it," since third parties loading untrusted
plugins is the scenario the SDK exists for. See `REFACTORING_PLAN.md`
item S3 (Critical, Large effort — explicitly **not** attempted in this
pass; sandboxing a plugin runtime is a design decision, not a
mechanical fix).

## LLM / Agent injection surfaces

Not applicable in this slice — no LLM prompt-construction code or
agent runtime is present to review (see `PROJECT_AUDIT.md`'s scope
caveat). Flagged as an open item for whichever slice contains the
agent orchestration layer.
