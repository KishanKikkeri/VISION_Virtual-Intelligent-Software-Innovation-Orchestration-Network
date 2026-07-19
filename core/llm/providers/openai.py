"""OpenAI adapter."""
from __future__ import annotations
import time
from typing import List
from core.contracts import FinishReason, LLMMessage, LLMProvider, LLMResponse

class OpenAIProvider:
    provider_name = "openai"
    def __init__(self, api_key: str):
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(api_key=api_key)
    def get_models(self):
        return ["gpt-4o","gpt-4o-mini","o3","o4-mini"]
    async def complete(self, messages: List[LLMMessage], model: str,
                       max_tokens: int = 4096, temperature: float = 0.2, **kw) -> LLMResponse:
        kw2 = {"model":model,"messages":[m.to_dict() for m in messages],"max_tokens":max_tokens}
        if model not in ("o3","o4-mini"): kw2["temperature"]=temperature
        t0 = time.monotonic()
        r  = await self._client.chat.completions.create(**kw2)
        ms  = int((time.monotonic()-t0)*1000)
        c   = r.choices[0]; inp=r.usage.prompt_tokens; out=r.usage.completion_tokens
        from core.llm._pricing import cost
        return LLMResponse(content=c.message.content or "", input_tokens=inp,
               output_tokens=out, total_tokens=inp+out, model=model,
               provider=LLMProvider.OPENAI, finish_reason=FinishReason(c.finish_reason or "stop"),
               latency_ms=ms, cost_usd=cost(model,inp,out))
    async def complete_with_retry(self, messages, model, max_tokens=4096,
                                  temperature=0.2, max_retries=3, **kw):
        import asyncio
        for i in range(max_retries):
            try: return await self.complete(messages,model,max_tokens,temperature)
            except Exception as e:
                if i==max_retries-1: raise
                await asyncio.sleep(5*(2**i))
