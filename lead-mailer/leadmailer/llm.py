"""Pluggable LLM providers. Each part (finder/writer/sender) picks a provider by name in settings.yaml:

    llm:
      providers:
        local_ollama: {type: ollama, base_url: ..., model: ...}
        other:        {type: openai_compatible, base_url: ..., model: ..., api_key_env: ...}
      use: {finder: none, writer: local_ollama, sender: none}

Swapping the model behind any part is a YAML edit, never a code change.
"""
import json
import os
import urllib.error
import urllib.request

from .config import ConfigError, Settings


class LLMError(Exception):
    pass


class Provider:
    name = "none"

    def available(self) -> bool:
        return False

    def complete(self, prompt: str, system: str | None = None) -> str:
        raise LLMError("no provider configured")


class NoneProvider(Provider):
    pass


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMError(f"{url}: {e}") from e


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.timeout = int(cfg.get("timeout_seconds", 60))

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, system: str | None = None) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        out = _post_json(f"{self.base_url}/api/generate", payload, {}, self.timeout)
        text = out.get("response")
        if not isinstance(text, str):
            raise LLMError("ollama returned no text")
        return text.strip()


class OpenAICompatibleProvider(Provider):
    """Any /v1/chat/completions style endpoint (OpenAI, Groq, LM Studio, vLLM, ...)."""

    name = "openai_compatible"

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.timeout = int(cfg.get("timeout_seconds", 60))
        self.api_key = os.environ.get(cfg.get("api_key_env", ""), "")

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        out = _post_json(f"{self.base_url}/chat/completions", {"model": self.model, "messages": messages}, headers, self.timeout)
        try:
            return out["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("unexpected chat completion response") from e


TYPES = {"ollama": OllamaProvider, "openai_compatible": OpenAICompatibleProvider}


def build_provider(cfg: dict | None) -> Provider:
    if not cfg:
        return NoneProvider()
    kind = cfg.get("type")
    if kind not in TYPES:
        raise ConfigError(f"unknown llm provider type: {kind!r} (known: {', '.join(TYPES)})")
    return TYPES[kind](cfg)


def provider_for(settings: Settings, part: str) -> Provider:
    """part is finder | writer | sender."""
    use = settings.get(f"llm.use.{part}")
    if use in (None, "none"):
        return NoneProvider()
    return build_provider(settings.get("llm.providers")[use])
