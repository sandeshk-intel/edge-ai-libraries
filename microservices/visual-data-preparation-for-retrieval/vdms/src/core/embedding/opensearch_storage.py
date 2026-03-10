# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
OpenSearch storage backend for the DataPrep microservice.

Exposes the same interface as the VDMS-based storage (SimpleVDMSClient /
SDKVDMSClient) so the upper layers (simplified_embedding_helper,
sdk_embedding_helper) can call:
    - store_frame_embeddings(embeddings, frame_metadatas) -> list[str]
    - store_text_embedding(text, metadata) -> list[str]
    - store_text_embedding_with_vector(text, vector, metadata) -> list[str]

Designed for AWS Managed OpenSearch with k-NN plugin (nmslib / HNSW).
"""

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from opensearchpy import OpenSearch, RequestsHttpConnection, helpers

from src.common import Strings, logger, settings


# ---------------------------------------------------------------------------
# Connection builder
# ---------------------------------------------------------------------------

def _build_client() -> OpenSearch:
    """
    Create an authenticated OpenSearch client.

    Auth priority:
      1. AWS IAM (if OPENSEARCH_AWS_REGION is set)
      2. Basic auth (if OPENSEARCH_USER is set)
      3. Anonymous
    """
    host = settings.OPENSEARCH_HOST
    port = int(settings.OPENSEARCH_PORT)
    use_ssl = str(settings.OPENSEARCH_USE_SSL).lower() in ("true", "1", "yes")
    verify = str(settings.OPENSEARCH_VERIFY_CERTS).lower() in ("true", "1", "yes")

    kwargs: Dict[str, Any] = {
        "hosts": [{"host": host, "port": port}],
        "use_ssl": use_ssl,
        "verify_certs": verify,
        "connection_class": RequestsHttpConnection,
        "timeout": 60,
    }

    aws_region = settings.OPENSEARCH_AWS_REGION
    if aws_region:
        try:
            import boto3
            from requests_aws4auth import AWS4Auth

            creds = boto3.Session().get_credentials()
            if creds:
                kwargs["http_auth"] = AWS4Auth(
                    creds.access_key, creds.secret_key, aws_region, "es",
                    session_token=creds.token,
                )
                logger.info("OpenSearch: AWS IAM auth (region=%s)", aws_region)
        except ImportError:
            logger.warning("boto3/requests_aws4auth missing – skipping IAM auth")

    if "http_auth" not in kwargs and settings.OPENSEARCH_USER:
        kwargs["http_auth"] = (settings.OPENSEARCH_USER, settings.OPENSEARCH_PASSWORD)
        logger.info("OpenSearch: basic auth (user=%s)", settings.OPENSEARCH_USER)

    client = OpenSearch(**kwargs)
    logger.info("OpenSearch client created → %s:%d (ssl=%s)", host, port, use_ssl)
    return client


# ---------------------------------------------------------------------------
# OpenSearch storage class
# ---------------------------------------------------------------------------

class OpenSearchStorageClient:
    """
    Drop-in replacement for SimpleVDMSClient / SDKVDMSClient storage layer.

    It writes embeddings + metadata to an OpenSearch k-NN index so the
    retriever side (search-ms ``opensearch_retriever``) can read them back.
    """

    def __init__(
        self,
        collection_name: str = "",
        embedding_dimensions: int = 512,
        multimodal_api_url: str = "",
        model_name: str = "",
    ):
        # OPENSEARCH_INDEX takes priority when set — it is the dedicated
        # frame-embedding index.  ``collection_name`` (== DB_COLLECTION) is
        # the *summary* index and must NOT override it.
        self.index_name = (
            settings.OPENSEARCH_INDEX
            or collection_name
            or settings.DB_COLLECTION
            or "video_frame_embeddings"
        )
        self.embedding_dimensions = embedding_dimensions
        self.multimodal_api_url = multimodal_api_url
        self.model_name = model_name
        self.vector_field = "embedding"

        self.client = _build_client()
        self._ensure_index()

        logger.info(
            "OpenSearchStorageClient ready (index=%s, dim=%d)",
            self.index_name, self.embedding_dimensions,
        )

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _ensure_index(self) -> None:
        """Create the k-NN index if it does not already exist."""
        if self.client.indices.exists(index=self.index_name):
            logger.info("OpenSearch index '%s' already exists", self.index_name)
            return

        body = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 256,
                    "number_of_shards": 3,
                    "number_of_replicas": 2,
                }
            },
            "mappings": {
                "properties": {
                    self.vector_field: {
                        "type": "knn_vector",
                        "dimension": self.embedding_dimensions,
                        "method": {
                            "name": "hnsw",
                            "space_type": "innerproduct",
                            "engine": "faiss",
                            "parameters": {
                                "ef_construction": 256,
                                "m": 48,
                            },
                        },
                    },
                    "page_content": {"type": "text"},
                    "metadata": {"type": "object", "enabled": True},
                }
            },
        }

        self.client.indices.create(index=self.index_name, body=body)
        logger.info(
            "Created OpenSearch k-NN index '%s' (dim=%d, engine=faiss/hnsw, space=ip)",
            self.index_name, self.embedding_dimensions,
        )

    # ------------------------------------------------------------------
    # Bulk helpers
    # ------------------------------------------------------------------

    def _bulk_index(
        self,
        embeddings: List[List[float]],
        texts: List[str],
        metadatas: List[dict],
    ) -> List[str]:
        """Index documents in bulk and return generated IDs."""
        actions = []
        ids: List[str] = []

        for emb, text, meta in zip(embeddings, texts, metadatas):
            doc_id = str(uuid.uuid4())
            ids.append(doc_id)
            actions.append({
                "_index": self.index_name,
                "_id": doc_id,
                "_source": {
                    self.vector_field: emb,
                    "page_content": text,
                    "metadata": meta,
                },
            })

        start = time.time()
        success, errors = helpers.bulk(self.client, actions, raise_on_error=False)
        elapsed = time.time() - start

        if errors:
            logger.error("Bulk index errors: %s", errors[:5])
            raise Exception(f"OpenSearch bulk index failed – {len(errors)} errors")

        logger.info(
            "Bulk indexed %d docs in %.3fs (%d success)",
            len(actions), elapsed, success,
        )
        return ids

    # ------------------------------------------------------------------
    # Public API — matches SimpleVDMSClient / SDKVDMSClient interface
    # ------------------------------------------------------------------

    def store_frame_embeddings(
        self,
        embeddings: List[List[float]],
        frame_metadatas: List[dict],
    ) -> List[str]:
        """
        Store pre-computed frame embeddings with metadata.

        Mirrors ``SimpleVDMSClient.store_frame_embeddings()`` exactly.
        """
        if not embeddings:
            return []

        if len(embeddings) != len(frame_metadatas):
            raise ValueError(
                f"Mismatch: {len(embeddings)} embeddings vs "
                f"{len(frame_metadatas)} metadata entries"
            )

        frame_texts: List[str] = []
        cleaned_metas: List[dict] = []

        for i, meta in enumerate(frame_metadatas):
            video_id = meta.get("video_id", "unknown")
            frame_num = meta.get("frame_number", i)
            frame_type = meta.get("frame_type", "full_frame")
            crop_index = meta.get("crop_index")

            if frame_type == "detected_crop" and crop_index is not None:
                text = f"frame_{frame_num}_crop_{crop_index}_{video_id}"
            else:
                text = f"frame_{frame_num}_{video_id}"

            frame_texts.append(text)
            # OpenSearch accepts nested JSON natively — no need to flatten
            cleaned_metas.append(meta)

        return self._bulk_index(embeddings, frame_texts, cleaned_metas)

    def store_text_embedding(self, text: str, metadata: dict = None) -> List[str]:
        """
        Embed *text* via the multimodal API and store the result.

        Mirrors ``SimpleVDMSClient.store_text_embedding()``.
        """
        import requests as req

        if not self.multimodal_api_url:
            raise ValueError("Multimodal API URL required for text embedding")

        resp = req.post(
            self.multimodal_api_url,
            json={
                "model": self.model_name,
                "input": {"type": "text", "text": text},
                "encoding_format": "float",
            },
            timeout=30,
        )
        resp.raise_for_status()
        embedding = resp.json()["embedding"]

        return self._bulk_index(
            [embedding],
            [text],
            [metadata or {}],
        )

    def store_text_embedding_with_vector(
        self,
        text: str,
        embedding_vector: List[float],
        metadata: dict = None,
    ) -> List[str]:
        """Store text with a pre-computed embedding vector."""
        if not embedding_vector:
            raise ValueError("Embedding vector cannot be empty")

        return self._bulk_index(
            [embedding_vector],
            [text],
            [metadata or {}],
        )

    def store_embeddings_from_manifest(self, video_metadata_path) -> dict:
        """
        Process frame metadata file and store embeddings (API mode).

        Mirrors ``SimpleVDMSClient.store_embeddings_from_manifest()``.
        """
        import pathlib
        import requests as req

        from src.core.utils.config_utils import read_config

        metadata = read_config(video_metadata_path, type="json")
        if metadata is None:
            raise Exception(Strings.metadata_read_error)

        # Try batch manifest path first
        frames_manifest_path = None
        for _, data in metadata.items():
            if isinstance(data, dict) and "frames_manifest_path" in data:
                frames_manifest_path = data["frames_manifest_path"]
                break

        if frames_manifest_path and self.multimodal_api_url:
            extracted_frames = 0
            post_detection_items = 0
            try:
                with open(frames_manifest_path) as f:
                    manifest = json.load(f)
                extracted_frames = manifest.get("total_frames") or len(manifest.get("frames", []))
                post_detection_items = manifest.get("total_metadata_entries") or extracted_frames
            except Exception:
                pass

            embed_start = time.time()
            resp = req.post(
                self.multimodal_api_url,
                json={
                    "model": self.model_name,
                    "input": {
                        "type": "frames_batch",
                        "frames_manifest_path": frames_manifest_path,
                    },
                    "encoding_format": "float",
                },
                timeout=120,
            )
            resp.raise_for_status()
            embeddings = resp.json()["embedding"]
            embedding_time = time.time() - embed_start

            frame_metas = []
            for _, data in metadata.items():
                clean = {
                    k: v for k, v in data.items()
                    if k not in ("image_path", "video_temp_path", "frames_manifest_path")
                }
                frame_metas.append(clean)

            storage_start = time.time()
            ids = self.store_frame_embeddings(embeddings, frame_metas)
            storage_time = time.time() - storage_start

            return {
                "ids": ids,
                "embedding_time": embedding_time,
                "storage_time": storage_time,
                "post_detection_items": post_detection_items or len(frame_metas),
                "extracted_frames": extracted_frames or len(frame_metas),
            }

        # Fallback: individual frames
        return self._process_individual_frames(metadata)

    def _process_individual_frames(self, metadata: dict) -> dict:
        """Fallback: embed and store frames one-by-one."""
        import requests as req

        all_ids: List[str] = []
        embed_total = 0.0
        store_total = 0.0

        for key, data in metadata.items():
            if "image_path" not in data or not self.multimodal_api_url:
                continue

            t0 = time.time()
            resp = req.post(
                self.multimodal_api_url,
                json={
                    "model": self.model_name,
                    "input": {"type": "image_file", "image_path": data["image_path"]},
                    "encoding_format": "float",
                },
                timeout=30,
            )
            resp.raise_for_status()
            embedding = resp.json()["embedding"]
            embed_total += time.time() - t0

            frame_meta = {
                k: v for k, v in data.items()
                if k not in ("image_path", "video_temp_path", "frames_manifest_path")
            }
            t1 = time.time()
            ids = self.store_frame_embeddings([embedding], [frame_meta])
            store_total += time.time() - t1
            all_ids.extend(ids)

        return {
            "ids": all_ids,
            "embedding_time": embed_total,
            "storage_time": store_total,
            "post_detection_items": len(all_ids),
            "extracted_frames": len(metadata),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def check_and_update_properties(self) -> None:
        """No-op compatibility stub (VDMS-specific index refresh)."""
        # OpenSearch handles index refresh automatically
        self.client.indices.refresh(index=self.index_name)
        logger.debug("OpenSearch index '%s' refreshed", self.index_name)
