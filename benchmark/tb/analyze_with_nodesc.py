#!/usr/bin/env python3
import glob
import json
import math
from collections import Counter
from pathlib import Path

TB = Path(__file__).resolve().parent
FROZEN = TB.parent / "results"
LEAK = {"agg_01", "simple_01", "group_02", "nested_01", "temporal_01", "temporal_02", "rel_01"}
UNSTABLE = {"win_05"}
CELLS = [("clear + layer (frozen)", "frozen_coder14b"), ("clear, schema only", "tb_clear_bare"),
         ("opaque + layer", "tb_opaque_layer"), ("opaque + rules/examples only", "tb_opaque_nodesc"), ("opaque, schema only", "tb_opaque_bare")]


def load(pattern):
    f = sorted(glob.glob(pattern))
    assert len(f) == 1, (pattern, f)
    recs = [json.loads(l) for l in open(f[0])]
    return {r["id"]: r for r in recs if "_meta" not in r}


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (c - h), 100 * (c + h)


def graded(run):
    return {i: r["verdict"] == "correct" for i, r in run.items() if r["verdict"] != "manual"}


out = ["# Opaque-schema experiment — 2x2 (qwen2.5-coder:14b, prompt-v2.2-frozen code)\n",
       "Exploratory, one run per cell. 'clear + layer' = the frozen Study B run (read only).\n"]
for s in ("broad", "mid"):
    runs = {}
    for name, lab in CELLS:
        base = FROZEN if lab.startswith("frozen") else TB / "results"
        runs[name] = load(str(base / f"*_{lab}_{s}.jsonl"))
    out.append(f"\n## {s} set\n")
    out.append("| cell | correct | 95% CI | excl. 7 leaked | invalid SQL | api errors |")
    out.append("|---|---|---|---|---|---|")
    for name in runs:
        g = graded(runs[name])
        k, n = sum(g.values()), len(g)
        lo, hi = wilson(k, n)
        gl = {i: v for i, v in g.items() if i not in LEAK}
        v = Counter(r["verdict"] for r in runs[name].values())
        out.append(f"| {name} | {k}/{n} ({100*k/n:.1f}%) | {lo:.1f}–{hi:.1f}% | "
                   f"{sum(gl.values())}/{len(gl)} ({100*sum(gl.values())/len(gl):.1f}%) | "
                   f"{v.get('invalid_sql', 0)} | {v.get('api_error', 0)} |")
    ref = graded(runs["clear + layer (frozen)"])
    out.append("\nPer-question changes vs the frozen run (+ gained, − lost):\n")
    for name in list(runs)[1:]:
        g = graded(runs[name])
        gained = sorted(i for i in g if g[i] and not ref.get(i))
        lost = sorted(i for i in g if ref.get(i) and not g[i])
        mark = lambda ids: ", ".join(i + ("*" if i in UNSTABLE else "") for i in ids) or "—"
        out.append(f"- **{name}**: +{len(gained)} [{mark(gained)}]  −{len(lost)} [{mark(lost)}]")
    cats = sorted({r["category"] for r in runs["clear + layer (frozen)"].values()})
    out.append("\n| category | " + " | ".join(runs) + " |")
    out.append("|---|" + "---|" * len(runs))
    for c in cats:
        row = []
        for name in runs:
            g = graded({i: r for i, r in runs[name].items() if r["category"] == c})
            row.append(f"{sum(g.values())}/{len(g)}" if g else "–")
        out.append(f"| {c} | " + " | ".join(row) + " |")
out.append("\n`*` = win_05, numerically unstable on the view (±0.01), treat a flip as noise.")
(TB / "results" / "analysis_with_nodesc.md").write_text("\n".join(out) + "\n", encoding="utf-8")
print("\n".join(out))
