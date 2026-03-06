"""
Qdrant-based semantic search for AI interactions.

Self-contained module with:
- InteractionVectorStore: Qdrant client + OpenAI embeddings
- InteractionIndexer: Pipeline for indexing interactions with auto-tagging
"""

from .store import InteractionVectorStore, SearchResult
from .indexer import InteractionIndexer, generate_tags

__all__ = [
    "InteractionVectorStore",
    "SearchResult",
    "InteractionIndexer",
    "generate_tags",
]
