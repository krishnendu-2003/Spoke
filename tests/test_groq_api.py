import json

import httpx
import pytest

from spoke.groq_api import GroqClient, GroqError


def client_with(handler):
    c = GroqClient("gsk_test")
    c._client = httpx.Client(base_url="https://api.groq.com/openai/v1", transport=httpx.MockTransport(handler))
    return c


def test_chat_returns_only_content_never_reasoning_and_sends_params():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant",
            "content": "So the deploy is at 6 tomorrow.",
            "reasoning": "The user said 5 then corrected to 6, so ...",
        }}]})

    c = client_with(handler)
    out = c.chat(model="openai/gpt-oss-20b", messages=[], timeout=1, max_tokens=100,
                 extra={"reasoning_effort": "low", "include_reasoning": False})
    assert out == "So the deploy is at 6 tomorrow."
    assert seen["reasoning_effort"] == "low" and seen["include_reasoning"] is False
    assert seen["model"] == "openai/gpt-oss-20b" and seen["temperature"] == 0


def test_http_errors_carry_status_for_model_repick():
    c = client_with(lambda req: httpx.Response(404, json={"error": {"message": "model does not exist"}}))
    with pytest.raises(GroqError) as e:
        c.chat(model="gone", messages=[], timeout=1, max_tokens=10)
    assert e.value.status == 404 and "does not exist" in str(e.value)


def test_error_message_never_contains_key():
    c = client_with(lambda req: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    with pytest.raises(GroqError) as e:
        c.chat(model="m", messages=[], timeout=1, max_tokens=10)
    assert "gsk_test" not in str(e.value)


def test_stt_retries_once_on_5xx():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"text": " hi "})

    assert client_with(handler).transcribe(b"x", model="w", language="en", prompt="", timeout=1) == "hi"
    assert len(calls) == 2
