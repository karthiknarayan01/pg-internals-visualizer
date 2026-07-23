"""RAG retrieval over the interdb.jp PostgreSQL internals knowledge base
built by knowledge/build_index.py."""
import os

import chromadb
from chromadb.utils.embedding_functions.ollama_embedding_function import (
    OllamaEmbeddingFunction,
)

CHROMA_DIR = os.environ.get("CHROMA_DB_DIR", "./knowledge/chroma_db")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
COLLECTION_NAME = "pg_internals"

_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        embed_fn = OllamaEmbeddingFunction(url=OLLAMA_API_BASE, model_name=EMBED_MODEL)
        _collection = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
    return _collection


def query_internals(concept: str, n_results: int = 3) -> dict:
    """Retrieve passages from the PostgreSQL internals book (interdb.jp) that
    explain a given concept (e.g. "MVCC snapshot", "hash join", "sequential
    scan"). Use this to ground any explanation of PostgreSQL internal
    behavior in the real documentation instead of guessing.

    Args:
        concept: A short phrase describing the internals concept to look up.
        n_results: How many passages to return (default 3).

    Returns:
        {"status": "success", "passages": [{"text": ..., "source": ...}, ...]}
    """
    try:
        collection = _get_collection()
        res = collection.query(query_texts=[concept], n_results=n_results)
        passages = [
            {"text": doc, "source": meta.get("source")}
            for doc, meta in zip(res["documents"][0], res["metadatas"][0])
        ]
        return {"status": "success", "passages": passages}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
