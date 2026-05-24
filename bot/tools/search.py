"""Tavily web search tool for PyFlue agent.

Wraps the Tavily Search API to provide the agent with
real-time web research capabilities for market analysis.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None  # type: ignore[assignment,misc]


def _get_client() -> Any:
    if TavilyClient is None:
        raise RuntimeError("tavily-python is required. Install with: pip install tavily-python")
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY environment variable is not set")
    return TavilyClient(api_key=api_key)


async def tavily_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the web using Tavily.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return (default 5).

    Returns:
        Dict with 'results' (list of {title, url, content}) and 'answer' (str).
    """
    try:
        client = _get_client()
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth="advanced",
            include_answer=True,
        )
        results = []
        for r in response.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", "")[:500],
            })
        return {
            "answer": response.get("answer", ""),
            "results": results,
            "query": query,
        }
    except Exception as exc:
        logger.error(f"Tavily search failed: {exc}")
        return {"answer": "", "results": [], "query": query, "error": str(exc)}
