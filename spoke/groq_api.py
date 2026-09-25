"""Thin Groq (OpenAI-compatible) HTTP client shared by STT and cleanup.

One persistent httpx.Client so TLS is reused across dictations; `warm()` is called on
key-down so the connection is hot by the time the key is released.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.groq.com/openai/v1"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GroqError(Exception):
    """Raised with a short, log-safe message (never contains the API key)."""


class GroqClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120.0),
            timeout=httpx.Timeout(10.0, connect=3.0),
        )
        self._last_used = 0.0
        self._warm_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def warm(self) -> None:
        """Pre-open the TLS connection in the background if it's likely gone cold."""
        if time.monotonic() - self._last_used < 30:
            return
        if not self._warm_lock.acquire(blocking=False):
            return

        def _run():
            try:
                t0 = time.perf_counter()
                self._client.get("/models", timeout=3.0)
                self._last_used = time.monotonic()
                log.debug("groq connection warmed in %.0f ms", (time.perf_counter() - t0) * 1000)
            except Exception as e:
                log.debug("warm-up failed: %s", type(e).__name__)
            finally:
                self._warm_lock.release()

        threading.Thread(target=_run, daemon=True).start()

    def _post(self, path: str, *, timeout: float, retries: int, **kwargs) -> dict:
        attempt = 0
        while True:
            try:
                resp = self._client.post(path, timeout=timeout, **kwargs)
                self._last_used = time.monotonic()
                if resp.status_code in RETRYABLE_STATUS and attempt < retries:
                    attempt += 1
                    log.warning("groq %s -> HTTP %s, retrying", path, resp.status_code)
                    continue
                if resp.status_code >= 400:
                    raise GroqError(f"HTTP {resp.status_code} from {path}: {_error_text(resp)}")
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < retries:
                    attempt += 1
                    log.warning("groq %s -> %s, retrying", path, type(e).__name__)
                    continue
                raise GroqError(f"{type(e).__name__} calling {path}") from e

    def transcribe(
        self,
        wav: bytes,
        *,
        model: str,
        language: str | None,
        prompt: str,
        timeout: float,
        retries: int = 1,
    ) -> str:
        data = {"model": model, "response_format": "json", "temperature": "0"}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("audio.wav", wav, "audio/wav")}
        body = self._post("/audio/transcriptions", data=data, files=files, timeout=timeout, retries=retries)
        return str(body.get("text", "")).strip()

    def chat(self, *, model: str, messages: list[dict], timeout: float, max_tokens: int) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # No retry: cleanup has a hard 1 s budget and falls back to the raw transcript.
        body = self._post("/chat/completions", json=payload, timeout=timeout, retries=0)
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise GroqError("malformed chat response") from e

    def list_models(self, timeout: float = 5.0) -> list[str]:
        resp = self._client.get("/models", timeout=timeout)
        if resp.status_code >= 400:
            raise GroqError(f"HTTP {resp.status_code} from /models: {_error_text(resp)}")
        self._last_used = time.monotonic()
        return sorted(m["id"] for m in resp.json().get("data", []) if m.get("active", True))


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = resp.text[:200]
    return (msg or "").strip()[:300]
