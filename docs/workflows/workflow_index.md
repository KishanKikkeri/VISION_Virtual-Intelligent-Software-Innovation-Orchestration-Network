# Workflow Index

| Workflow | Purpose | Healthy | Nodes | Edges | Interrupts | Parallel | Depth |
|---|---|---|---|---|---|---|---|
| [architecture](architecture.md) | Turns approved product requirements into a reviewed, vers... | ✓ | 18 | 22 | 1 | 1 | 11 |
| [devops](devops.md) | Provisions infrastructure and deploys reviewed artifacts,... | ✓ | 14 | 22 | 1 | 0 | 11 |
| [engineering](engineering.md) | Implements a feature: task breakdown, parallel backend/fr... | ✓ | 13 | 20 | 0 | 1 | 8 |
| [incident_response](incident_response.md) | Reacts to raised alerts: triage, diagnosis, remediation, ... | ✓ | 7 | 7 | 0 | 0 | 6 |
| [manager_delegation](manager_delegation.md) | Per-task delegation loop: select department/agent/model, ... | ✓ | 13 | 16 | 0 | 0 | 10 |
| [manager_lifecycle](manager_lifecycle.md) | The project's end-to-end phase state machine — intake thr... | ✓ | 21 | 37 | 3 | 0 | 14 |
| [monitoring](monitoring.md) | Continuously collects infrastructure/application/log/trac... | ✓ | 7 | 7 | 0 | 0 | 6 |
| [qa](qa.md) | Generates and executes unit/integration/regression/perfor... | ✓ | 18 | 24 | 0 | 2 | 13 |
| [repository](repository.md) | Source-control operations — branch, commit, PR, approval ... | ✓ | 13 | 38 | 1 | 0 | 9 |
| [security](security.md) | Runs dependency, secret, and compliance scans over Engine... | ✓ | 16 | 22 | 0 | 1 | 11 |

_Note: `product` and `docs` departments have no LangGraph workflow of their own (confirmed in services/integration/lifecycle.py's `_graph_registry()` docstring) and are intentionally absent from this index rather than represented with an invented graph. `manager` is split into two independent graphs — `manager_lifecycle` and `manager_delegation` — rather than a single `manager` entry._
