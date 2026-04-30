"""Validate the MLX encoder against transformers/torch on the same weights.

Retrieval that is quietly wrong is worse than no retrieval, so this asserts the
vectors match a reference implementation rather than merely 'looking normalised'.
"""
import sys, numpy as np, torch
sys.path.insert(0, ".")
from transformers import AutoModel, AutoTokenizer
from voice.embed import Embedder
import config

TEXTS = ["what should I order for dinner tonight?",
         "I'm allergic to shellfish",
         "the weather in Paris is mild in October",
         "my dog Pip is a beagle"]

mine = Embedder().encode(TEXTS)

tok = AutoTokenizer.from_pretrained(config.EMBED_MODEL)
ref_model = AutoModel.from_pretrained(config.EMBED_MODEL).eval()
enc = tok(TEXTS, padding=True, truncation=True, max_length=256, return_tensors="pt")
with torch.no_grad():
    ref = ref_model(**enc).last_hidden_state[:, 0]
ref = torch.nn.functional.normalize(ref, dim=-1).numpy()

cos = (mine * ref).sum(1)
print("per-text cosine vs reference:", np.round(cos, 5))
print("max abs diff:", float(np.abs(mine - ref).max()))
assert cos.min() > 0.999, f"encoder disagrees with reference: {cos}"

sim = mine @ mine.T
print("\nsemantic check (dinner query vs each):")
for t, s in zip(TEXTS, sim[0]):
    print(f"  {s:+.3f}  {t}")
assert sim[0][1] > sim[0][3], "shellfish should beat the dog for a dinner query"
print("\nPASS")
