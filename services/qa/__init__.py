"""
services/qa — M3.4 QA Service.
Importing this module registers all 10 agents with AgentFactory.
"""
# Workers (5) — register on import
import services.qa.workers

# Leads (4) — register on import
import services.qa.leads

# Head (1) — registers on import
import services.qa.head

__all__ = ["build_qa_graph"]


def build_qa_graph(checkpointer=None):
    from services.qa.workflows.qa_graph import build_qa_graph as _build
    return _build(checkpointer=checkpointer)
