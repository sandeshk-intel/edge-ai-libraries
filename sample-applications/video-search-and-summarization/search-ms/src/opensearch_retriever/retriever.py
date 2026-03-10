# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
OpenSearch-based vector retriever for Video Search.

Drop-in replacement for the VDMS retriever, exposing the same interface:
  - get_vectordb()  -> object with similarity_search_with_score()
  - aggregate_frame_results_to_videos()  (re-exported from shared code)

Designed for AWS Managed OpenSearch Service with k-NN plugin enabled.
"""

import os
import time
from typing import List, Tuple, Any, Dict, Optional

from opensearchpy import OpenSearch, RequestsHttpConnection
from langchain_core.documents import Document

from src.utils.common import logger, settings
from src.vdms_retriever.embedding_wrapper import EmbeddingAPI
# Re-export aggregation — it is DB-agnostic and works on Document objects
from src.vdms_retriever.retriever import aggregate_frame_results_to_videos  # noqa: F401


# ---------------------------------------------------------------------------
# OpenSearch connection helpers
# ---------------------------------------------------------------------------

def _build_opensearch_client() -> OpenSearch:
    """
    Build an OpenSearch client configured for AWS Managed OpenSearch or
    a self-hosted OpenSearch cluster.

    Supported auth methods (evaluated in order):
      1. AWS IAM via requests_aws4auth (if OPENSEARCH_AWS_REGION is set)
      2. Basic auth (if OPENSEARCH_USER is set)
      3. Anonymous / no auth
    """
    host = settings.OPENSEARCH_HOST
    port = int(settings.OPENSEARCH_PORT)
    use_ssl = str(settings.OPENSEARCH_USE_SSL).lower() in ("true", "1", "yes")
    verify_certs = str(settings.OPENSEARCH_VERIFY_CERTS).lower() in ("true", "1", "yes")

    kwargs: Dict[str, Any] = {
        "hosts": [{"host": host, "port": port}],
        "use_ssl": use_ssl,
        "verify_certs": verify_certs,
        "connection_class": RequestsHttpConnection,
        "timeout": 60,
    }

    # --- AWS IAM auth (preferred for AWS Managed OpenSearch) ---------------
    aws_region = getattr(settings, "OPENSEARCH_AWS_REGION", "") or os.getenv("OPENSEARCH_AWS_REGION", "")
    if aws_region:
        try:
            import boto3
            from requests_aws4auth import AWS4Auth

            credentials = boto3.Session().get_credentials()
            if credentials:
                aws_auth = AWS4Auth(
                    credentials.access_key,
                    credentials.secret_key,
                    aws_region,
                    "es",
                    session_token=credentials.token,
                )
                kwargs["http_auth"] = aws_auth
                logger.info("OpenSearch client using AWS IAM authentication (region=%s)", aws_region)
            else:
                logger.warning("AWS region set but no credentials found – falling back")
        except ImportError:
            logger.warning("boto3 / requests_aws4auth not installed – skipping AWS IAM auth")

    # --- Basic auth --------------------------------------------------------
    if "http_auth" not in kwargs:
        user = settings.OPENSEARCH_USER
        password = settings.OPENSEARCH_PASSWORD
        if user:
            kwargs["http_auth"] = (user, password)
            logger.info("OpenSearch client using basic auth (user=%s)", user)

    client = OpenSearch(**kwargs)
    logger.info(
        "OpenSearch client created: host=%s port=%d ssl=%s verify=%s",
        host, port, use_ssl, verify_certs,
    )
    return client


# ---------------------------------------------------------------------------
# Lightweight vector-store wrapper (VDMS-compatible interface)
# ---------------------------------------------------------------------------

class OpenSearchVectorStore:
    """
    Thin wrapper around opensearch-py that presents the same
    ``similarity_search_with_score()`` interface used by the VDMS LangChain
    vectorstore so the rest of search-ms works unchanged.
    """

    def __init__(
        self,
        client: OpenSearch,
        embedding: EmbeddingAPI,
        index_name: str,
        vector_field: str = "embedding",
    ):
        self.client = client
        self.embedding = embedding
        self.index_name = index_name
        self.vector_field = vector_field

    # ------------------------------------------------------------------
    def similarity_search_with_score(
        self,
        query: str,
        k: int = 10,
        fetch_k: int = 0,
        **kwargs,
    ) -> List[Tuple[Document, float]]:
        """
        Embed *query*, run a k-NN search against the OpenSearch index,
        and return ``(Document, score)`` pairs identical to what the
        LangChain VDMS vectorstore returns.

        ``fetch_k`` is accepted for API compatibility but ignored (OpenSearch
        handles candidate selection internally via its k-NN engine).
        """
        # 1. Embed the query text
        embed_start = time.perf_counter()
        query_vector = self.embedding.embed_query(query)
        embed_ms = (time.perf_counter() - embed_start) * 1000
        logger.debug("Query embedding generated in %.1f ms (dim=%d)", embed_ms, len(query_vector))

        # 2. Build the k-NN query
        body = {
            "size": k,
            "_source": {"excludes": [self.vector_field]},  # don't return bulky vectors
            "query": {
                "knn": {
                    self.vector_field: {
                        "vector": query_vector,
                        "k": k,
                    }
                }
            },
        }

        # 3. Execute
        search_start = time.perf_counter()
        response = self.client.search(index=self.index_name, body=body)
        search_ms = (time.perf_counter() - search_start) * 1000
        hits = response.get("hits", {}).get("hits", [])
        logger.info(
            "OpenSearch k-NN search completed in %.1f ms – %d hits (requested k=%d)",
            search_ms, len(hits), k,
        )

        # 4. Convert to (Document, score) tuples
        results: List[Tuple[Document, float]] = []
        for hit in hits:
            source = hit.get("_source", {})
            score = float(hit.get("_score", 0.0))

            # Metadata lives either at top-level or nested under "metadata"
            metadata: dict = source.get("metadata", {})
            if not metadata:
                # Fallback: treat the entire _source (minus vector) as metadata
                metadata = {k: v for k, v in source.items() if k != self.vector_field}

            # Ensure page_content exists (some ingest flows put text in 'content' or 'text')
            page_content = (
                source.get("page_content")
                or source.get("content")
                or source.get("text")
                or metadata.get("page_content")
                or ""
            )

            doc = Document(page_content=str(page_content), metadata=metadata)
            results.append((doc, score))

        return results


# ---------------------------------------------------------------------------
# Public entry-points (mirror vdms_retriever.retriever)
# ---------------------------------------------------------------------------

_os_client: Optional[OpenSearch] = None
_vectordb: Optional[OpenSearchVectorStore] = None


def get_vectordb() -> OpenSearchVectorStore:
    """
    Initialise and return an OpenSearch-backed vector store.
    The instance is cached for the lifetime of the process.
    """
    global _os_client, _vectordb

    if _vectordb is not None:
        return _vectordb

    embeddings = EmbeddingAPI(
        api_url=settings.EMBEDDINGS_ENDPOINT,
        model_name=settings.EMBEDDINGS_MODEL_NAME,
    )

    # Probe embedding dimension (same as VDMS path)
    _ = embeddings.get_embedding_length()

    _os_client = _build_opensearch_client()

    index_name = (
        settings.OPENSEARCH_INDEX
        or settings.INDEX_NAME
        or "video_frame_embeddings"
    )

    _vectordb = OpenSearchVectorStore(
        client=_os_client,
        embedding=embeddings,
        index_name=index_name,
    )

    logger.info("OpenSearch vector store initialised (index=%s)", index_name)
    return _vectordb
