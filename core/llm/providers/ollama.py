"""Ollama local adapter."""
from __future__ import annotations
import time
from typing import List
import httpx
from core.contracts import FinishReason, LLMMessage, LLMProvider, LLMResponse

class OllamaProvider:
    provider_name = "ollama"
    def __init__(self, base_url: str = "http://localhost:11434"):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=300.0)
    def get_models(self):
        return ["llama3.3","mistral","codellama","deepseek-r1"]
    async def complete(self, messages: List[LLMMessage], model: str,
                       max_tokens: int = 4096, temperature: float = 0.2, **kw) -> LLMResponse:
        t0 = time.monotonic()
        r  = await self._http.post("/api/chat", json={"model":model,
             "messages":[m.to_dict() for m in messages],"stream":False,
             "options":{"temperature":temperature,"num_predict":max_tokens}})
        r.raise_for_status(); d = r.json()
        ms  = int((time.monotonic()-t0)*1000)
        inp = d.get("prompt_eval_count",0); out = d.get("eval_count",0)
        return LLMResponse(content=d["message"]["content"], input_tokens=inp,
               output_tokens=out, total_tokens=inp+out, model=model,
               provider=LLMProvider.OLLAMA, finish_reason=FinishReason.STOP,
               latency_ms=ms, cost_usd=0.0)
    async def complete_with_retry(self, messages, model, max_tokens=4096,
                                  temperature=0.2, max_retries=3, **kw):
        import asyncio
        for i in range(max_retries):
            try: return await self.complete(messages,model,max_tokens,temperature)
            except Exception as e:
                if i==max_retries-1: raise
                await asyncio.sleep(5*(2**i))
