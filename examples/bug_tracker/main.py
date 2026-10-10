"""
examples/bug_tracker/main.py
=================================
M4.10 §5 — minimal in-memory bug tracker. Demonstrates the platform's
own "pure model + pure functions" layering convention (see any M4.x
`*_models.py` / core module pair) at teaching scale: one `BugReport`
Pydantic model, a few pure functions over a list of them, and a tiny
CLI demo run at the bottom. No database — swap `_STORE` for a real
repository to graduate this into a platform service.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Status(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class BugReport(BaseModel):
    id: int
    title: str
    description: str = ""
    severity: Severity = Severity.MEDIUM
    status: Status = Status.OPEN
    reported_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    assignee: Optional[str] = None


def file_bug(store: List[BugReport], title: str, description: str = "", severity: Severity = Severity.MEDIUM) -> BugReport:
    next_id = (max((b.id for b in store), default=0)) + 1
    bug = BugReport(id=next_id, title=title, description=description, severity=severity)
    store.append(bug)
    return bug


def assign_bug(store: List[BugReport], bug_id: int, assignee: str) -> Optional[BugReport]:
    for bug in store:
        if bug.id == bug_id:
            bug.assignee = assignee
            bug.status = Status.IN_PROGRESS
            return bug
    return None


def resolve_bug(store: List[BugReport], bug_id: int) -> Optional[BugReport]:
    for bug in store:
        if bug.id == bug_id:
            bug.status = Status.RESOLVED
            return bug
    return None


def triage_summary(store: List[BugReport]) -> dict:
    summary = {s.value: 0 for s in Status}
    for bug in store:
        summary[bug.status.value] += 1
    critical_open = [b for b in store if b.severity == Severity.CRITICAL and b.status == Status.OPEN]
    return {"by_status": summary, "critical_open_count": len(critical_open)}


def main() -> int:
    store: List[BugReport] = []
    file_bug(store, "Dashboard fails to load release section", severity=Severity.HIGH)
    file_bug(store, "Typo in Operations.md", severity=Severity.LOW)
    third = file_bug(store, "Installer crashes with no requirements.txt", severity=Severity.CRITICAL)

    assign_bug(store, third.id, "president_sac")
    resolve_bug(store, third.id)

    for bug in store:
        print(f"#{bug.id} [{bug.severity.value}/{bug.status.value}] {bug.title} (assignee={bug.assignee})")

    print(triage_summary(store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
