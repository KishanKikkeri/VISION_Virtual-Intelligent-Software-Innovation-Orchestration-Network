"""
services/devops — M3.6 DevOps Service.
Importing this module registers all 10 agents with AgentFactory.
"""
# Workers (6) — register on import
import services.devops.workers

# Leads (3) — register on import
import services.devops.leads

# Head (1) — registers on import
import services.devops.head

__all__ = ["build_devops_graph"]


def build_devops_graph(checkpointer=None):
    from services.devops.workflows.devops_graph import build_devops_graph as _build
    return _build(checkpointer=checkpointer)
