"""Optional research + LLM signal. Everything here is a no-op unless keys are configured.

* TAVILY_API_KEY             -> news snippets for the market question
* LLM_API_KEY + LLM_MODEL    -> an OpenAI-compatible chat endpoint (LLM_BASE_URL, default
                                https://api.openai.com/v1; Groq/OpenRouter/etc. also work) that
                                reads the snippets and returns P(YES).
The SLM never depends on this; when present the LLM view is blended in logit space.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional
from urllib import request

logger = logging.getLogger(__name__)


def tavily_enabled() -> bool:
    return bool(os.getenv("TAVILY_API_KEY", "").strip())


def llm_enabled() -> bool:
    return bool(os.getenv("LLM_API_KEY", "").strip() and os.getenv("LLM_MODEL", "").strip())


def research(question: str, max_results: int = 3) -> dict:
    if not tavily_enabled():
        return {"enabled": False, "answer": "", "results": []}
    try:
        from tavily import TavilyClient

        resp = TavilyClient(api_key=os.environ["TAVILY_API_KEY"].strip()).search(
            query=question, max_results=max_results, search_depth="basic", include_answer=True
        )
        results = [{"title": r.get("title", ""), "url": r.get("url", ""), "content": (r.get("content") or "")[:400]} for r in resp.get("results", [])]
        return {"enabled": True, "answer": resp.get("answer") or "", "results": results}
    except Exception as exc:  # pragma: no cover - network
        logger.warning("Tavily research failed: %s", exc)
        return {"enabled": True, "answer": "", "results": [], "error": str(exc)}


def llm_probability(question: str, market_price: float, notes: dict) -> Optional[float]:
    if not llm_enabled():
        return None
    snippets = "\n".join(f"- {r['title']}: {r['content']}" for r in notes.get("results", [])[:3])
    prompt = (
        "You are a calibrated forecaster. Estimate the probability that this prediction-market question "
        "resolves YES. Use the news below; if it is uninformative, stay close to the market price.\n"
        f"Question: {question}\nCurrent market price for YES: {market_price:.3f}\n"
        f"News summary: {notes.get('answer', '')}\n{snippets}\n"
        'Reply with JSON only: {"p_yes": <number between 0 and 1>}'
    )
    body = json.dumps({"model": os.environ["LLM_MODEL"], "temperature": 0, "messages": [{"role": "user", "content": prompt}]}).encode()
    base = (os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    req = request.Request(f"{base}/chat/completions", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {os.environ['LLM_API_KEY'].strip()}"})
    try:
        with request.urlopen(req, timeout=30) as resp:
            text = json.load(resp)["choices"][0]["message"]["content"]
        match = re.search(r"([01](?:\.\d+)?)", text.split("p_yes", 1)[-1])
        if match:
            p = float(match.group(1))
            if 0.0 < p < 1.0:
                return p
    except Exception as exc:  # pragma: no cover - network
        logger.warning("LLM probability failed: %s", exc)
    return None
