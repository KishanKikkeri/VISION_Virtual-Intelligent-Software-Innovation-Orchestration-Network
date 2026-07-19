"""Google Gemini adapter."""
from __future__ import annotations
import time
from typing import List
from core.contracts import FinishReason, LLMMessage, LLMProvider, LLMResponse

class GeminiProvider:
    provider_name = "gemini"
    def __init__(self, api_key: str):
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        self._genai = genai
    def get_models(self):
        return ["gemini-2.5-pro","gemini-2.0-flash"]
    async def complete(self, messages: List[LLMMessage], model: str,
                       max_tokens: int = 4096, temperature: float = 0.2, **kw) -> LLMResponse:
        system = next((m.content for m in messages if m.role=="system"), None)
        gm  = self._genai.GenerativeModel(model_name=model, system_instruction=system,
              generation_config=self._genai.types.GenerationConfig(
                  max_output_tokens=max_tokens, temperature=temperature))
        hist = [{"role":"user" if m.role=="user" else "model","parts":[m.content]}
                for m in messages if m.role != "system"]
        t0 = time.monotonic()
        chat = gm.start_chat(history=hist[:-1])
        r    = await chat.send_message_async(hist[-1]["parts"][0])
        ms   = int((time.monotonic()-t0)*1000)
        inp  = getattr(r.usage_metadata,"prompt_token_count",0)
        out  = getattr(r.usage_metadata,"candidates_token_count",0)
        from core.llm._pricing import cost
        return LLMResponse(content=r.text, input_tokens=inp, output_tokens=out,
               total_tokens=inp+out, model=model, provider=LLMProvider.GEMINI,
               finish_reason=FinishReason.STOP, latency_ms=ms, cost_usd=cost(model,inp,out))
    async def complete_with_retry(self, messages, model, max_tokens=4096,
                                  temperature=0.2, max_retries=3, **kw):
        import asyncio
        for i in range(max_retries):
            try: return await self.complete(messages,model,max_tokens,temperature)
            except Exception as e:
                if i==max_retries-1: raise
                await asyncio.sleep(5*(2**i))
