"""LLM stage: mlx-lm streaming with explicit prefix-cache reuse (reference D4).

One KV cache holds the whole conversation. Each turn we diff the new full
prompt against what's already cached and prefill only the delta. Anything that
varies per turn (recalled memory, tool results — Stage 3) must therefore sit
LAST in the prompt, or it invalidates the cached prefix behind it and silently
doubles time-to-first-token.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterator

from mlx_lm import load, stream_generate
from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache

import config

Message = dict[str, str]


def _common_prefix(a: list[int], b: list[int]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class LLM:
    def __init__(self, model_id: str = config.LLM_MODEL) -> None:
        self.model, self.tokenizer = load(model_id)
        self._cache = make_prompt_cache(self.model)
        self._cached_ids: list[int] = []

    def reset(self) -> None:
        """New conversation: drop the KV cache and prefix bookkeeping."""
        self._cache = make_prompt_cache(self.model)
        self._cached_ids = []

    def _prompt_ids(self, messages: list[Message]) -> list[int]:
        return list(
            self.tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True
            )
        )

    def stream_reply(self, messages: list[Message]) -> Iterator[str]:
        """Blocking generator of text chunks; reuses the KV prefix (D4)."""
        prompt_ids = self._prompt_ids(messages)
        common = _common_prefix(prompt_ids, self._cached_ids)

        # Divergence (rare in append-only chat, e.g. a re-tokenized boundary):
        # trim the stale tail so the cache holds exactly the shared prefix.
        if common < len(self._cached_ids):
            trim_prompt_cache(self._cache, len(self._cached_ids) - common)
            self._cached_ids = self._cached_ids[:common]

        delta = prompt_ids[common:]
        print(f"[llm] prefill delta={len(delta)} "
              f"(reused prefix {common}/{len(prompt_ids)})", flush=True)

        generated: list[int] = []
        for resp in stream_generate(
            self.model, self.tokenizer, prompt=delta,
            max_tokens=config.LLM_MAX_TOKENS, prompt_cache=self._cache,
        ):
            generated.append(resp.token)
            if resp.text:
                yield resp.text

        # The cache now physically holds prompt_ids + generated.
        self._cached_ids = prompt_ids + generated

    async def astream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Async wrapper: run the blocking generator off the event loop and
        deliver chunks as they arrive (never block audio delivery — CLAUDE.md)."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker() -> None:
            try:
                for chunk in self.stream_reply(messages):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as exc:  # surface worker errors to the awaiter
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        fut = loop.run_in_executor(None, worker)
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
        await fut
