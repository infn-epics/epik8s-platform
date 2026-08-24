import fnmatch
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests
from fastmcp import FastMCP


app = FastMCP("rag-documentation")

RAGFLOW_BASE_URL = os.environ["RAGFLOW_BASE_URL"].rstrip("/")
RAGFLOW_API_KEY = os.environ["RAGFLOW_API_KEY"]
DATASET_NAME_GLOBS = os.getenv("DATASET_NAME_GLOBS", "*")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
DATASET_CACHE_TTL_SECONDS = int(os.getenv("DATASET_CACHE_TTL_SECONDS", "300"))

session = requests.Session()
_dataset_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])


def _headers() -> dict[str, str]:
  return {
    "Authorization": f"Bearer {RAGFLOW_API_KEY}",
    "Content-Type": "application/json",
  }


def _response_data(response: requests.Response) -> Any:
  response.raise_for_status()
  payload = response.json()
  if payload.get("code", 0) != 0:
    raise RuntimeError(f"RAGFlow error: {payload.get('message', 'unknown error')}")
  return payload.get("data")


def _all_datasets(force_refresh: bool = False) -> list[dict[str, Any]]:
  global _dataset_cache

  cached_at, cached_datasets = _dataset_cache
  if not force_refresh and cached_datasets and time.monotonic() - cached_at < DATASET_CACHE_TTL_SECONDS:
    return cached_datasets

  datasets_by_id: dict[str, dict[str, Any]] = {}
  page_size = 100
  for page in range(1, 101):
    response = session.get(
      f"{RAGFLOW_BASE_URL}/api/v1/datasets",
      headers=_headers(),
      params={"page": page, "page_size": page_size},
      timeout=REQUEST_TIMEOUT_SECONDS,
    )
    batch = _response_data(response) or []
    for dataset in batch:
      if dataset.get("id"):
        datasets_by_id[dataset["id"]] = dataset
    if len(batch) < page_size:
      break

  datasets = sorted(datasets_by_id.values(), key=lambda item: item.get("name", "").lower())
  _dataset_cache = (time.monotonic(), datasets)
  return datasets


def _patterns(name_globs: str = "") -> list[str]:
  configured = name_globs.strip() or DATASET_NAME_GLOBS
  return [pattern.strip().lower() for pattern in configured.split(",") if pattern.strip()]


def _selected_datasets(name_globs: str = "", force_refresh: bool = False) -> list[dict[str, Any]]:
  patterns = _patterns(name_globs)
  return [
    dataset
    for dataset in _all_datasets(force_refresh)
    if any(fnmatch.fnmatchcase(str(dataset.get("name", "")).lower(), pattern) for pattern in patterns)
  ]


def _dataset_summary(dataset: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": dataset.get("id"),
    "name": dataset.get("name"),
    "description": dataset.get("description", ""),
    "document_count": dataset.get("document_count", 0),
    "chunk_count": dataset.get("chunk_count", 0),
  }


def _search_dataset(
  dataset: dict[str, Any],
  query: str,
  limit: int,
  similarity_threshold: float,
) -> dict[str, Any]:
  response = requests.post(
    f"{RAGFLOW_BASE_URL}/api/v1/retrieval",
    headers=_headers(),
    json={
      "question": query,
      "dataset_ids": [dataset["id"]],
      "page_size": limit,
      "similarity_threshold": similarity_threshold,
    },
    timeout=REQUEST_TIMEOUT_SECONDS,
  )
  return _response_data(response) or {}


@app.tool()
def list_datasets(name_globs: str = "", force_refresh: bool = False) -> dict[str, Any]:
  """List accessible RAGFlow documentation datasets, optionally filtered by comma-separated name globs."""
  datasets = _selected_datasets(name_globs, force_refresh)
  return {
    "configured_name_globs": _patterns(name_globs),
    "dataset_count": len(datasets),
    "datasets": [_dataset_summary(dataset) for dataset in datasets],
  }


@app.tool()
def search_documentation(
  query: str,
  limit: int = 8,
  name_globs: str = "",
  similarity_threshold: float = 0.2,
) -> dict[str, Any]:
  """Search all configured RAGFlow documentation datasets and return the most relevant chunks."""
  query = query.strip()
  if not query:
    raise ValueError("query must not be empty")

  datasets = _selected_datasets(name_globs)
  if not datasets:
    return {
      "query": query,
      "dataset_count": 0,
      "datasets": [],
      "matches": [],
      "note": "No accessible dataset matches the configured name globs.",
    }

  bounded_limit = max(1, min(limit, 50))
  bounded_threshold = max(0.0, min(similarity_threshold, 1.0))
  searchable = [dataset for dataset in datasets if int(dataset.get("chunk_count") or 0) > 0]
  matches = []
  errors = []
  total = 0
  with ThreadPoolExecutor(max_workers=min(8, max(1, len(searchable)))) as executor:
    futures = {
      executor.submit(_search_dataset, dataset, query, bounded_limit, bounded_threshold): dataset
      for dataset in searchable
    }
    for future in as_completed(futures):
      dataset = futures[future]
      try:
        result = future.result()
      except Exception as exc:
        errors.append({"dataset": dataset.get("name", ""), "error": str(exc)})
        continue

      total += int(result.get("total") or 0)
      for chunk in result.get("chunks") or []:
        matches.append(
          {
            "dataset": dataset.get("name", ""),
            "document": chunk.get("document_name") or chunk.get("docnm_kwd", ""),
            "content": chunk.get("content") or chunk.get("content_with_weight", ""),
            "similarity": chunk.get("similarity"),
            "document_id": chunk.get("document_id") or chunk.get("doc_id"),
          }
        )

  matches.sort(key=lambda match: float(match.get("similarity") or 0), reverse=True)

  return {
    "query": query,
    "dataset_count": len(datasets),
    "datasets": [dataset.get("name", "") for dataset in datasets],
    "matches": matches[:bounded_limit],
    "total": total,
    "dataset_errors": errors,
  }


if __name__ == "__main__":
  app.run(transport="streamable-http", host="0.0.0.0", port=8080)
