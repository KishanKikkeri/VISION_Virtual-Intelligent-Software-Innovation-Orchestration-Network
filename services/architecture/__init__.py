"""
services/architecture — M3.1 Architecture Service.
Importing this module registers all 12 agents with AgentFactory.
"""
# Workers (9) — register on import
import services.architecture.workers

# Leads (3) — register on import
import services.architecture.leads

# Head (1) — registers on import
import services.architecture.head

__all__ = ["build_architecture_graph"]

def build_architecture_graph(checkpointer=None):
    from services.architecture.workflows.architecture_graph import build_architecture_graph as _build
    return _build(checkpointer=checkpointer)
