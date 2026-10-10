# AASC Platform — v1.0.0 Release Candidate

**Release date:** 2026-07-19
**Codename:** M4.10 Final Platform Release & Launch

## Summary

The AASC platform reached feature completeness at M4.9. This release
turns that feature-complete platform into a v1.0.0 Release Candidate:
release validation tooling, generated documentation, an installation
wizard, runnable example projects and sample plugins, a benchmark
suite, formal release packaging, and an automated Final QA sweep.

No runtime behavior, database schema, API contract, or workflow
changed in this release — see `release/CHANGELOG.md` for the itemized
list of additions.

## Getting started

```
python scripts/install/install.py
python examples/hello_world/main.py
python services/integration/release_validation/release_cli.py check 1.0.0-rc1
```

## Upgrade notes

This release is purely additive on top of M4.9. Existing deployments
require no data migration and no configuration changes to keep
functioning as before; the new `/platform/release/*` endpoints and
dashboard Release section are opt-in additions.

## Known limitations

This is a **Release Candidate**, not a final GA build. See
`docs/M4.10_Final_Release_Handover.md` §4 "Known limitations / deferred
work" for the full list — most notably: several benchmark metrics
(agent latency, replay performance, true workflow-engine speed) are not
measurable in this build's environment because the real LangGraph
runtime and M4.1/M4.2 modules are outside this build's scope; QA's
chaos/security/replay checks report `SKIPPED` rather than `PASS` for
the same reason.

## Roadmap

See `docs/M4.10_Final_Release_Handover.md` §6 "Future roadmap."
