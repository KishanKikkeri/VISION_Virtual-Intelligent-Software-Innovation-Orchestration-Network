"""
examples/ai_chatbot/main.py
=================================
M4.10 §5 — minimal scripted chatbot loop. `route_intent` stands in for
a LangGraph router node; `respond` stands in for an LLM call node. Both
are plain deterministic Python here (see README for why) so the example
is runnable with zero external dependencies or API keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List


@dataclass
class ConversationState:
    turns: List[Dict[str, str]] = field(default_factory=list)

    def add(self, role: str, text: str) -> None:
        self.turns.append({"role": role, "text": text})


def route_intent(message: str) -> str:
    lowered = message.lower()
    if any(word in lowered for word in ("hi", "hello", "hey")):
        return "greeting"
    if "help" in lowered:
        return "help"
    if "bye" in lowered:
        return "farewell"
    return "unknown"


_RESPONSES: Dict[str, Callable[[str], str]] = {
    "greeting": lambda msg: "Hello! I'm the AASC example chatbot — ask me for 'help'.",
    "help": lambda msg: "I understand: greetings, help requests, and farewells. Try saying 'bye'.",
    "farewell": lambda msg: "Goodbye!",
    "unknown": lambda msg: "I didn't understand that — try 'help'.",
}


def respond(state: ConversationState, message: str) -> str:
    state.add("user", message)
    intent = route_intent(message)
    reply = _RESPONSES[intent](message)
    state.add("assistant", reply)
    return reply


def main() -> int:
    state = ConversationState()
    script = ["hello", "help", "what's the weather", "bye"]
    for message in script:
        reply = respond(state, message)
        print(f"user: {message}")
        print(f"bot:  {reply}")
    print(f"\n{len(state.turns)} turns recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
