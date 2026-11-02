#!/usr/bin/env python3
import glob
import json
import math
from pathlib import Path

from translations import NOISY_CATEGORIES

HERE = Path(__file__).resolve().parent
FROZEN = HERE.parent / "results"


def load(pattern):
    f = sorted(glob.glob(pattern))
    assert len(f) == 1, (pattern, f)
    return {r["id"]: r for r in map(json.loads, open(f[0])) if "_meta" not in r}


def wilson(k, n, z=1.96):
    if n == 0:
        return 0, 0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (c - h), 100 * (c + h)


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, p)


out = ["# Greek vs English — qwen2.5-coder:14b, frozen code, same gold SQL\n",
       "Greek = frozen Study B run. English = translated questions + English domain variant "
       "(synonyms, rule trigger phrases, example questions). One run each (deterministic).\n",
       "Caveat: the code-side freshness note still contains the Greek words 'τώρα', 'σήμερα', 'χθες'.\n"]
for s in ("broad", "mid"):
    el = load(str(FROZEN / f"*_frozen_coder14b_{s}.jsonl"))
    en = load(str(HERE / "results" / f"*_lang_en_{s}.jsonl"))
    ids = [i for i in el if el[i]["verdict"] != "manual"]
    out.append(f"\n## {s} set\n")
    out.append("| subset | n | Greek | English | Greek-only correct | English-only correct | McNemar p |")
    out.append("|---|---|---|---|---|---|---|")
    groups = {"clean": [i for i in ids if el[i]["category"] not in NOISY_CATEGORIES],
              "noisy": [i for i in ids if el[i]["category"] in NOISY_CATEGORIES],
              "all": ids}
    detail = {}
    for g, sub in groups.items():
        if not sub:
            continue
        a = [el[i]["verdict"] == "correct" for i in sub]
        b = [en[i]["verdict"] == "correct" for i in sub]
        only_el = [i for i, x, y in zip(sub, a, b) if x and not y]
        only_en = [i for i, x, y in zip(sub, a, b) if y and not x]
        detail[g] = (only_el, only_en)
        n = len(sub)
        ka, kb = sum(a), sum(b)
        la, ha = wilson(ka, n)
        lb, hb = wilson(kb, n)
        out.append(f"| {g} | {n} | {ka}/{n} ({100*ka/n:.1f}%, {la:.0f}–{ha:.0f}) | "
                   f"{kb}/{n} ({100*kb/n:.1f}%, {lb:.0f}–{hb:.0f}) | {len(only_el)} | {len(only_en)} | "
                   f"{mcnemar_exact(len(only_el), len(only_en)):.3f} |")
    for g in ("clean", "noisy"):
        if g in detail:
            o_el, o_en = detail[g]
            out.append(f"\n- {g}: correct only in Greek: {', '.join(o_el) or '—'}; "
                       f"correct only in English: {', '.join(o_en) or '—'}")
(HERE / "results" / "analysis_lang.md").write_text("\n".join(out) + "\n", encoding="utf-8")
print("\n".join(out))
