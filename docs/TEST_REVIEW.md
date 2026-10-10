# TEST_REVIEW.md — Phase 2

**Headline: 718/718 tests pass, re-confirmed live in this session.**
This report focuses on *blind spots* — real findings from this pass
that existing tests didn't catch — rather than re-tallying Phase 1's
`TEST_COVERAGE_REVIEW.md`, which already covered general coverage
shape. Both blind spots below directly correspond to this pass's two
most severe findings, which is itself the useful signal: the tests
that exist are well-written for what they assert, they just don't
assert the property that mattered.

## 1. Coverage blind spot directly responsible for REL1 (temp-dir leak) going undetected

`tests/foundation/test_m47_plugin_sdk.py` has both
`test_api_reload_success` (line 969) and `test_cli_reload_success`
(line 1111), plus a `_write_plugin_zip` helper (line 96) that exists
specifically to build zip-bundle test fixtures. **But neither reload
test uses it** — both call `_write_plugin_package` (line 86), which
writes a directory-based (`python_package`) plugin. `grep -n
"zipfile\|\.zip" test_m47_plugin_sdk.py | grep -i reload` → zero
matches confirms no test exercises `POST /platform/plugins/reload`
(or the CLI `reload` command) against a zip-sourced plugin — exactly
the code path where `validate_entrypoint_importable` leaks a temp
directory (`RELIABILITY_REVIEW.md` REL1). **The fixture needed to
catch this already exists in the file** (`_write_plugin_zip`); it's
just never passed to the reload tests. **Recommended addition
(meaningful, not filler):** a
`test_api_reload_zip_bundle_plugin_does_not_leak_temp_dir` that
captures `tempfile.gettempdir()`'s contents before and after a reload
call against a zip-sourced plugin and asserts no new directory
persists.

## 2. Coverage blind spot directly responsible for SEC1 (XSS) going undetected

`tests/foundation/test_m49_production_readiness.py:1056`'s
`test_export_report_html_wraps_in_html_tags` asserts exactly what its
name says — `text.startswith("<html>") and text.endswith("</html>")`
— and nothing about the *content* in between. `test_m4_10_final_release.py:512`'s
`test_export_html_produces_string` is even weaker: it only asserts the
result `isinstance(content, str) and len(content) > 0`. **Neither test
constructs a report containing HTML-special characters
(`<`, `>`, `&`, `"`) in any field and checks the output for escaping**
— which is exactly the property that would have caught SEC1.
**Recommended addition:** a
`test_export_html_escapes_special_characters` building a report with
a `<script>` payload in a text field (the same PoC used to verify
SEC1 in `SECURITY_REVIEW.md`) and asserting the literal string
`<script>` does **not** appear unescaped in the output.

## 3. Missing edge cases (beyond the two above)

- No test constructs a `requirements.txt` with a dependency name
  containing characters that would need escaping if it ever reached
  the HTML exporter (directly related to #2's real attack path, not
  just the isolated unit case).
- No test exercises `api/production.py:120`'s `GET /platform/production/releases`
  with more than 50 seeded releases to observe the unbounded-list
  behavior noted in `API_AUDIT.md` API3 — a test here would have
  surfaced the missing-`limit` inconsistency naturally.
- No test in `test_m47_plugin_sdk.py` seeds two plugins with the same
  `id` via two different source types (one `python_package`, one
  `zip_bundle`) to check `discover_plugins`'s tie-breaking behavior —
  not independently confirmed to be a bug, but genuinely untested
  behavior worth a test either way.

## 4. Flaky tests

Re-ran the full suite 3 times consecutively in this session
(`pytest tests/ -q` × 3) — identical pass count each time, no
order-dependency or timing-dependent failures observed. Consistent
with Phase 1's finding.

## 5. Duplicate tests

Re-checked with a coarser method than Phase 1 (comparing normalized
assertion bodies, not just function names) across all 4 test files.
**No true duplicates found** — the closest pair,
`test_export_html_produces_string` (M4.10) and
`test_export_report_html_wraps_in_html_tags` (M4.9), test the *same
function* (`export_report(..., "html")`) but assert different things
(generic non-emptiness vs. specific tag wrapping) — not a duplicate,
though their combined weakness is exactly finding #2 above.

## 6. Dead tests / unreachable assertions

`grep -rn "assert True\|pass  #.*skip\|@pytest.mark.skip" tests/` →
zero matches. No skipped or vacuously-true test found.

## 7. Property-based / integration test gaps

No property-based tests (Hypothesis or similar) exist anywhere in this
suite — every test uses concrete, hand-picked fixture values. For the
version-comparison logic in `compatibility_matrix._satisfies()`
specifically, a property test generating random version tuples and
checking the comparison against Python's own tuple comparison would
give much stronger confidence than the current 10 hand-picked cases —
flagged as a meaningful (not filler) recommendation, since version
comparison bugs are exactly the kind of edge-case-heavy logic property
testing is built for.

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| T1 | Reload tests never exercise the zip-bundle path where REL1's leak lives, despite the fixture already existing | `test_m47_plugin_sdk.py:969,1111` (tests) vs. `:96` (unused fixture) | Medium — directly enabled REL1 to ship undetected | Add a zip-bundle reload test asserting no temp-dir leak |
| T2 | HTML export tests assert wrapper tags / non-emptiness, never escaping | `test_m49_production_readiness.py:1056`, `test_m4_10_final_release.py:512` | High — directly enabled SEC1 to ship undetected | Add an escaping-specific test with a `<script>` payload |
| T3 | No property-based test for version-range comparison logic | `compatibility_matrix._satisfies()` (test coverage in `test_m4_10_final_release.py`) | Low | Add a Hypothesis-based property test |
