"""Sentence embeddings: a BERT encoder written directly against MLX.

Why hand-rolled rather than a library: every off-the-shelf option dragged in
weights we'd have to ship. `mlx-embeddings` pulls mlx-vlm + opencv + uvicorn,
and sentence-transformers pulls its own torch stack. This file is ~120 lines,
adds no dependency the app doesn't already have, and runs on the GPU alongside
the LLM. Validated against transformers/torch in tools/check_embed.py.

bge-small-en-v1.5: 33M params, 384 dims, MIT licensed. CLS pooling and L2 norm,
which is what the model was trained for — mean pooling degrades retrieval.
"""
from __future__ import annotations

import json
from typing import Optional

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from huggingface_hub import snapshot_download
from transformers import AutoTokenizer

import config


class _SelfAttention(nn.Module):
    def __init__(self, dim: int, heads: int) -> None:
        super().__init__()
        self.heads = heads
        self.scale = (dim // heads) ** -0.5
        self.query, self.key, self.value = (nn.Linear(dim, dim) for _ in range(3))
        self.out = nn.Linear(dim, dim)
        self.norm = nn.LayerNorm(dim, eps=1e-12)

    def __call__(self, x: mx.array, mask: mx.array) -> mx.array:
        B, L, D = x.shape
        def split(p):
            return p.reshape(B, L, self.heads, -1).transpose(0, 2, 1, 3)
        q, k, v = split(self.query(x)), split(self.key(x)), split(self.value(x))
        scores = (q @ k.transpose(0, 1, 3, 2)) * self.scale + mask
        ctx = (mx.softmax(scores, axis=-1) @ v).transpose(0, 2, 1, 3).reshape(B, L, D)
        return self.norm(x + self.out(ctx))


class _Layer(nn.Module):
    def __init__(self, dim: int, heads: int, ff: int) -> None:
        super().__init__()
        self.attn = _SelfAttention(dim, heads)
        self.up = nn.Linear(dim, ff)
        self.down = nn.Linear(ff, dim)
        self.norm = nn.LayerNorm(dim, eps=1e-12)

    def __call__(self, x: mx.array, mask: mx.array) -> mx.array:
        x = self.attn(x, mask)
        return self.norm(x + self.down(nn.gelu(self.up(x))))


class Embedder:
    def __init__(self, model_id: str = config.EMBED_MODEL) -> None:
        path = snapshot_download(model_id, allow_patterns=[
            "*.json", "*.txt", "model.safetensors"])
        cfg = json.load(open(f"{path}/config.json"))
        self.dim = cfg["hidden_size"]
        self._tok = AutoTokenizer.from_pretrained(path)
        self._max = min(cfg["max_position_embeddings"], config.EMBED_MAX_TOKENS)

        w = mx.load(f"{path}/model.safetensors")
        w = {k[len("bert."):] if k.startswith("bert.") else k: v for k, v in w.items()}

        self._word = w["embeddings.word_embeddings.weight"]
        self._pos = w["embeddings.position_embeddings.weight"]
        self._type = w["embeddings.token_type_embeddings.weight"]
        self._emb_norm = nn.LayerNorm(self.dim, eps=1e-12)
        self._emb_norm.weight = w["embeddings.LayerNorm.weight"]
        self._emb_norm.bias = w["embeddings.LayerNorm.bias"]

        heads = cfg["num_attention_heads"]
        self._layers = []
        for i in range(cfg["num_hidden_layers"]):
            p = f"encoder.layer.{i}."
            layer = _Layer(self.dim, heads, cfg["intermediate_size"])
            a = layer.attn
            for name, src in (("query", "attention.self.query"),
                              ("key", "attention.self.key"),
                              ("value", "attention.self.value")):
                lin = getattr(a, name)
                lin.weight, lin.bias = w[p + src + ".weight"], w[p + src + ".bias"]
            a.out.weight = w[p + "attention.output.dense.weight"]
            a.out.bias = w[p + "attention.output.dense.bias"]
            a.norm.weight = w[p + "attention.output.LayerNorm.weight"]
            a.norm.bias = w[p + "attention.output.LayerNorm.bias"]
            layer.up.weight = w[p + "intermediate.dense.weight"]
            layer.up.bias = w[p + "intermediate.dense.bias"]
            layer.down.weight = w[p + "output.dense.weight"]
            layer.down.bias = w[p + "output.dense.bias"]
            layer.norm.weight = w[p + "output.LayerNorm.weight"]
            layer.norm.bias = w[p + "output.LayerNorm.bias"]
            self._layers.append(layer)

        # Materialise every weight now, on this thread. `mx.load` returns lazy
        # arrays whose first evaluation needs the CPU stream — and that stream
        # doesn't exist on the asyncio.to_thread workers we encode from, so
        # leaving them lazy fails the first time a background task embeds.
        mx.eval(self._word, self._pos, self._type,
                self._emb_norm.parameters(),
                [l.parameters() for l in self._layers])

    def encode(self, texts: list[str]) -> np.ndarray:
        """→ (n, dim) float32, L2-normalised, so cosine similarity is a dot."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        enc = self._tok(texts, padding=True, truncation=True,
                        max_length=self._max, return_tensors="np")
        # Pinned to an explicit stream: this runs from asyncio.to_thread workers
        # alongside the LLM and TTS, and MLX streams are per-thread.
        with mx.stream(mx.default_device()):
            ids = mx.array(enc["input_ids"].astype(np.int32))
            attn = mx.array(enc["attention_mask"].astype(np.float32))
            B, L = ids.shape

            x = (self._word[ids] + self._pos[mx.arange(L)]
                 + self._type[mx.zeros((B, L), mx.int32)])
            x = self._emb_norm(x)
            # Additive mask: padding gets -inf so softmax ignores it entirely.
            mask = (1.0 - attn)[:, None, None, :] * -1e9
            for layer in self._layers:
                x = layer(x, mask)

            cls = x[:, 0]                              # bge is trained on CLS
            # Not mx.linalg.norm: that op is CPU-stream only, and the CPU stream
            # doesn't exist on the worker threads we encode from.
            cls = cls * mx.rsqrt((cls * cls).sum(axis=-1, keepdims=True) + 1e-12)
            mx.eval(cls)
        return np.array(cls, dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    def warmup(self) -> None:
        self.encode(["warm"])


def gated_top_k(query_vec: np.ndarray, ids: list[int], mat: Optional[np.ndarray],
                floor: float, k: int) -> list[int]:
    """Top-k row ids whose cosine clears `floor` — empty when nothing does.

    Returning nothing is the common, desirable case: injecting a weak "match"
    costs prefill on every turn and actively misleads the model. Shared by
    conversation recall and news retrieval so both gate identically.
    """
    if mat is None or not len(ids):
        return []
    sims = mat @ query_vec                      # both sides L2-normalised
    order = sims.argsort()[::-1][:k]
    return [ids[i] for i in order if float(sims[i]) >= floor]


_shared: Optional[Embedder] = None


def shared() -> Optional[Embedder]:
    """Lazily loaded and optional: retrieval degrades to FTS5 if this fails,
    rather than taking the whole backend down with it."""
    global _shared
    if _shared is None:
        try:
            _shared = Embedder()
        except Exception as exc:
            print(f"[embed] unavailable, falling back to keyword search: {exc}",
                  flush=True)
            return None
    return _shared
