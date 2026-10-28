"""A bounded one-shot call to the ai-node's local model, for production's services.

Production fetches the news and decides what to do with the answer; the ai-node only supplies the
model. So this takes text in and gives text back: the model is the runner's own (never chosen by the
caller), the request is size-capped, and nothing here reaches the internet.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

MAX_INPUT_CHARS = 40_000
MAX_TOKENS = 4_000
TIMEOUT_SECONDS = 600
MAX_CONCURRENT = 2


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    user: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    json_output: bool = True
    think: bool = False
    max_tokens: int = Field(default=700, ge=16, le=MAX_TOKENS)


class Completer:
    """Runs the calls and counts the ones in flight, so the ai-node does not sleep in the middle."""

    def __init__(self, post: Any = requests.post) -> None:
        self._post = post
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._lock = threading.Lock()
        self._inflight = 0

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def run(self, request: CompleteRequest) -> dict[str, Any]:
        if len(request.system) + len(request.user) > MAX_INPUT_CHARS:
            raise HTTPException(status_code=413, detail="the request is too long")
        if os.getenv("TRADINGAGENTS_LLM_PROVIDER", "ollama") != "ollama":
            raise HTTPException(status_code=501, detail="the runner's model is not a local Ollama model")
        model = os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "")
        if not model:
            raise HTTPException(status_code=503, detail="the runner has no model configured")
        base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/").removesuffix("/v1")
        if not self._slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="the model is busy with other requests")
        with self._lock:
            self._inflight += 1
        started = time.monotonic()
        try:
            body: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "stream": False,
                "think": request.think,
                "options": {"temperature": 0, "num_predict": request.max_tokens},
            }
            if request.json_output:
                body["format"] = "json"
            try:
                response = self._post(f"{base}/api/chat", json=body, timeout=TIMEOUT_SECONDS)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise HTTPException(
                    status_code=502, detail=f"the model call failed: {type(exc).__name__}"
                ) from None
        finally:
            with self._lock:
                self._inflight -= 1
            self._slots.release()
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            raise HTTPException(status_code=502, detail="the model returned an unexpected answer")
        return {
            "content": str(message.get("content", "")),
            "model": model,
            "seconds": round(time.monotonic() - started, 1),
            "output_tokens": payload.get("eval_count"),
            "done_reason": payload.get("done_reason"),
        }
