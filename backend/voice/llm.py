"""LLM stage: mlx-lm streaming with explicit prefix-cache reuse (reference D4).

One KV cache holds the whole conversation. Each turn we diff the new full
prompt against what's already cached and prefill only the delta. Anything that
varies per turn (recalled memory, tool results — Stage 3) must therefore sit
LAST in the prompt, or it invalidates the cached prefix behind it and silently
doubles time-to-first-token.
"""
from __future__ import annotations

import asyncio
import threading
from typing import AsyncIterator, Iterator, Optional

import mlx.core as mx
from mlx_lm import load, stream_generate
from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache

import config
from voice.tools import TOOLS

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
        self._prime_len = 0
        # One model, one KV cache, several worker threads (generation, background
        # summarising, re-priming when the daily briefing lands). Running two of
        # those at once crashes MLX natively — no Python traceback, the process
        # just dies — so every entry point that touches the model takes this.
        self._lock = threading.RLock()

    def reset(self) -> None:
        """New conversation: drop everything the conversation added, but keep
        the primed system prefix — re-prefilling it costs seconds (see prime)."""
        if self._prime_len and len(self._cached_ids) >= self._prime_len:
            trim_prompt_cache(self._cache, len(self._cached_ids) - self._prime_len)
            self._cached_ids = self._cached_ids[:self._prime_len]
        else:
            self._cache = make_prompt_cache(self.model)
            self._cached_ids = []

    def prime(self, system: str) -> None:
        """Prefill the stable prefix — persona, tool schemas, day table — once at
        startup and pin it under every later `reset`.

        Worth the trouble because the tool schemas alone are ~640 tokens and
        this machine prefills at only ~200 tok/s: without priming, the first
        turn of every session pays ~4.7 s before the model says anything.
        """
        with self._lock:
            self._prime_len = 0
            self.reset()
        # Prime only the span that a real prompt genuinely begins with, so the
        # cached tokens are a true prefix and `_common_prefix` reuses all of it.
        probe = self._prompt_ids([{"role": "system", "content": system},
                                  {"role": "user", "content": "hi"}])
        sys_only = list(self.tokenizer.apply_chat_template(
            [{"role": "system", "content": system}],
            add_generation_prompt=False, tokenize=True, tools=TOOLS))
        n = _common_prefix(sys_only, probe)

        with self._lock:
            ids = mx.array(probe[:n])
            step = 512  # chunked like mlx-lm's own prefill: full-length logits
            while ids.size > step:      # for 900 positions would spike ~0.5 GB
                self.model(ids[:step][None], cache=self._cache)
                mx.eval([c.state for c in self._cache])
                ids = ids[step:]
            self.model(ids[None], cache=self._cache)
            mx.eval([c.state for c in self._cache])

            self._cached_ids = probe[:n]
            self._prime_len = n
        print(f"[llm] primed {n} prefix tokens", flush=True)

    def complete(self, messages: list[Message], max_tokens: int = 160) -> str:
        """One-shot generation on a throwaway cache.

        Used for background work like summarising (D8). It must NOT touch
        `self._cache`: sharing it would evict the live conversation's prefix and
        make the user's next turn pay a full re-prefill.
        """
        prompt = self._prompt_ids(messages)
        out: list[str] = []
        with self._lock:
            cache = make_prompt_cache(self.model)
            for resp in stream_generate(self.model, self.tokenizer, prompt=prompt,
                                        max_tokens=max_tokens, prompt_cache=cache):
                if resp.text:
                    out.append(resp.text)
        return "".join(out).strip()

    def warmup(self) -> None:
        """Compile Metal kernels with a throwaway generation so turn 1 is fast."""
        for _ in self.stream_reply([{"role": "user", "content": "Hi"}]):
            pass
        self.reset()

    def _prompt_ids(self, messages: list[Message]) -> list[int]:
        # `tools=` renders the signatures into the system block — stable prefix,
        # so tool-calling costs one prefill per session, not one per turn (D4).
        return list(
            self.tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True, tools=TOOLS
            )
        )

    def stream_reply(self, messages: list[Message]) -> Iterator[str]:
        """Blocking generator of text chunks; reuses the KV prefix (D4)."""
        prompt_ids = self._prompt_ids(messages)
        # The lock is held for the whole generation — acquired on the first
        # next(), released when the generator finishes or is closed by barge-in —
        # so a re-prime or a background summary cannot land mid-flight.
        with self._lock:
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
            completed = False
            try:
                for resp in stream_generate(
                    self.model, self.tokenizer, prompt=delta,
                    max_tokens=config.LLM_MAX_TOKENS, prompt_cache=self._cache,
                ):
                    generated.append(resp.token)
                    if resp.text:
                        yield resp.text
                completed = True
            finally:
                if completed:
                    # The cache now physically holds prompt_ids + generated.
                    self._cached_ids = prompt_ids + generated
                else:
                    # Interrupted (generator closed by barge-in): the cache holds
                    # tokens the user never fully heard — drop it so next turn does
                    # a clean re-prefill (D3/D4).
                    self.reset()

    async def astream(self, messages: list[Message],
                      stop: Optional["threading.Event"] = None) -> AsyncIterator[str]:
        """Async wrapper: run the blocking generator off the event loop and
        deliver chunks as they arrive (never block audio delivery — CLAUDE.md).
        If `stop` is set (barge-in) we stop pulling and close the generator, which
        halts MLX generation and triggers the cache reset above."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker() -> None:
            gen = self.stream_reply(messages)
            try:
                for chunk in gen:
                    if stop is not None and stop.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as exc:  # surface worker errors to the awaiter
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                gen.close()  # -> stream_reply.finally on interruption
                loop.call_soon_threadsafe(queue.put_nowait, None)

        fut = loop.run_in_executor(None, worker)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            await fut
