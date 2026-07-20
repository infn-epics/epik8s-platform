import os
import re
from typing import Any

import requests
from fastmcp import FastMCP


app = FastMCP("rag-mcp")

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant.ai-platform.svc.cluster.local:6333").rstrip("/")
DEFAULT_COLLECTION = os.getenv("DEFAULT_COLLECTION", "confluence-docs")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))


session = requests.Session()


def _qdrant_headers() -> dict[str, str]:
  headers = {"Content-Type": "application/json"}
  if QDRANT_API_KEY:
    headers["api-key"] = QDRANT_API_KEY
  return headers


def _tokenize(query: str) -> list[str]:
  return [t for t in re.findall(r"[a-zA-Z0-9]+", query.lower()) if len(t) >= 3]


def _snippet(text: str, max_len: int = 280) -> str:
  if len(text) <= max_len:
    return text
  return text[: max_len - 3] + "..."


def _score_point(payload: dict[str, Any], tokens: list[str]) -> float:
  title = str(payload.get("title", "")).lower()
  text = str(payload.get("text", "")).lower()
  score = 0.0
  for token in tokens:
    if token in title:
      score += 5.0
    score += float(text.count(token))
  return score


def _iter_points(collection: str, max_points: int = 5000) -> list[dict[str, Any]]:
  points: list[dict[str, Any]] = []
  offset = None
  while len(points) < max_points:
    payload = {
      "limit": 256,
      "with_payload": True,
      "with_vector": False,
    }
    if offset is not None:
      payload["offset"] = offset

    resp = session.post(
      f"{QDRANT_URL}/collections/{collection}/points/scroll",
      headers=_qdrant_headers(),
      json=payload,
      timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    result = resp.json().get("result", {})
    batch = result.get("points", [])
    if not batch:
      break

    points.extend(batch)
    offset = result.get("next_page_offset")
    if offset is None:
      break

  return points


@app.tool()
def search_confluence(
  query: str,
  space_key: str = "LDCG",
  limit: int = 5,
  collection: str = "",
) -> dict[str, Any]:
  """Search ingested Confluence chunks in Qdrant and return top matching passages."""
  effective_collection = collection.strip() or DEFAULT_COLLECTION
  tokens = _tokenize(query)
  if not tokens:
    return {
      "collection": effective_collection,
      "space_key": space_key,
      "query": query,
      "matches": [],
      "note": "Query is too short. Provide a more specific query.",
    }

  points = _iter_points(effective_collection)
  wanted_space = space_key.strip().lower()
  ranked = []
  for p in points:
    payload = p.get("payload", {})
    if wanted_space and str(payload.get("space_key", "")).lower() != wanted_space:
      continue

    score = _score_point(payload, tokens)
    if score <= 0:
      continue

    ranked.append(
      {
        "score": round(score, 3),
        "title": payload.get("title", ""),
        "url": payload.get("url", ""),
        "updated_at": payload.get("updated_at", ""),
        "chunk_index": payload.get("chunk_index", 0),
        "snippet": _snippet(str(payload.get("text", ""))),
      }
    )

  ranked.sort(key=lambda x: x["score"], reverse=True)
  return {
    "collection": effective_collection,
    "space_key": space_key,
    "query": query,
    "matches": ranked[: max(1, min(limit, 20))],
    "searched_points": len(points),
  }


@app.tool()
def get_collection_stats(collection: str = "") -> dict[str, Any]:
  """Return Qdrant collection metadata, including points count and vector configuration."""
  effective_collection = collection.strip() or DEFAULT_COLLECTION
  resp = session.get(
    f"{QDRANT_URL}/collections/{effective_collection}",
    headers=_qdrant_headers(),
    timeout=REQUEST_TIMEOUT_SECONDS,
  )
  resp.raise_for_status()
  return resp.json().get("result", {})


if __name__ == "__main__":
  app.run(transport="streamable-http", host="0.0.0.0", port=8080)
