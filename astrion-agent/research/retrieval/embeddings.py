"""Optional multilingual local encoder; model service credentials are never reused."""

import os
import threading
from functools import lru_cache


class FastEmbedEncoder:
    def __init__(self, model, cache):
        from fastembed import TextEmbedding

        self.name = "fastembed:" + model
        self.model = TextEmbedding(model_name=model, cache_dir=cache, threads=2)
        self.lock = threading.Lock()

    def encode(self, texts, *, query=False):
        with self.lock:
            method = self.model.query_embed if query else self.model.passage_embed
            return [v.tolist() for v in method(texts)]


@lru_cache(maxsize=4)
def configured_encoder(backend, model, cache):
    if backend == "none":
        return None
    if backend != "fastembed":
        raise ValueError("unsupported_embedding_backend")
    return FastEmbedEncoder(model, cache)


def from_env(cache):
    return configured_encoder(
        os.environ.get("ASTRION_EMBEDDING_BACKEND", "none"),
        os.environ.get(
            "ASTRION_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        ),
        (os.environ.get("ASTRION_MODEL_CACHE") or str(cache)),
    )
