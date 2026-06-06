#!/usr/bin/env python3
"""Check that every model in config.MODELS is safe to offer in the picker.

The failure this exists to catch is silent: several well-known models accept
`tools=` on their chat template and simply ignore it, so all thirteen of
Fennel's tools vanish from the prompt with no error anywhere. Hermes-3-8B,
Hermes-2-Pro and Llama-3-Groq-Tool-Use all do it, despite their names.

Run before adding a model to the registry:

    uv run --project backend python scripts/vet-models.py

Only config and tokenizer files are downloaded — a few MB per model, never the
weights.
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

SMALL = ["*.json", "*.jinja", "*.txt", "tokenizer.model"]


def main() -> int:
    bad = 0
    for m in config.MODELS:
        repo = m["id"]
        try:
            path = snapshot_download(repo, allow_patterns=SMALL)
            tok = load_tok(pathlib.Path(path))
            text = tok.apply_chat_template(
                [{"role": "system", "content": config.LLM_SYSTEM},
                 {"role": "user", "content": "hi"}],
                add_generation_prompt=True, tokenize=False, tools=TOOLS,
                enable_thinking=False)
        except Exception as exc:
            print(f"FAIL {m['name']:11} {repo}: {type(exc).__name__}: {exc}")
            bad += 1
            continue

        n_tools = sum(t["function"]["name"] in text for t in TOOLS)
        persona = "Your name is Fennel" in text
        # An open <think> means the model reasons before answering, which the
        # voice loop cannot afford; a pre-closed pair is the suppression working.
        open_think = bool(re.search(r"<think>(?!\s*</think>)", text))
        eos_ok = tok.convert_tokens_to_ids(tok.eos_token) in tok.eos_token_ids

        problems = []
        if (n_tools == len(TOOLS)) != m["tools"]:
            problems.append(f"registry says tools={m['tools']}, renders {n_tools}/{len(TOOLS)}")
        if not persona:
            problems.append("system prompt dropped")
        if open_think:
            problems.append("leaves <think> open")
        if not eos_ok:
            problems.append(f"{tok.eos_token!r} missing from stop ids "
                            "(handled at runtime, but worth knowing)")

        mark = "ok  " if not problems else "WARN"
        print(f"{mark} {m['name']:11} tools {n_tools:2}/{len(TOOLS)}  {repo}")
        for p in problems:
            print(f"       - {p}")
        bad += bool(problems and problems[0].startswith("registry"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
