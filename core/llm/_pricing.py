"""Shared token pricing table."""
_PRICES = {
    "claude-opus-4-6":  (15.00,75.00), "claude-sonnet-4-6":(3.00,15.00),
    "claude-haiku-4-5": (0.80, 4.00),  "gpt-4o":           (5.00,15.00),
    "gpt-4o-mini":      (0.15, 0.60),  "o3":               (10.0,40.00),
    "gemini-2.5-pro":   (1.25,10.00),  "gemini-2.0-flash": (0.10, 0.40),
}
def cost(model: str, inp: int, out: int) -> float:
    p = _PRICES.get(model, (0.0, 0.0))
    return (inp/1_000_000)*p[0] + (out/1_000_000)*p[1]
