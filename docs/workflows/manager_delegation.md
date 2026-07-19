# Workflow: manager_delegation

**Status:** ✓ healthy

## Purpose

Per-task delegation loop: select department/agent/model, monitor progress, retry or escalate, validate completion.

## Nodes

- **Entry:** `select_department`
- **Finish:** `__end__`
- **All nodes (13):** `__end__`, `__start__`, `assign_task`, `collect_results`, `dead_letter`, `escalate_task`, `handle_retry`, `monitor_progress`, `select_agent`, `select_department`, `select_model`, `task_complete`, `validate_completion`

## Routing Table

| Source Node | Routing Function | Outcome | Target |
|---|---|---|---|
| validate_completion | route_after_validation | complete | task_complete |
| validate_completion | route_after_validation | dead_letter | dead_letter |
| validate_completion | route_after_validation | retry | handle_retry |
| handle_retry | route_escalation | dead_letter | dead_letter |
| handle_retry | route_escalation | escalate | escalate_task |

## Parallel Branches

_No parallel branches._

## Interrupt Nodes

_None._

## Diagram

```mermaid
graph TD
    START([START])
    select_department[select_department]
    select_agent[select_agent]
    select_model[select_model]
    assign_task[assign_task]
    monitor_progress[monitor_progress]
    collect_results[collect_results]
    validate_completion{{validate_completion}}
    handle_retry{{handle_retry}}
    escalate_task[escalate_task]
    dead_letter[dead_letter]
    task_complete[task_complete]
    END([END])
    START --> select_department
    assign_task --> monitor_progress
    collect_results --> validate_completion
    escalate_task --> select_agent
    handle_retry -->|dead_letter| dead_letter
    handle_retry -->|escalate| escalate_task
    handle_retry --> select_agent
    monitor_progress --> collect_results
    select_agent --> select_model
    select_department --> select_agent
    select_model --> assign_task
    validate_completion -->|dead_letter| dead_letter
    validate_completion -->|retry| handle_retry
    validate_completion -->|complete| task_complete
    dead_letter --> END
    task_complete --> END
```

## Statistics

| Metric | Value |
|---|---|
| Nodes | 13 |
| Edges | 16 |
| Graph depth | 10 |
| Average branching factor | 1.33 |
| Reachability | 100.0% |
| Dead ends | 0 |
| Cycles detected | 2 |
| Interrupt nodes | none |
| Checkpoint-capable | yes |
| Parallel branches | 0 |


## Warnings

_None._

## Errors

_None._
