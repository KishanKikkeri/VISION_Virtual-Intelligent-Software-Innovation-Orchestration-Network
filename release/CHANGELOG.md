# Changelog

All notable changes to the AASC platform are documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [1.0.0-rc1] — M4.10 Final Platform Release & Launch

### Added
- `services/integration/release_validation/` — release scoring, compatibility
  matrix, dependency checking, environment audit, benchmark summary,
  unified readiness report, documentation generator, Final QA runner,
  release packaging/manifest, and `release_cli.py`.
- `scripts/install/` — `install.py`, `verify.py`, `doctor.py` installation
  wizard (Python, dependencies, Docker, Redis, Postgres, NATS checks).
- `docs/generated/` — auto-generated Architecture/API/Workflows/Agents/
  Events/Database/Plugins/Deployment/Security/Operations documentation.
- `examples/` — hello_world, ai_chatbot, bug_tracker, documentation_generator
  runnable sample projects.
- `plugins/examples/` — hello_plugin, metrics_plugin, notification_plugin
  sample plugins demonstrating manifests, permissions, hooks, lifecycle.
- `benchmarks/` — benchmark runner producing `benchmark.json`/`benchmark.md`.
- `release/` — this changelog, release notes, LICENSE, NOTICE, VERSION,
  and `release_manifest.json`.
- `/platform/release/*` API surface exposing readiness, docs generation,
  QA, and benchmark endpoints.
- Dashboard: Release section (readiness score, workflow/plugin counts,
  security posture, chaos score, production score, version, git commit,
  build date).
- CI: release-validation, docs-generation, and packaging steps added to
  the existing GitHub Actions workflow.

### Changed
- Nothing in the existing M3.1–M4.9 surface was modified. This milestone
  is additive-only per its mission statement.

### Known limitations
See `docs/M4.10_Final_Release_Handover.md` §4 for the full list.

## [Unreleased] — prior milestones
See `docs/M4.9_Production_Readiness_Handover.md`, `docs/M4.8_Workflow_
Designer_Handover.md`, and `docs/M4.7_Plugin_SDK_Handover.md` for the
full history of M3.1 through M4.9.
