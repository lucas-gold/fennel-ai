# Third-party components in Fennel

Fennel is licensed **GPL-3.0-or-later** (see `LICENSE`). 

## Models bundled or downloaded on first run

| Component | Role | Licence |
|---|---|---|
| [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) (4-bit, mlx-community) — "Everyday" | conversation | Apache-2.0 |
| [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) (4-bit, mlx-community) — "Balanced" | conversation | Apache-2.0 |
| [Qwen2.5-Coder-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-7B) (4-bit, mlx-community) — "Code" | conversation | Apache-2.0 |
| [Hermes-4-14B](https://huggingface.co/NousResearch/Hermes-4-14B) (4-bit, mlx-community) — "Agent" | conversation | Apache-2.0 |
| [Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B) (4-bit, mlx-community) — "Light" | conversation | Apache-2.0 |
| [Rocinante-X-12B-v1](https://huggingface.co/TheDrummer/Rocinante-X-12B-v1) (4-bit, ailexleon) — "Creative" | conversation | Apache-2.0, inherited: the fine-tune carries no tag of its own, and its base, [Mistral NeMo](https://huggingface.co/mistralai/Mistral-Nemo-Base-2407), is Apache-2.0 — which permits derivatives under the same terms. |
| [FLUX.2-klein-4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) (4-bit, mflux) | image generation | Apache-2.0 |
| [Whisper small.en](https://huggingface.co/openai/whisper-small.en) (mlx-community conversion) | speech recognition | Apache-2.0 |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (4-bit, mlx-community) | speech synthesis | Apache-2.0 |
| [Silero VAD v5](https://github.com/snakers4/silero-vad) | voice activity detection | MIT |
| [bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | memory + retrieval embeddings | MIT |

None of these are bundled. Each is downloaded from Hugging Face on the machine
that runs it, at the user's request and after the consent screen, so Fennel
distributes no model weights.

Every listed model is Apache-2.0 or MIT. Llama 3.2 filled the "Light" slot
until it became clear that Meta's community licence is not an open-source one —
it carries an acceptable-use policy, a clause covering services above 700
million monthly users, and an attribution requirement — so it was replaced with
Qwen3-1.7B rather than shipped with an asterisk.

**Models added by hand** — the picker will take any MLX repo from Hugging Face —
carry whatever licence their publisher set. Fennel neither checks nor records it,
and the obligation is the user's.

## Libraries

| Component | Licence |
|---|---|
| espeak-ng (via `espeakng-loader`) | GPL-3.0 |
| `phonemizer-fork` | GPL-3.0 |
| `misaki` (Kokoro G2P) | Apache-2.0 |
| MLX, `mlx-lm`, `mlx-whisper`, `mlx-audio` | MIT |
| `transformers`, `tokenizers`, `safetensors` | Apache-2.0 |
| `onnxruntime` | MIT |
| `numpy`, `scipy`, `soundfile`, `websockets`, `torch` | BSD-3-Clause |
| `mflux` (image generation) | MIT |
| `opencv-python`, `huggingface-hub`, `hf-transfer` | Apache-2.0 |
| `pillow` | MIT-CMU |
| `matplotlib` | PSF-based (matplotlib licence) |
| `sentencepiece` | Apache-2.0 |
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

**On RSS.** Feeds are read, not republished or redistributed. Headlines and the feeds' own summaries stay on the user's machine and are summarised locally by the on-device
model.

## Full licence texts

- GPL-3.0: `LICENSE` in this repository, and in the app under Fennel → Licences.
- Apache-2.0: `licenses/APACHE-2.0.txt`.
- MIT and BSD notices: `licenses/PERMISSIVE.txt`.
