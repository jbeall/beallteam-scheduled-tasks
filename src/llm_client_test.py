"""Request shapes and response handling for the Ollama and OpenRouter backends."""

import json

import httpx

from src.llm_client import LlmClient, LlmConfig, LlmError, strip_thinking

MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Texts..."}]


def _client(config: LlmConfig, handler) -> tuple[LlmClient, list[dict]]:
    sent: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append({"url": str(request.url), "body": body, "headers": dict(request.headers)})
        return handler(body)

    client = LlmClient(config)
    client._client = httpx.Client(transport=httpx.MockTransport(record))
    return client, sent


def test_strip_thinking() -> None:
    text, stripped = strip_thinking("<think>hmm</think>\nThe note.")
    assert text == "The note." and stripped >= len("<think>hmm</think>")
    assert strip_thinking("reasoning only closed</think>The note.")[0] == "The note."
    assert strip_thinking("Plain note.") == ("Plain note.", 0)


def test_ollama_request_disables_thinking_and_sets_context() -> None:
    config = LlmConfig(provider="ollama", base_url="http://ollama:11434", model="qwen3.8:27b", num_ctx=16384)
    client, sent = _client(
        config,
        lambda body: httpx.Response(200, json={"message": {"content": "Note."}, "prompt_eval_count": 40, "eval_count": 5}),
    )
    result = client.chat(MESSAGES, json_output=True)
    body = sent[0]["body"]
    assert sent[0]["url"] == "http://ollama:11434/api/chat"
    assert body["think"] is False and body["stream"] is False and body["format"] == "json"
    assert body["options"]["num_ctx"] == 16384
    assert (result.text, result.prompt_tokens, result.completion_tokens, result.warning) == ("Note.", 40, 5, None)


def test_ollama_retries_without_think_for_models_that_do_not_support_it() -> None:
    def handler(body):
        if "think" in body:
            return httpx.Response(400, json={"error": '"llama3" does not support thinking'})
        return httpx.Response(200, json={"message": {"content": "Note."}})

    client, sent = _client(LlmConfig(provider="ollama", base_url="http://o", model="llama3"), handler)
    assert client.chat(MESSAGES).text == "Note."
    assert ["think" in s["body"] for s in sent] == [True, False]


def test_ollama_warns_when_prompt_cannot_fit_context() -> None:
    config = LlmConfig(provider="ollama", base_url="http://o", model="m", num_ctx=1024, max_tokens=800)
    client, _ = _client(config, lambda body: httpx.Response(200, json={"message": {"content": "Note."}}))
    long_messages = [{"role": "user", "content": "x" * 4000}]
    assert "context window" in client.chat(long_messages).warning


def test_openrouter_request_turns_off_reasoning_and_denies_data_collection() -> None:
    config = LlmConfig(provider="openrouter", base_url="https://openrouter.ai/api/v1", model="qwen/qwen3.8-27b", api_key="k")
    client, sent = _client(
        config,
        lambda body: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Note."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 6, "cost": 0.0002},
            },
        ),
    )
    result = client.chat(MESSAGES)
    body = sent[0]["body"]
    assert sent[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent[0]["headers"]["authorization"] == "Bearer k"
    assert body["reasoning"] == {"effort": "none"}
    assert body["provider"] == {"data_collection": "deny"}
    assert (result.text, result.cost_usd) == ("Note.", 0.0002)


def test_openrouter_falls_back_when_reasoning_is_mandatory() -> None:
    def handler(body):
        if "reasoning" in body:
            return httpx.Response(400, json={"error": {"message": "Reasoning is mandatory for this endpoint"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "<think>x</think>Note.", "reasoning": "y"}, "finish_reason": "stop"}]},
        )

    config = LlmConfig(provider="openrouter", base_url="https://o", model="m", api_key="k")
    client, sent = _client(config, handler)
    result = client.chat(MESSAGES)
    assert result.text == "Note." and result.thinking_chars > 0
    assert ["reasoning" in s["body"] for s in sent] == [True, False]


def test_unclosed_think_block_is_not_returned_as_the_note() -> None:
    assert strip_thinking("<think>\nOkay, so Pat asked about the HOA and")[0] == ""


def test_ollama_flags_output_cut_off_at_max_tokens() -> None:
    config = LlmConfig(provider="ollama", base_url="http://o", model="m")
    client, _ = _client(
        config,
        lambda body: httpx.Response(200, json={"message": {"content": "<think>still going"}, "done_reason": "length"}),
    )
    result = client.chat(MESSAGES)
    assert result.text == "" and result.warning == "output hit max_tokens before any answer"


def test_non_json_reply_raises_llm_error(monkeypatch) -> None:
    monkeypatch.setattr("src.llm_client.time.sleep", lambda seconds: None)
    config = LlmConfig(provider="ollama", base_url="http://o", model="m")
    client, sent = _client(config, lambda body: httpx.Response(200, text="<html>502 Bad Gateway</html>"))
    try:
        client.chat(MESSAGES)
    except LlmError as exc:
        assert "non-JSON" in str(exc)
    else:
        raise AssertionError("expected LlmError")
    assert len(sent) == 4


def test_provider_override_ignores_the_other_providers_base_url(monkeypatch) -> None:
    monkeypatch.setattr("src.llm_client.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("LLM_API_KEY", "k")
    config = LlmConfig.from_env(provider="openrouter", model="qwen/qwen3.8-27b")
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert LlmConfig.from_env(model="qwen3.8:27b").base_url == "http://localhost:11434"
