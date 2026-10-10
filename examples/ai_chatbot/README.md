# ai_chatbot

M4.10 §5 sample project — a minimal scripted chatbot loop demonstrating
the *shape* of an agent-style request/response cycle, without a live
LLM call or the platform's real LangGraph orchestration (neither is
available to invoke standalone in this example environment). Intent
routing is a small keyword-based dispatcher standing in for what a real
agent's LangGraph node would do — swap `route_intent`/`respond` for a
real model call to graduate this into an actual chatbot.

Run:

```
python examples/ai_chatbot/main.py
```
