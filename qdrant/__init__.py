"""
Qdrant-based semantic search for Claude conversations.

Self-contained module with:
- ConversationVectorStore: Qdrant client + OpenAI embeddings
- ConversationIndexer: Pipeline for indexing conversations with auto-tagging
"""

from .store import ConversationVectorStore, SearchResult
from .indexer import ConversationIndexer, generate_tags

__all__ = [
    "ConversationVectorStore",
    "SearchResult",
    "ConversationIndexer",
    "generate_tags",
]
