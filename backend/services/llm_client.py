"""
Shared LLM Client
=================

Thin wrapper around the OpenAI-compatible endpoints used elsewhere in the
project (chatbot.py uses the same providers). This client is intentionally
lightweight and stateless so that any service (requirements_generator,
hld_generator, codebase_analyzer) can grab an instance without dragging in
the full chatbot machinery.

Provider selection follows the same env-var contract as `chatbot.py`:

  LLM_PROVIDER = "azure_ai_foundry" | "aks"

For MDD_NEW the default is `azure_ai_foundry` (Llama-3.3-70B-Instruct).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


_DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "azure_ai_foundry").lower()


def _resolve_provider(provider: str) -> Dict[str, str]:
    """Resolve env-driven config for a given provider name."""
    provider = (provider or _DEFAULT_PROVIDER).lower()

    if provider == "azure_ai_foundry":
        return {
            "provider": "azure_ai_foundry",
            "base_url": os.getenv(
                "AZURE_AI_FOUNDRY_URL",
                "https://wdllmazureai.services.ai.azure.com/openai/v1/",
            ),
            "api_key": os.getenv("AZURE_AI_FOUNDRY_API_KEY", ""),
            "model": os.getenv("AZURE_AI_FOUNDRY_MODEL_NAME", "Llama-3.3-70B-Instruct"),
        }

    if provider == "aks":
        return {
            "provider": "aks",
            "base_url": os.getenv("AKS_LLM_URL", "http://10.104.1.10/mistral/v1"),
            "api_key": os.getenv("AKS_LLM_API_KEY", "not-needed"),
            "model": os.getenv("AKS_MODEL_NAME", "mistralai/Devstral-Small-2507"),
        }

    raise ValueError(
        f"Unknown LLM_PROVIDER='{provider}'. Use 'azure_ai_foundry' or 'aks'."
    )


class LLMClient:
    """Minimal OpenAI-compatible chat client used by HLD-pipeline services."""

    def __init__(self, provider: Optional[str] = None):
        cfg = _resolve_provider(provider or _DEFAULT_PROVIDER)
        self.provider: str = cfg["provider"]
        self.base_url: str = cfg["base_url"]
        self.model: str = cfg["model"]
        self._client = OpenAI(base_url=self.base_url, api_key=cfg["api_key"])

    # ------------------------------------------------------------------
    # Core call
    # ------------------------------------------------------------------
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        extra_messages: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Single-turn chat completion with transient error retries."""
        import time
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if extra_messages:
            messages.extend(extra_messages)
        messages.append({"role": "user", "content": user_prompt})

        max_retries = 3
        base_delay = 2.0
        
        for attempt in range(1, max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                res_content = (response.choices[0].message.content or "").strip()
                if not res_content:
                    raise RuntimeError("LLM returned an empty response")
                return res_content
            except Exception as e:
                print(f"⚠️ LLM call attempt {attempt}/{max_retries} failed: {e}")
                if attempt == max_retries:
                    raise
                time.sleep(base_delay * (2 ** (attempt - 1)))

    def info(self) -> Dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
        }

    def switch_provider(self, provider: str) -> Dict[str, str]:
        """Switch LLM provider at runtime (aks <-> azure_ai_foundry)"""
        cfg = _resolve_provider(provider)
        self.provider = cfg["provider"]
        self.base_url = cfg["base_url"]
        self.model = cfg["model"]
        self._client = OpenAI(base_url=self.base_url, api_key=cfg["api_key"])
        return {
            "status": "success",
            "provider": self.provider,
            "model_name": self.model,
            "base_url": self.base_url,
        }


# Module-level singleton for convenience
_default_client: Optional[LLMClient] = None


def get_llm_client(force_reload: bool = False) -> LLMClient:
    """Return a shared LLMClient instance (env-driven)."""
    global _default_client
    if _default_client is None or force_reload:
        _default_client = LLMClient()
    return _default_client
