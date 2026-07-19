"""
core/llm/registry.py
======================
LLM Provider Registry — initialised once at startup.
All agents call LLMProviderRegistry.complete() — never vendor SDKs directly.
Handles provider instantiation, health checking, and failover.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Tuple

import structlog

from core.config.settings import get_settings
from core.contracts import FinishReason, LLMMessage, LLMProvider, LLMResponse

log = structlog.get_logger(__name__)


class LLMProviderRegistry:
    """
    Central registry for all LLM provider adapters.
    Call initialise() once at service startup.
    Call complete() from BaseAgent.call_llm() — never call vendor APIs directly.
    """

    _providers: Dict[str, object] = {}   # provider_name → adapter instance

    @classmethod
    def initialise(cls) -> None:
        """Instantiates all providers whose API keys are available."""
        settings = cls._settings = get_settings()
        cls._providers = {}

        if settings.anthropic_api_key:
            cls._try_register("anthropic", settings.anthropic_api_key)
        if settings.openai_api_key:
            cls._try_register("openai", settings.openai_api_key)
        if settings.gemini_api_key:
            cls._try_register("gemini", settings.gemini_api_key)
        if settings.openrouter_api_key:
            cls._try_register("openrouter", settings.openrouter_api_key)

        # Ollama always attempted (no key)
        cls._try_register("ollama", None)

        if not cls._providers:
            log.warning("no_llm_providers_registered",
                        message="No LLM providers available. Check API keys in .env")
        else:
            log.info("llm_providers_ready", providers=list(cls._providers.keys()))

    @classmethod
    def _try_register(cls, name: str, api_key: Optional[str]) -> None:
        try:
            adapter = _build_adapter(name, api_key)
            cls._providers[name] = adapter
            log.debug("llm_provider_registered", provider=name)
        except ImportError as e:
            log.warning("llm_provider_skipped", provider=name, reason=f"Missing SDK: {e}")
        except Exception as e:
            log.warning("llm_provider_failed", provider=name, reason=str(e))

    @classmethod
    async def complete(
        cls,
        provider:    str,
        model:       str,
        messages:    List[LLMMessage],
        max_tokens:  int   = 4096,
        temperature: float = 0.2,
        max_retries: int   = 3,
    ) -> LLMResponse:
        """
        Executes an LLM completion with retry + automatic failover.
        This is the ONLY method agents should call for LLM inference.
        """
        adapter = cls._providers.get(provider)
        if not adapter:
            raise ValueError(f"Provider '{provider}' is not registered. "
                             f"Available: {list(cls._providers.keys())}")
        return await adapter.complete_with_retry(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=max_retries,
        )

    @classmethod
    def available(cls) -> List[str]:
        return list(cls._providers.keys())

    @classmethod
    async def health_check(cls) -> Dict[str, bool]:
        """Pings each registered provider with a minimal completion."""
        results: Dict[str, bool] = {}
        for name, adapter in cls._providers.items():
            try:
                await adapter.complete(
                    messages=[LLMMessage(role="user", content="Reply: OK")],
                    model=adapter.get_models()[0],
                    max_tokens=5,
                )
                results[name] = True
            except Exception:
                results[name] = False
        return results


def _build_adapter(name: str, api_key: Optional[str]) -> object:
    """Lazily imports and instantiates the correct provider adapter."""
    if name == "anthropic":
        from core.llm.providers.anthropic import AnthropicProvider
        return AnthropicProvider(api_key=api_key)
    if name == "openai":
        from core.llm.providers.openai import OpenAIProvider
        return OpenAIProvider(api_key=api_key)
    if name == "gemini":
        from core.llm.providers.gemini import GeminiProvider
        return GeminiProvider(api_key=api_key)
    if name == "openrouter":
        from core.llm.providers.openrouter import OpenRouterProvider
        return OpenRouterProvider(api_key=api_key)
    if name == "ollama":
        from core.llm.providers.ollama import OllamaProvider
        settings = get_settings()
        return OllamaProvider(base_url=settings.ollama_base_url)
    raise ValueError(f"Unknown provider: '{name}'")
