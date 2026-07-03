#!/usr/bin/env python3
"""Check every model in config.MODELS before it is offered in the picker.

Some models accept `tools=` on their chat template and render nothing, which
drops every tool with no error. Renders the prompt and counts what came out.

    uv run --project backend python scripts/vet-models.py

Downloads config and tokenizer files only, never weights.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

from huggingface_hub import snapshot_download          # noqa: E402
from mlx_lm.tokenizer_utils import load as load_tok    # noqa: E402

import config                                          # noqa: E402
from voice.tools import TOOLS                          # noqa: E402

CONFIG_ONLY = ["*.json", "*.jinja", "*.txt", "tokenizer.model"]


def main() -> int:
    failures = 0
    for m in config.MODELS:
        repo = m["id"]
        try:
            path = snapshot_download(repo, allow_patterns=CONFIG_ONLY)
            tok = load_tok(pathlib.Path(path))
            text = tok.apply_chat_template(
                [{"role": "system", "content": config.LLM_SYSTEM},
                 {"role": "user", "content": "hi"}],
                add_generation_prompt=True, tokenize=False, tools=TOOLS,
                enable_thinking=False)
        except Exception as exc:
            print(f"FAIL {m['name']:11} {repo}: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        rendered = sum(t["function"]["name"] in text for t in TOOLS)
        # A pre-closed <think></think> is the suppression working; an open one
        # means it reasons before answering.
        open_think = bool(re.search(r"<think>(?!\s*</think>)", text))
        eos_ok = tok.convert_tokens_to_ids(tok.eos_token) in tok.eos_token_ids

        mismatch = (rendered == len(TOOLS)) != m["tools"]
        warnings = []
        if mismatch:
            warnings.append(f"registry says tools={m['tools']}, "
                            f"renders {rendered}/{len(TOOLS)}")
        if "Your name is Fennel" not in text:
            warnings.append("system prompt dropped")
        if open_think:
            warnings.append("leaves <think> open")
        if not eos_ok:
            warnings.append(f"{tok.eos_token!r} missing from stop ids "
                            "(handled at runtime)")

        print(f"{'WARN' if warnings else 'ok  '} {m['name']:11} "
              f"tools {rendered:2}/{len(TOOLS)}  {repo}")
        for w in warnings:
            print(f"       - {w}")
        failures += mismatch
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
