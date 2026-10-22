"""Chat client for OpenRouter (or any OpenAI-compatible server) and a local Ollama server.

Configure with .env (see .env.example):
  LLM_PROVIDER   ollama | openrouter | openai
  LLM_BASE_URL   default http://localhost:11434 (ollama) / https://openrouter.ai/api/v1 (openrouter)
  LLM_MODEL      e.g. an Ollama tag or an OpenRouter model slug
  LLM_API_KEY    OpenRouter key (not needed for Ollama)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv

from src.paths import ROOT

DEFAULT_BASE_URLS = {
    "ollama": "http://localhost:11434",
    "openrouter": "https://openrouter.ai/api/v1",
}
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 800
    num_ctx: int = 16384  # Ollama context window; its small default silently truncates long prompts
    think: bool = False  # Qwen3-family models "think" before answering unless told not to
    timeout: float = 300.0

    @classmethod
    def from_env(cls, *, provider: str | None = None, model: str | None = None) -> "LlmConfig":
        load_dotenv(ROOT / ".env")
        provider = (provider or os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
        base_url = os.getenv("LLM_BASE_URL", "").strip() or DEFAULT_BASE_URLS.get(provider, "")
        model = (model or os.getenv("LLM_MODEL") or "").strip()
        if not base_url:
            raise LlmError(f"Set LLM_BASE_URL for provider {provider!r}.")
        if not model:
            raise LlmError("Set LLM_MODEL in .env (or pass --model).")
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if provider == "openrouter" and not api_key:
            raise LlmError("Set LLM_API_KEY to your OpenRouter key.")
        return cls(
            provider=provider,
            base_url=base_url.rstrip("/"),
            model=model,
            api_key=api_key,
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "800")),
            num_ctx=int(os.getenv("LLM_NUM_CTX", "16384")),
            think=os.getenv("LLM_THINK", "false").strip().lower() in {"1", "true", "yes"},
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "300")),
        )


@dataclass(frozen=True)
class LlmResult:
    text: str
    seconds: float
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None = None
    thinking_chars: int = 0
    warning: str | None = None


def strip_thinking(text: str) -> tuple[str, int]:
    """Remove <think>…</think> reasoning that some servers leave in the content."""
    removed = 0
    if "</think>" in text:
        head, _, tail = text.rpartition("</think>")
        removed = len(head) + len("</think>")
        text = tail
    cleaned = _THINK_BLOCK.sub("", text)
    return cleaned.strip(), removed + len(text) - len(cleaned)


class LlmClient:
    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        self._client = httpx.Client(timeout=config.timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LlmClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def chat(self, messages: list[dict[str, str]], *, json_output: bool = False) -> LlmResult:
        if self.config.provider == "ollama":
            return self._ollama(messages, json_output)
        return self._openai_compatible(messages, json_output)

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[dict[str, Any], float]:
        last_error = ""
        for attempt in range(4):
            started = time.monotonic()
            try:
                response = self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                time.sleep(2**attempt)
                continue
            elapsed = time.monotonic() - started
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"{response.status_code}: {response.text[:300]}"
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise LlmError(f"POST {url} failed ({response.status_code}): {response.text[:500]}")
            return response.json(), elapsed
        raise LlmError(f"POST {url} failed after retries: {last_error}")

    def _ollama(self, messages: list[dict[str, str]], json_output: bool) -> LlmResult:
        cfg = self.config
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "stream": False,
            "think": cfg.think,
            "options": {
                "num_ctx": cfg.num_ctx,
                "temperature": cfg.temperature,
                "num_predict": cfg.max_tokens,
            },
        }
        if json_output:
            body["format"] = "json"
        url = f"{cfg.base_url}/api/chat"
        try:
            data, seconds = self._post(url, body, {})
        except LlmError as exc:
            if "does not support thinking" not in str(exc):
                raise
            body.pop("think")  # non-thinking model: retry without the flag
            data, seconds = self._post(url, body, {})
        message = data.get("message") or {}
        text, stripped = strip_thinking(message.get("content") or "")
        prompt_tokens = data.get("prompt_eval_count")
        warning = None
        if prompt_tokens and prompt_tokens >= cfg.num_ctx - 16:
            warning = f"prompt filled the {cfg.num_ctx}-token context window; it was probably truncated"
        return LlmResult(
            text=text,
            seconds=seconds,
            prompt_tokens=prompt_tokens,
            completion_tokens=data.get("eval_count"),
            thinking_chars=stripped + len(message.get("thinking") or ""),
            warning=warning,
        )

    def _openai_compatible(self, messages: list[dict[str, str]], json_output: bool) -> LlmResult:
        cfg = self.config
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        if json_output:
            body["response_format"] = {"type": "json_object"}
        if cfg.provider == "openrouter":
            headers["X-Title"] = "Beall Team SMS notes"
            body["reasoning"] = {"enabled": cfg.think}
        data, seconds = self._post(f"{cfg.base_url}/chat/completions", body, headers)
        choices = data.get("choices") or []
        if not choices:
            raise LlmError(f"No choices in response: {str(data)[:300]}")
        message = choices[0].get("message") or {}
        text, stripped = strip_thinking(message.get("content") or "")
        usage = data.get("usage") or {}
        return LlmResult(
            text=text,
            seconds=seconds,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"),
            thinking_chars=stripped + len(message.get("reasoning") or ""),
            warning="output hit max_tokens" if choices[0].get("finish_reason") == "length" else None,
        )
