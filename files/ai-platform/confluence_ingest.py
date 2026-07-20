import datetime as dt
import hashlib
import json
import os
import re
import uuid
from typing import Any

import requests
from bs4 import BeautifulSoup


def _env(name: str, default: str | None = None) -> str:
  value = os.getenv(name, default)
  if value is None:
    raise RuntimeError(f"Missing required environment variable: {name}")
  return value


CONFLUENCE_BASE_URL = _env("CONFLUENCE_BASE_URL").rstrip("/")
CONFLUENCE_AUTH_TYPE = _env("CONFLUENCE_AUTH_TYPE", "bearer").strip().lower()
CONFLUENCE_EMAIL = _env("CONFLUENCE_EMAIL", "")
CONFLUENCE_API_TOKEN = _env("CONFLUENCE_API_TOKEN", "")
CONFLUENCE_PAT = _env("CONFLUENCE_PAT", "")
CONFLUENCE_SPACES = [s.strip() for s in _env("CONFLUENCE_SPACES", "").split(",") if s.strip()]
CONFLUENCE_PAGE_LIMIT = int(_env("CONFLUENCE_PAGE_LIMIT", "50"))
CONFLUENCE_SINCE_DAYS = int(_env("CONFLUENCE_SINCE_DAYS", "30"))

QDRANT_URL = _env("QDRANT_URL").rstrip("/")
QDRANT_COLLECTION = _env("QDRANT_COLLECTION", "confluence-docs")
QDRANT_API_KEY = _env("QDRANT_API_KEY", "")

EMBEDDING_MODE = _env("EMBEDDING_MODE", "auto").strip().lower()

EMBEDDINGS_URL = _env("EMBEDDINGS_URL")
EMBEDDINGS_MODEL = _env("EMBEDDINGS_MODEL")
EMBEDDINGS_API_KEY = _env("EMBEDDINGS_API_KEY", "")
HASH_VECTOR_SIZE = int(_env("HASH_VECTOR_SIZE", "384"))

CHUNK_SIZE = int(_env("CHUNK_SIZE", "1100"))
CHUNK_OVERLAP = int(_env("CHUNK_OVERLAP", "150"))
REQUEST_TIMEOUT_SECONDS = int(_env("REQUEST_TIMEOUT_SECONDS", "30"))


session = requests.Session()
session.headers.update({"Content-Type": "application/json"})


def _qdrant_headers() -> dict[str, str]:
  headers = {"Content-Type": "application/json"}
  if QDRANT_API_KEY:
    headers["api-key"] = QDRANT_API_KEY
  return headers


def _normalized(value: str) -> str:
  return re.sub(r"[^a-z0-9]", "", value.lower())


def _space_matches(page: dict[str, Any]) -> bool:
  if not CONFLUENCE_SPACES:
    return True

  space = page.get("space", {})
  candidates = {
    _normalized(str(space.get("key", ""))),
    _normalized(str(space.get("name", ""))),
  }
  wanted = {_normalized(name) for name in CONFLUENCE_SPACES}
  return bool(candidates.intersection(wanted))


def _confluence_auth() -> tuple[dict[str, str], tuple[str, str] | None]:
  if CONFLUENCE_AUTH_TYPE == "bearer":
    if not CONFLUENCE_PAT:
      raise RuntimeError("CONFLUENCE_PAT is required when CONFLUENCE_AUTH_TYPE=bearer")
    return ({"Authorization": f"Bearer {CONFLUENCE_PAT}"}, None)

  if CONFLUENCE_AUTH_TYPE == "basic":
    if not CONFLUENCE_EMAIL or not CONFLUENCE_API_TOKEN:
      raise RuntimeError(
        "CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN are required when CONFLUENCE_AUTH_TYPE=basic"
      )
    return ({}, (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN))

  raise RuntimeError(f"Unsupported CONFLUENCE_AUTH_TYPE: {CONFLUENCE_AUTH_TYPE}")


def confluence_request(path: str, params: dict[str, Any]) -> dict[str, Any]:
  url = f"{CONFLUENCE_BASE_URL}{path}"
  auth_headers, auth_tuple = _confluence_auth()
  response = session.get(
    url,
    params=params,
    headers=auth_headers,
    auth=auth_tuple,
    timeout=REQUEST_TIMEOUT_SECONDS,
  )
  response.raise_for_status()
  return response.json()


def fetch_pages() -> list[dict[str, Any]]:
  cutoff_date = (dt.datetime.utcnow() - dt.timedelta(days=CONFLUENCE_SINCE_DAYS)).strftime("%Y-%m-%d")
  cql = f"type = page AND lastmodified >= '{cutoff_date}'"

  start = 0
  pages: list[dict[str, Any]] = []
  while True:
    data = confluence_request(
      "/rest/api/content/search",
      {
        "cql": cql,
        "start": start,
        "limit": CONFLUENCE_PAGE_LIMIT,
        "expand": "body.storage,body.view,version,space",
      },
    )
    results = data.get("results", [])
    batch = [p for p in results if _space_matches(p)]
    pages.extend(batch)
    # Continue pagination until Confluence result pages are exhausted.
    if len(results) < CONFLUENCE_PAGE_LIMIT:
      break
    start += CONFLUENCE_PAGE_LIMIT
  return pages


def html_to_text(html: str) -> str:
  soup = BeautifulSoup(html, "html.parser")
  text = soup.get_text(separator="\n")
  text = re.sub(r"\n{3,}", "\n\n", text)
  return text.strip()


