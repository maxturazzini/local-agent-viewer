"""
ConversationVectorStore - Qdrant client with integrated OpenAI embeddings.

Provides semantic search for indexed Claude conversations.
Supports both local file mode and remote HTTP mode (Qdrant server).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import os
import hashlib

from qdrant_client import QdrantClient, models
from openai import OpenAI


@dataclass
class SearchResult:
    """Result from semantic search."""
    score: float
    payload: Dict[str, Any]
    session_id: str


class ConversationVectorStore:
    """Qdrant store + OpenAI embeddings in one class.

    Supports two connection modes:
    - File mode: QdrantClient(path=...) — local embedded, no server needed
    - HTTP mode: QdrantClient(url=...) — connects to a Qdrant HTTP server
    """

    VECTOR_SIZE = 1536  # text-embedding-3-small
    EMBEDDING_MODEL = "text-embedding-3-small"

    def __init__(
        self,
        data_path: Optional[Path] = None,
        collection: str = "conversations",
        openai_api_key: Optional[str] = None,
        url: Optional[str] = None,
    ):
        """
        Args:
            url: Qdrant HTTP server URL (e.g. "http://your-server:6333").
                 Takes priority over data_path.
            data_path: Path to Qdrant data directory (file/embedded mode).
            collection: Collection name.
            openai_api_key: OpenAI API key (defaults to OPENAI_API_KEY env var).
        """
        self.collection = collection
        self._mode = "http" if url else "file"

        if url:
            self.data_path = None
            self.client = QdrantClient(url=url, check_compatibility=False)
        elif data_path is not None:
            self.data_path = Path(data_path)
            self.client = QdrantClient(path=str(self.data_path))
        else:
            raise ValueError("Either url or data_path must be provided.")

        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key required. Set OPENAI_API_KEY env var.")
        self.openai = OpenAI(api_key=api_key)

        self._embed_cache: Dict[str, List[float]] = {}

    def ensure_collection(self, recreate: bool = False) -> None:
        """Create collection if it doesn't exist."""
        collections = [c.name for c in self.client.get_collections().collections]

        if recreate and self.collection in collections:
            self.client.delete_collection(self.collection)
            print(f"[info] Collection '{self.collection}' deleted")
            collections.remove(self.collection)

        if self.collection not in collections:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self.VECTOR_SIZE,
                    distance=models.Distance.COSINE,
                ),
            )
            print(f"[info] Collection '{self.collection}' created")

    def embed(self, text: str) -> List[float]:
        """Embed text with OpenAI (cached by content hash)."""
        text = text[:8000]
        cache_key = hashlib.md5(text.encode()).hexdigest()

        if cache_key in self._embed_cache:
            return self._embed_cache[cache_key]

        response = self.openai.embeddings.create(
            model=self.EMBEDDING_MODEL,
            input=text
        )
        vector = response.data[0].embedding

        if len(self._embed_cache) < 100:
            self._embed_cache[cache_key] = vector

        return vector

    def upsert(self, session_id: str, vector: List[float], payload: Dict) -> None:
        """Insert or update a vector."""
        point_id = self._session_to_id(session_id)

        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={**payload, "session_id": session_id}
                )
            ],
            wait=True
        )

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict] = None
    ) -> List[SearchResult]:
        """Semantic search with optional filters."""
        query_vector = self.embed(query)
        qdrant_filter = self._build_filter(filters) if filters else None

        response = self.client.query_points(
            collection_name=self.collection,
            query=query_vector,
            limit=limit,
            query_filter=qdrant_filter,
        )

        return [
            SearchResult(
                score=r.score,
                payload=r.payload or {},
                session_id=r.payload.get("session_id", "") if r.payload else ""
            )
            for r in response.points
        ]

    def delete(self, session_id: str) -> bool:
        """Remove a vector by session_id."""
        point_id = self._session_to_id(session_id)

        self.client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=[point_id]),
            wait=True
        )
        return True

    def update_tags(self, session_id: str, tags: List[str]) -> bool:
        """Update only tags without re-embedding."""
        point_id = self._session_to_id(session_id)

        self.client.set_payload(
            collection_name=self.collection,
            payload={"tags": tags},
            points=[point_id],
            wait=True
        )
        return True

    def is_indexed(self, session_id: str) -> bool:
        """Check if conversation is indexed."""
        point_id = self._session_to_id(session_id)

        try:
            result = self.client.retrieve(
                collection_name=self.collection,
                ids=[point_id]
            )
            return len(result) > 0
        except Exception:
            return False

    def get(self, session_id: str) -> Optional[Dict]:
        """Retrieve payload for a session."""
        point_id = self._session_to_id(session_id)

        try:
            result = self.client.retrieve(
                collection_name=self.collection,
                ids=[point_id],
                with_payload=True
            )
            return result[0].payload if result else None
        except Exception:
            return None

    def list_all_tags(self) -> Dict[str, int]:
        """List all tags with usage count."""
        tag_counts: Dict[str, int] = {}

        offset = None
        while True:
            results, offset = self.client.scroll(
                collection_name=self.collection,
                limit=100,
                offset=offset,
                with_payload=True
            )

            for point in results:
                tags = point.payload.get("tags", []) if point.payload else []
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

            if offset is None:
                break

        return tag_counts

    def count(self, filters: Optional[Dict] = None) -> int:
        """Count indexed conversations with optional filters."""
        qdrant_filter = self._build_filter(filters) if filters else None

        result = self.client.count(
            collection_name=self.collection,
            count_filter=qdrant_filter,
            exact=True
        )
        return result.count

    def _session_to_id(self, session_id: str) -> int:
        """Convert session_id string to Qdrant-compatible int ID."""
        return int(hashlib.md5(session_id.encode()).hexdigest()[:15], 16)

    def _build_filter(self, filters: Dict) -> models.Filter:
        """Build Qdrant filter from dict."""
        conditions = []

        for key, value in filters.items():
            if isinstance(value, list):
                conditions.append(
                    models.FieldCondition(
                        key=key,
                        match=models.MatchAny(any=value)
                    )
                )
            else:
                conditions.append(
                    models.FieldCondition(
                        key=key,
                        match=models.MatchValue(value=value)
                    )
                )

        return models.Filter(must=conditions) if conditions else None
