"""Shared Ollama client for Night Shift agents.

The inference endpoint and model are configurable via environment
variables OLLAMA_URL and OLLAMA_MODEL, defaulting to the local Ollama
instance. Local behavior is unchanged when these are absent.
"""
import json
import os
import time
from typing import Any

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")


def call_model(system_prompt: str, user_prompt: str, expect_json: bool = True, timeout: float = None) -> dict[str, Any]:
    """Call the local Ollama model and return structured metadata.

    Returns a dict with keys:
      - success: bool
      - response: parsed JSON dict or raw string
      - raw: raw response string
      - elapsed_ms: float
      - error: str or None
    """
    if timeout is None:
        timeout = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
    prompt = f"{system_prompt}\n\n{user_prompt}\n\nResponse:"
    payload = {
        "model": MODEL,
        "think": False,
        "format": "json" if expect_json else None,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 1024, "temperature": 0.0},
    }

    start = time.perf_counter()
    try:
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {"success": False, "response": None, "raw": "", "elapsed_ms": elapsed_ms, "error": str(exc)}

    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    raw = data.get("response", "").strip()

    if expect_json:
        try:
            parsed = json.loads(raw)
            return {"success": True, "response": parsed, "raw": raw, "elapsed_ms": elapsed_ms, "error": None}
        except json.JSONDecodeError as exc:
            return {"success": False, "response": None, "raw": raw, "elapsed_ms": elapsed_ms, "error": f"Malformed JSON: {exc}"}

    return {"success": True, "response": raw, "raw": raw, "elapsed_ms": elapsed_ms, "error": None}
