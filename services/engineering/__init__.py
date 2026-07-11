"""
services/engineering — M3.3 Engineering Service.
Importing this module registers all 20 agents with AgentFactory.
"""
# Workers (15) — register on import
import services.engineering.workers

# Leads (4) — register on import
import services.engineering.leads

# Head (1) — registers on import
import services.engineering.head

__all__ = ["build_engineering_graph"]


def build_engineering_graph(checkpointer=None):
    from services.engineering.workflows.engineering_graph import build_engineering_graph as _build
    return _build(checkpointer=checkpointer)