def chunk_text(text: str) -> list[str]:
  if len(text) <= CHUNK_SIZE:
    return [text]
  chunks = []
  start = 0
  step = max(1, CHUNK_SIZE - CHUNK_OVERLAP)
  while start < len(text):
    end = min(len(text), start + CHUNK_SIZE)
    chunks.append(text[start:end])
    start += step
  return chunks


def _l2_normalize(vector: list[float]) -> list[float]:
  norm = sum(v * v for v in vector) ** 0.5
  if norm == 0:
    return vector
  return [v / norm for v in vector]


def _local_hash_embedding(text: str) -> list[float]:
  vec = [0.0] * HASH_VECTOR_SIZE
  for token in re.findall(r"\w+", text.lower()):
    h = hashlib.sha1(token.encode("utf-8")).digest()
    idx = int.from_bytes(h[:4], "big") % HASH_VECTOR_SIZE
    sign = -1.0 if (h[4] & 1) else 1.0
    vec[idx] += sign
  return _l2_normalize(vec)


def _local_hash_embeddings(texts: list[str]) -> list[list[float]]:
  return [_local_hash_embedding(text) for text in texts]


def embed_texts(texts: list[str]) -> list[list[float]]:
  if EMBEDDING_MODE == "local-hash":
    return _local_hash_embeddings(texts)

  headers = {"Content-Type": "application/json"}
  if EMBEDDINGS_API_KEY:
    headers["Authorization"] = f"Bearer {EMBEDDINGS_API_KEY}"

  response = session.post(
    EMBEDDINGS_URL,
    headers=headers,
    data=json.dumps({"model": EMBEDDINGS_MODEL, "input": texts}),
    timeout=REQUEST_TIMEOUT_SECONDS,
  )
  if response.status_code >= 400:
    if EMBEDDING_MODE == "auto":
      print(
        f"Embedding endpoint failed ({response.status_code}), falling back to local-hash embeddings"
      )
      return _local_hash_embeddings(texts)
    response.raise_for_status()

  data = response.json().get("data", [])
  if not data:
    if EMBEDDING_MODE == "auto":
      print("Embedding endpoint returned empty data, falling back to local-hash embeddings")
      return _local_hash_embeddings(texts)
    raise RuntimeError("Embeddings response has no data")
  return [item["embedding"] for item in data]


def ensure_collection(vector_size: int) -> None:
  get_resp = session.get(
    f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
    headers=_qdrant_headers(),
    timeout=REQUEST_TIMEOUT_SECONDS,
  )
  if get_resp.status_code == 200:
    return

  create_payload = {
    "vectors": {
      "size": vector_size,
      "distance": "Cosine",
    }
  }
  create_resp = session.put(
    f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
    headers=_qdrant_headers(),
    data=json.dumps(create_payload),
    timeout=REQUEST_TIMEOUT_SECONDS,
  )
  create_resp.raise_for_status()


def upsert_points(points: list[dict[str, Any]]) -> None:
  payload = {"points": points}
  response = session.put(
    f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points?wait=true",
    headers=_qdrant_headers(),
    data=json.dumps(payload),
    timeout=REQUEST_TIMEOUT_SECONDS,
  )
  response.raise_for_status()


def page_url(base_url: str, page: dict[str, Any]) -> str:
  links = page.get("_links", {})
  webui = links.get("webui")
  links_base = links.get("base")
  if links_base and webui:
    return f"{links_base}{webui}"
  if webui:
    return f"{base_url}{webui}"
  return f"{base_url}/pages/viewpage.action?pageId={page.get('id', '')}"


def build_points(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
  points: list[dict[str, Any]] = []
  for page in pages:
    page_id = str(page.get("id", ""))
    title = page.get("title", "untitled")
    body = page.get("body", {})
    body_html = body.get("storage", {}).get("value", "")
    if not body_html:
      body_html = body.get("view", {}).get("value", "")
    plain_text = html_to_text(body_html)
    if not plain_text:
      continue

    chunks = chunk_text(plain_text)
    embeddings = embed_texts(chunks)
    for index, (chunk, vector) in enumerate(zip(chunks, embeddings)):
      source_key = f"{page_id}:{index}:{page.get('version', {}).get('number', 0)}"
      point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source_key))
      points.append(
        {
          "id": point_id,
          "vector": vector,
          "payload": {
            "source": "confluence",
            "space_key": page.get("space", {}).get("key", "unknown"),
            "page_id": page_id,
            "title": title,
            "url": page_url(CONFLUENCE_BASE_URL, page),
            "version": page.get("version", {}).get("number", 0),
            "updated_at": page.get("version", {}).get("when", ""),
            "chunk_index": index,
            "text": chunk,
          },
        }
      )
  return points


def main() -> None:
  print("Starting Confluence ingestion...")
  pages = fetch_pages()
  print(f"Fetched {len(pages)} pages from Confluence")
  if not pages:
    print("No pages to ingest")
    return

  points = build_points(pages)
  print(f"Prepared {len(points)} vector points")
  if not points:
    print("No chunks generated, exiting")
    return

  vector_size = len(points[0]["vector"])
  ensure_collection(vector_size)
  upsert_points(points)
  print(f"Upserted {len(points)} points into collection '{QDRANT_COLLECTION}'")


if __name__ == "__main__":
  main()
