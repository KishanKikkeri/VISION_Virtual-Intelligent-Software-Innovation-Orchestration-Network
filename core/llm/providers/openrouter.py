"""OpenRouter adapter (OpenAI-compatible)."""
from __future__ import annotations
import os, time
from typing import List
from core.contracts import FinishReason, LLMMessage, LLMProvider, LLMResponse

class OpenRouterProvider:
    provider_name = "openrouter"
    def __init__(self, api_key: str):
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer":os.getenv("OPENROUTER_SITE_URL","https://aasc.dev"),
                             "X-Title":os.getenv("OPENROUTER_APP_NAME","AASC")})
    def get_models(self):
        return ["openrouter/auto","anthropic/claude-sonnet-4-6","openai/gpt-4o"]
    async def complete(self, messages: List[LLMMessage], model: str,
                       max_tokens: int = 4096, temperature: float = 0.2, **kw) -> LLMResponse:
        t0 = time.monotonic()
        r  = await self._client.chat.completions.create(model=model,
             messages=[m.to_dict() for m in messages],
             max_tokens=max_tokens, temperature=temperature)
        ms  = int((time.monotonic()-t0)*1000)
        c   = r.choices[0]
        inp = r.usage.prompt_tokens if r.usage else 0
        out = r.usage.completion_tokens if r.usage else 0
        from core.llm._pricing import cost
        return LLMResponse(content=c.message.content or "", input_tokens=inp,
               output_tokens=out, total_tokens=inp+out, model=model,
               provider=LLMProvider.OPENROUTER,
               finish_reason=FinishReason(c.finish_reason or "stop"),
               latency_ms=ms, cost_usd=cost(model,inp,out))
    async def complete_with_retry(self, messages, model, max_tokens=4096,
                                  temperature=0.2, max_retries=3, **kw):
        import asyncio
        for i in range(max_retries):
            try: return await self.complete(messages,model,max_tokens,temperature)
            except Exception as e:
                if i==max_retries-1: raise
                await asyncio.sleep(5*(2**i))
