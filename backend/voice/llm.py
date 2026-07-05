"""Streaming generation with explicit prefix-cache reuse.

One KV cache holds the whole conversation. Each turn we diff the new full
prompt against what's already cached and prefill only the delta. Anything that
varies per turn — recalled memory, tool results — must sit LAST in the prompt,
or it invalidates the cached prefix behind it and doubles time-to-first-token.
"""
from __future__ import annotations

import asyncio
import gc
import glob
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator, Iterator, Optional

import mlx.core as mx
from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler
from mlx_lm.models.cache import (load_prompt_cache, make_prompt_cache,
                                  save_prompt_cache, trim_prompt_cache)

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
    def __init__(self, model_id: Optional[str] = None) -> None:
        # Resolved here rather than as a default argument, which would bind once at
        # class definition and ignore the model the picker chose.
        model_id = model_id or config.LLM_MODEL
        # The one thread MLX is allowed to see. mlx-lm generates inside a
        # module-level generation_stream and MLX registers streams per thread, so
        # arrays made on one thread can't be evaluated on another. Every entry
        # point here arrives on asyncio.to_thread, which hands out a different pool
        # thread each time — the lock below serialises access but not identity.
        # The weights load here too, so nothing MLX owns is born off-thread.
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
        self._mlx_thread = self._exec.submit(threading.current_thread).result()

        def _load() -> tuple:
            # Cap MLX's buffer pool before anything allocates, if the config asks for
            # it. Unbounded is MLX's default; the cap is for machines where the pool
            # competing with the weights means swapping.
            if config.MLX_CACHE_LIMIT_BYTES is not None:
                mx.set_cache_limit(config.MLX_CACHE_LIMIT_BYTES)
            return load(model_id)

        self.model_id = model_id
        self.model, self.tokenizer = self._exec.submit(_load).result()
        self._ensure_turn_end_stops()
        self._cache = self._exec.submit(make_prompt_cache, self.model).result()
        self._cached_ids: list[int] = []
        self._prime_len = 0
        self._prime_key = ""
        # Which tools to advertise. Optional ones follow the user's settings, and
        # changing them changes the primed prefix.
        self.tools = list(TOOLS)
        self._sampler = make_sampler(temp=config.LLM_TEMP, top_p=config.LLM_TOP_P)
        # Guards the KV cache against a caller queueing work from two places at
        # once. Two concurrent generations kill the process natively, with no
        # Python traceback.
        self._lock = threading.RLock()

    def _ensure_turn_end_stops(self) -> None:
        """Make sure the token the chat template ends a turn with stops generation.

        mlx-lm takes its stop ids from config.json and generation_config.json,
        which don't always agree with the template beside them — a config naming
        <|endoftext|> while every turn closes with <|im_end|> means nothing ever
        matches and generation runs to max_tokens. Adding the tokenizer's own
        declared eos_token is a no-op when the two already agree.
        """
        declared = getattr(self.tokenizer, "eos_token", None)
        if not declared:
            return
        try:
            token_id = self.tokenizer.convert_tokens_to_ids(declared)
            if token_id is None or token_id in self.tokenizer.eos_token_ids:
                return
            self.tokenizer.add_eos_token(declared)
        except Exception as exc:
            print(f"[llm] couldn't register {declared!r} as a stop token: {exc}",
                  flush=True)
            return
        print(f"[llm] added {declared!r} ({token_id}) to the stop tokens — the "
              "model config omitted it", flush=True)

    def unload(self) -> None:
        """Give the weights back before another model is loaded.

        Two models don't fit at once, so a swap frees the first outright: drop
        the KV cache and the weights, collect, empty MLX's buffer pool. Runs on
        the thread that allocated it, and retires the executor afterwards — this
        instance is finished.
        """
        def _free() -> None:
            self._cache = None
            self._cached_ids = []
            self.model = None
            self.tokenizer = None
            gc.collect()
            mx.clear_cache()
        try:
            self._on_mlx(_free)
        except Exception as exc:
            print(f"[llm] unload: {exc}", flush=True)
        finally:
            self._exec.shutdown(wait=True)
        print("[llm] unloaded", flush=True)

    @property
    def available(self) -> bool:
        """False once `unload` has run. The object outlives its weights, so
        anything that would generate has to check first."""
        return self.model is not None

    def _on_mlx(self, fn, *args, **kwargs):
        """Run `fn` on the one thread MLX is allowed to see, and wait for it."""
        if threading.current_thread() is self._mlx_thread:
            return fn(*args, **kwargs)
        return self._exec.submit(fn, *args, **kwargs).result()

    def reset(self) -> None:
        """New conversation: drop what the conversation added, keep the primed
        system prefix."""
        if self._prime_len and len(self._cached_ids) >= self._prime_len:
            trim_prompt_cache(self._cache, len(self._cached_ids) - self._prime_len)
            self._cached_ids = self._cached_ids[:self._prime_len]
        else:
            self._cache = make_prompt_cache(self.model)
            self._cached_ids = []

    def _prime_cache_path(self, token_count: int) -> str:
        """Where the primed KV state for this exact prefix lives on disk.

        Keyed by model + the precise prefix, so a changed briefing, a toggled
        tool or a new day simply misses and recomputes rather than restoring
        something subtly wrong.
        """
        from voice.store import APP_DIR
        key = hashlib.sha256(
            f"{self.model_id}|{token_count}|{self._prime_key}".encode()).hexdigest()[:16]
        # The model is in the filename so a sweep can tell one model's cache from
        # another's, and switching models doesn't throw away the one you left.
        return os.path.join(APP_DIR, "primecache",
                            f"{self._model_slug()}-{key}.safetensors")

    def _model_slug(self) -> str:
        return "".join(c if c.isalnum() else "_" for c in self.model_id)[-48:]

    def _stable_prefix_len(self, system: str, probe: list[int]) -> int:
        """How many leading tokens of a real prompt the user cannot influence.

        Rendering the system message alone is the direct way to ask, but some
        templates refuse it — Qwen3.5 raises "No user query found in messages".
        The fallback renders the same system block against two different user
        messages and takes the point where they diverge. Either answer is a true
        prefix of a real prompt, so a wrong guess costs reuse, not correctness.
        """
        try:
            sys_only = list(self.tokenizer.apply_chat_template(
                [{"role": "system", "content": system}],
                add_generation_prompt=False, tokenize=True, tools=self.tools))
            return _common_prefix(sys_only, probe)
        except Exception as exc:
            print(f"[llm] system-only render refused ({exc}); measuring the "
                  "stable prefix by divergence instead", flush=True)
            other = self._prompt_ids([{"role": "system", "content": system},
                                      {"role": "user", "content": "zzz"}])
            return _common_prefix(probe, other)

    def prime(self, system: str) -> None:
        """Prefill the stable prefix once at startup and pin it under `reset`.

        Persona, tool schemas and the day table come to a few thousand tokens.
        Without this the first turn of every session waits several seconds
        before the model says anything.
        """
        if threading.current_thread() is not self._mlx_thread:
            return self._on_mlx(self.prime, system)
        with self._lock:
            self._prime_len = 0
            self.reset()
        # Prime only the span a real prompt begins with, so the cached tokens are a
        # true prefix and _common_prefix reuses all of them.
        probe = self._prompt_ids([{"role": "system", "content": system},
                                  {"role": "user", "content": "hi"}])
        n = self._stable_prefix_len(system, probe)

        self._prime_key = system
        path = self._prime_cache_path(n)

        # Restoring takes about 0.03 s against ~15 s to recompute, and the result is
        # byte-identical on every launch.
        with self._lock:
            if os.path.exists(path):
                try:
                    restored, meta = load_prompt_cache(path, return_metadata=True)
                    if int(meta.get("n", -1)) == n:
                        mx.eval([c.state for c in restored])
                        self._cache = restored
                        self._cached_ids = probe[:n]
                        self._prime_len = n
                        mx.clear_cache()
                        print(f"[llm] restored {n} primed tokens from cache",
                              flush=True)
                        return
                except Exception as exc:
                    print(f"[llm] prime cache unusable, recomputing: {exc}",
                          flush=True)

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
            self._save_prime_cache(path, n)
            mx.clear_cache()      # a one-off spike; don't hold it for the session
        print(f"[llm] primed {n} prefix tokens", flush=True)

    #: How many models keep a primed prefix on disk. A few hundred MB each, so
    #: enough to switch between a couple of favourites without re-priming.
    KEEP_PRIME_CACHES = 4

    def _save_prime_cache(self, path: str, n: int) -> None:
        """Persist the primed state.

        This model's older prefixes are dropped — the prefix changes daily with
        the briefing. Other models' caches are kept, up to KEEP_PRIME_CACHES and
        least recently used first, so switching back to one is cheap.
        """
        try:
            folder = os.path.dirname(path)
            os.makedirs(folder, exist_ok=True)
            mine = os.path.join(folder, f"{self._model_slug()}-*.safetensors")
            for stale in glob.glob(mine):
                if stale != path:
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            save_prompt_cache(path, self._cache, metadata={"n": str(n)})

            others = [f for f in glob.glob(os.path.join(folder, "*.safetensors"))
                      if f != path]
            others.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            for old_file in others[self.KEEP_PRIME_CACHES - 1:]:
                try:
                    os.remove(old_file)
                except OSError:
                    pass
        except Exception as exc:
            print(f"[llm] couldn't save prime cache: {exc}", flush=True)

    def complete(self, messages: list[Message], max_tokens: int = 160) -> str:
        """One-shot generation on a throwaway cache.

        For background work such as summarising. It must not touch self._cache:
        sharing it evicts the live conversation's prefix.
        """
        if threading.current_thread() is not self._mlx_thread:
            return self._on_mlx(self.complete, messages, max_tokens)
        prompt = self._prompt_ids(messages)
        out: list[str] = []
        with self._lock:
            cache = make_prompt_cache(self.model)
            for resp in stream_generate(self.model, self.tokenizer, prompt=prompt,
                                        max_tokens=max_tokens, prompt_cache=cache,
                                        sampler=self._sampler):
                if resp.text:
                    out.append(resp.text)
        return "".join(out).strip()

    def warmup(self) -> None:
        """Compile Metal kernels with a throwaway generation so turn 1 is fast."""
        if threading.current_thread() is not self._mlx_thread:
            return self._on_mlx(self.warmup)
        for _ in self.stream_reply([{"role": "user", "content": "Hi"}]):
            pass
        self.reset()

    def _prompt_ids(self, messages: list[Message],
                    generation: bool = True) -> list[int]:
        # `tools=` renders the signatures into the system block, which is part
        # of the stable prefix — so tool-calling costs one prefill per session.
        return list(
            self.tokenizer.apply_chat_template(
                messages, add_generation_prompt=generation, tokenize=True,
                tools=self.tools,
                enable_thinking=False
            )
        )

    def warm(self, messages: list[Message]) -> None:
        """Prefill a conversation without generating, so the next turn starts hot.

        Called after the verbatim window is trimmed, which changes the front of
        the prompt and costs a full re-prefill — a couple of seconds landing on
        one unlucky turn. Doing it in a lull moves that off the conversation.

        No generation prompt: the next turn appends a user message first, so an
        assistant header would not be a prefix of it.
        """
        if threading.current_thread() is not self._mlx_thread:
            return self._on_mlx(self.warm, messages)
        ids = self._prompt_ids(messages, generation=False)
        with self._lock:
            common = _common_prefix(ids, self._cached_ids)
            if common < len(self._cached_ids):
                trim_prompt_cache(self._cache, len(self._cached_ids) - common)
                self._cached_ids = self._cached_ids[:common]
            delta = ids[common:]
            if not delta:
                return
            arr = mx.array(delta)
            step = 512
            while arr.size > step:
                self.model(arr[:step][None], cache=self._cache)
                mx.eval([c.state for c in self._cache])
                arr = arr[step:]
            self.model(arr[None], cache=self._cache)
            mx.eval([c.state for c in self._cache])
            self._cached_ids = ids
            mx.clear_cache()
        print(f"[llm] warmed {len(delta)} tokens during idle", flush=True)

    def stream_reply(self, messages: list[Message],
                     temp: Optional[float] = None) -> Iterator[str]:
        """Blocking generator of text chunks, reusing the KV prefix.

        `temp` overrides the sampler for one turn; drafting runs colder.
        """
        sampler = (self._sampler if temp is None
                   else make_sampler(temp=temp, top_p=config.LLM_TOP_P))
        prompt_ids = self._prompt_ids(messages)
        # Held for the whole generation, so a re-prime or a background summary
        # can't land mid-flight.
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
                    sampler=sampler,
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
                    # Interrupted by barge-in: the cache holds tokens the user
                    # never heard, so drop it and re-prefill next turn.
                    self.reset()

    async def astream(self, messages: list[Message],
                      stop: Optional["threading.Event"] = None,
                      temp: Optional[float] = None) -> AsyncIterator[str]:
        """Async wrapper: run the blocking generator off the event loop and
        deliver chunks as they arrive (never block audio delivery — CLAUDE.md).
        If `stop` is set (barge-in) we stop pulling and close the generator, which
        halts MLX generation and triggers the cache reset above."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker() -> None:
            gen = self.stream_reply(messages, temp=temp)
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

        fut = loop.run_in_executor(self._exec, worker)
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
