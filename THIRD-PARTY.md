# Third-party components in Fennel

Fennel itself is licensed **GPL-3.0-or-later** (see `LICENSE`). That choice is
forced rather than chosen: the speech pipeline links **espeak-ng** and
**phonemizer-fork**, both GPL-3.0, so the combined work must be GPL-3.0 too.
Everything else here is permissive and could have gone either way.

Distributing a Fennel binary therefore means making this source available to
whoever receives it (GPL-3.0 §6).

## Models bundled or downloaded on first run

| Component | Role | Licence |
|---|---|---|
| [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) (4-bit, mlx-community) | conversation | Apache-2.0 |
| [Whisper small.en](https://huggingface.co/openai/whisper-small.en) (mlx-community conversion) | speech recognition | Apache-2.0 |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (4-bit, mlx-community) | speech synthesis | Apache-2.0 |
| [Silero VAD v5](https://github.com/snakers4/silero-vad) | voice activity detection | MIT |
| [bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | memory + retrieval embeddings | MIT |

Apache-2.0 requires that its licence text travel with the distribution and that
modifications be stated. The models are used **as published** — quantised to
4-bit by mlx-community where noted, not otherwise modified or fine-tuned.

Kokoro's voice packs are worth a second look before any wider release: the model
is Apache-2.0, but individual voices derive from varied training data. Fennel
ships `af_heart` only.

## Libraries

| Component | Licence |
|---|---|
| espeak-ng (via `espeakng-loader`) | **GPL-3.0** — the reason Fennel is GPL |
| `phonemizer-fork` | **GPL-3.0** |
| `misaki` (Kokoro G2P) | Apache-2.0 |
| MLX, `mlx-lm`, `mlx-whisper`, `mlx-audio` | MIT |
| `transformers`, `tokenizers`, `safetensors` | Apache-2.0 |
| `onnxruntime` | MIT |
| `numpy`, `scipy`, `soundfile`, `websockets`, `torch` | BSD-3-Clause |
| `numba`, `llvmlite` | BSD-2-Clause |
| `tqdm` | MPL-2.0 and MIT |

## Live data sources (opt-in, off by default)

| Source | Used for | Terms |
|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | daily forecast | CC BY 4.0 — **attribution required**, given in Settings and here |
| [Wikipedia](https://www.wikipedia.org/) | `search_wikipedia` lookups | CC BY-SA 4.0 — extracts are shown with the article name and a link |
| BBC World, NPR, Ars Technica RSS | daily headlines | Feed metadata only (headline, the feed's own summary, link). No article text is fetched or stored. |
| [Ollama web search](https://ollama.com/) | `search_web` lookups | Hosted API, used only with the user's own key. Results are shown with their titles and links. |

Fennel makes no network request unless the user turns one of these on, and the
Ollama key is supplied by the user and stored in their macOS Keychain — it is
never written to Fennel's database or bundled with the app.

**On RSS.** Feeds are read, not republished. Headlines and the feeds' own
summaries stay on the user's machine and are summarised locally by the on-device
model; no article text is fetched, stored or redistributed, which is what these
feeds are published for. Republishing feed content would be a different
arrangement and would need each publisher's terms revisited first — BBC's
restrict commercial reuse — but Fennel does not do that and has no plans to.

## Full licence texts

- GPL-3.0: `LICENSE` in this repository, and in the app under Fennel → Licences.
- Apache-2.0: `licenses/APACHE-2.0.txt`.
- MIT and BSD notices: `licenses/PERMISSIVE.txt`.
