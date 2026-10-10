# REFACTORING_PLAN.md

Every item below cites the report it came from. "Implemented in this
pass" items were fixed and verified against the full test suite;
everything else is a scoped recommendation for a deliberate follow-up,
per the audit brief's "never change behavior unless objectively a bug"
constraint.

## Implemented in this pass

| ID | Item | Source | Risk | Verification |
|---|---|---|---|---|
| P1 | Reconstruct `requirements.txt` so `Dockerfile`'s documented build path works | `TECHNICAL_DEBT.md` D1 | Low — additive file, doesn't change any Python behavior | `pytest tests/ -q` unaffected (no code changed) |
| DC1 | Remove 22 confirmed-dead imports (of 24 `pyflakes` findings) | `DEAD_CODE_REPORT.md` | Low — unused names, no references | Full suite re-run after each batch |
| DC2 | Add `# noqa: F401` to `final_qa.check_plugins()`'s importability-probe import, matching the existing convention in `environment_validator.py` | `DEAD_CODE_REPORT.md` correction | None — comment-only | N/A |
| S2 | Validate every zip member's resolved path stays within the target extraction directory before extracting (new `_safe_extractall` helper in `plugin_loader.py`); `zipfile` has no `filter=` parameter to lean on (unlike `tarfile` post-PEP 706 — verified, see `SECURITY_REPORT.md`), so this is manual `os.path.realpath` validation, reject-whole-bundle-on-first-violation | `SECURITY_REPORT.md` | Low — additive validation; legitimate zips extract identically (verified with a real zip in a live test), malicious ones now raise `UnsafeZipMemberError` instead of extracting | New tests: `test_safe_extractall_extracts_legitimate_zip_bundle`, `test_safe_extractall_rejects_path_traversal_member`, `test_safe_extractall_rejects_absolute_path_member`, `test_import_entrypoint_zip_bundle_still_works_end_to_end` (143/143 `plugin_sdk` tests pass, 714/714 full suite) |
| S1 | `api/production.py`'s backup-destination default: keep `/tmp/aasc_backups` but document it's a default, not a hardcoded absolute requirement — **on inspection this is already caller-overridable** (`body.get("destination_dir", "/tmp/aasc_backups")`), so the real fix is documentation, not code. Added a one-line comment. | `SECURITY_REPORT.md` M1 | None | N/A |

## Recommended, not implemented (ranked)

| ID | Item | Severity | Effort | Impact | Source |
|---|---|---|---|---|---|
| D2 | Add secondary indexes on `created_at` (backup/restore records) and `workflow_name` (layouts) via a new additive Alembic migration | High | S | Prevents a real future full-table-scan scaling cliff; cheap now, expensive once tables are large | `PERFORMANCE_REPORT.md` |
| D3 | Design and implement plugin execution sandboxing (subprocess/gVisor isolation) before accepting untrusted third-party plugins | Critical | L | Closes the single largest security gap in this codebase; already scoped by the M4.7 handover doc | `SECURITY_REPORT.md` |
| D5 | Pin exact dependency versions (replace `>=` floors with `==` pins) after resolving and testing against a real environment | Medium | S | Reproducible builds; currently a `pip install` today vs. next month could pull different transitive versions | `DEPENDENCY_REVIEW.md` |
| PF1 | Same as D2 — listed once, cross-referenced | High | S | — | `PERFORMANCE_REPORT.md` |
| PF3 | Cap or evict `api/release.py`'s per-process history lists | Low | S | Bounds memory under sustained automated polling (not observed today) | `PERFORMANCE_REPORT.md` |
| SEC-AUDIT | Run `pip-audit`/`safety` against the pinned dependency set (post-D5) in CI | Medium | S | Ongoing CVE visibility this static pass couldn't provide (no network access to a CVE feed) | `DEPENDENCY_REVIEW.md` |
| COV1 | Add `pytest-cov` to CI and publish an actual coverage percentage | Low | S | Replaces this report's qualitative coverage estimate with a real number | `TEST_COVERAGE_REVIEW.md` |
| COV2 | Add a test for `scripts/install/install.py`'s `main()` (mock `subprocess.run` for the `apply_fixes`/`run_migrations` paths) | Low | S | Closes the one CLI entrypoint with zero direct test coverage | `TEST_COVERAGE_REVIEW.md` |
| COV3 | Add a test that forces `release_export.py`'s `_m49_export_report = None` fallback path | Low | S | Currently-unexecuted branch under test | `TEST_COVERAGE_REVIEW.md` |
| ARCH1 | Refactor `assemble_platform_dashboard()`'s growing parameter list into a `cards: Dict[str, BaseModel]` shape | Low | M | Readability at the next milestone's card addition, not today | `ARCHITECTURE_REVIEW.md` |
| API1 | Add a shared `_error_response()` helper across `api/*.py` to remove ~15 duplicated `HTTPException` call shapes | Low | S | Minor duplication reduction | `ARCHITECTURE_REVIEW.md` |

## Explicitly declined (and why)

- **Splitting `dashboard_builder.py` into per-card files.** Inspected
  and found to be breadth, not depth — see `ARCHITECTURE_REVIEW.md`.
  Splitting it would be stylistic churn the audit brief prohibits.
- **"Fixing" the two importability-probe imports by deleting them.**
  Would silently remove the plugin-availability check those functions
  exist to perform — see `DEAD_CODE_REPORT.md`'s correction. This is
  exactly the kind of automated "fix" the audit brief's "never change
  behavior unless objectively a bug" rule is meant to prevent; caught
  and corrected during this pass before being applied.
- **Auto-adding database indexes.** A schema change with real
  migration-ordering/rollout implications belongs to a deliberate
  decision by the project owner, not an automated audit pass — flagged
  at High severity instead (D2/PF1).
