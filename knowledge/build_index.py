"""Chunk the scraped interdb.jp pages and embed them into a persistent Chroma
collection, using a local Ollama embedding model (nomic-embed-text).

Usage: python knowledge/build_index.py
Requires knowledge/raw_pages/*.txt to already exist (run scrape_interdb.py first)
and `ollama pull nomic-embed-text` to have been run.
"""
import os
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions.ollama_embedding_function import (
    OllamaEmbeddingFunction,
)
from dotenv import load_dotenv

load_dotenv()

RAW_DIR = Path(__file__).parent / "raw_pages"
CHROMA_DIR = os.environ.get("CHROMA_DB_DIR", str(Path(__file__).parent / "chroma_db"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
COLLECTION_NAME = "pg_internals"

CHUNK_SIZE = 900  # characters
CHUNK_OVERLAP = 150


def chunk_text(text: str, source: str) -> list[dict]:
    lines = text.splitlines()
    body = "\n".join(lines[2:]) if lines[:1] == ["SOURCE: " + source] else text
    chunks = []
    start = 0
    while start < len(body):
        end = start + CHUNK_SIZE
        chunk = body[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - CHUNK_OVERLAP
    return [{"text": c, "source": source} for c in chunks]


def build() -> None:
    if not RAW_DIR.exists() or not any(RAW_DIR.glob("*.txt")):
        raise SystemExit(f"No scraped pages found in {RAW_DIR}. Run scrape_interdb.py first.")

    client = chromadb.PersistentClient(path=CHROMA_DIR)
    embed_fn = OllamaEmbeddingFunction(url=OLLAMA_API_BASE, model_name=EMBED_MODEL)

    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(COLLECTION_NAME, embedding_function=embed_fn)

    ids, docs, metadatas = [], [], []
    for path in sorted(RAW_DIR.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        source_line = text.splitlines()[0].removeprefix("SOURCE: ") if text.startswith("SOURCE:") else path.name
        for i, chunk in enumerate(chunk_text(text, source_line)):
            ids.append(f"{path.stem}-{i}")
            docs.append(chunk["text"])
            metadatas.append({"source": chunk["source"], "page": path.stem})

    print(f"Embedding {len(docs)} chunks from {len(list(RAW_DIR.glob('*.txt')))} pages...")
    batch = 50
    for i in range(0, len(docs), batch):
        collection.add(
            ids=ids[i : i + batch],
            documents=docs[i : i + batch],
            metadatas=metadatas[i : i + batch],
        )
        print(f"  indexed {min(i + batch, len(docs))}/{len(docs)}")

    print(f"Done. Collection '{COLLECTION_NAME}' has {collection.count()} chunks at {CHROMA_DIR}")


if __name__ == "__main__":
    build()
