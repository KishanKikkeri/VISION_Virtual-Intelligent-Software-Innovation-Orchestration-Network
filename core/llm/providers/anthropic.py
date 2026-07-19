"""Anthropic Claude adapter."""
from __future__ import annotations
import time
from typing import List, Optional, Dict, Any
from core.contracts import FinishReason, LLMMessage, LLMProvider, LLMResponse

class AnthropicProvider:
    provider_name = "anthropic"
    def __init__(self, api_key: str):
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
    def get_models(self):
        return ["claude-opus-4-6","claude-sonnet-4-6","claude-haiku-4-5"]
    async def complete(self, messages: List[LLMMessage], model: str,
                       max_tokens: int = 4096, temperature: float = 0.2, **kw) -> LLMResponse:
        system = next((m.content for m in messages if m.role=="system"), "")
        conv   = [m.to_dict() for m in messages if m.role != "system"]
        t0 = time.monotonic()
        r  = await self._client.messages.create(model=model, max_tokens=max_tokens,
              temperature=temperature, system=system, messages=conv)
        ms  = int((time.monotonic()-t0)*1000)
        inp = r.usage.input_tokens; out = r.usage.output_tokens
        from core.llm._pricing import cost
        return LLMResponse(content=r.content[0].text, input_tokens=inp, output_tokens=out,
               total_tokens=inp+out, model=model, provider=LLMProvider.ANTHROPIC,
               finish_reason=FinishReason(r.stop_reason or "stop"), latency_ms=ms,
               cost_usd=cost(model,inp,out))
    async def complete_with_retry(self, messages, model, max_tokens=4096,
                                  temperature=0.2, max_retries=3, **kw):
        import asyncio
        for i in range(max_retries):
            try: return await self.complete(messages,model,max_tokens,temperature)
            except Exception as e:
                if i==max_retries-1: raise
                await asyncio.sleep(5*(2**i))
