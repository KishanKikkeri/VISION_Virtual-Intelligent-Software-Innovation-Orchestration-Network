"""
services/security — M3.5 Security Service.
Importing this module registers all 9 agents with AgentFactory.
"""
# Workers (5) — register on import
import services.security.workers

# Leads (3) — register on import
import services.security.leads

# Head (1) — registers on import
import services.security.head

__all__ = ["build_security_graph"]


def build_security_graph(checkpointer=None):
    from services.security.workflows.security_graph import build_security_graph as _build
    return _build(checkpointer=checkpointer)
