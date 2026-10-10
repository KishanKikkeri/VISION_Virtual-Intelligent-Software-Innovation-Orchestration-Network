# QA_REPORT

Generated: 2026-07-19T15:08:59.609246+00:00

**4 passed / 0 failed / 3 skipped**

| Check | Status | Duration (ms) | Detail |
|---|---|---|---|
| workflow_validation | pass | 0.34 | validation_bridge.validate_layout importable |
| lint | pass | 207.38 | py_compile: no syntax errors |
| replay | skipped | 0.14 | execution_replay (M4.2) not available in this slice: No module named 'services.integration.execution_replay' |
| chaos | skipped | 0.06 | chaos_engineering (M4.5) not available in this slice: No module named 'services.integration.chaos_engineering' |
| security | skipped | 0.04 | security_hardening (M4.6) not available in this slice: No module named 'services.integration.security_hardening' |
| production | pass | 26.92 | environment_validator overall=pass |
| plugins | pass | 0.0 | plugin_sdk.plugin_validator importable |
