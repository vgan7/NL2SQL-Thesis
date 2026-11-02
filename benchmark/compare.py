#!/usr/bin/env python3
import glob
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
FILES = sys.argv[1:] or sorted(glob.glob(str(HERE / "results" / "*.summary.json")))

runs = [json.load(open(f)) for f in FILES]
if not runs:
    sys.exit("no summary.json files found — run runner.py first")


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


groups = defaultdict(dict)
for r in runs:
    qfile = r.get("meta", {}).get("questions_file", "questions.yaml")
    started = r.get("meta", {}).get("started", "")
    prev = groups[qfile].get(r["label"])
    if prev is None or started >= prev.get("meta", {}).get("started", ""):
        groups[qfile][r["label"]] = r

md, tex = ["# Model comparison\n"], []

for qfile in sorted(groups):
    g = list(groups[qfile].values())
    n = g[0]["n"]
    cats = sorted({c for r in g for c in r["by_category"]})

    md.append(f"## {qfile}  (n = {n} graded)\n")
    md.append("| model | exec. acc | 95% CI | valid-SQL | retry | SQL-gen s | total s |")
    md.append("|---|---|---|---|---|---|---|")
    for r in g:
        k = round(r["exec_accuracy"] * r["n"])
        lo, hi = wilson(k, r["n"])
        lat = r.get("latency", {})
        md.append(
            f"| {r['label']} | {k}/{r['n']} ({r['exec_accuracy']*100:.1f}%) | "
            f"{lo*100:.1f}-{hi*100:.1f}% | {r['valid_rate']*100:.1f}% | "
            f"{r['retry_rate']*100:.1f}% | {lat.get('sql_generation', {}).get('mean', '-')} | "
            f"{lat.get('total', {}).get('mean', '-')} |"
        )
    md.append("\n### Execution accuracy by category\n")
    md.append("| category | " + " | ".join(r["label"] for r in g) + " |")
    md.append("|---|" + "---|" * len(g))
    for c in cats:
        md.append(
            f"| {c} | "
            + " | ".join(
                (f"{r['by_category'][c]*100:.0f}%" if c in r["by_category"] else "-")
                for r in g
            )
            + " |"
        )
    md.append("")

    tag = qfile.replace("questions", "").replace(".yaml", "").strip("_-") or "main"
    tex += [
        r"\begin{table}[t]\centering",
        rf"\caption{{NL2SQL results, {qfile} ($n={n}$). CI: Wilson 95\%.}}"
        rf"\label{{tab:nl2sql-{tag}}}",
        r"\begin{tabular}{lrlrrr}", r"\toprule",
        r"model & acc. & 95\% CI & valid & retry & total (s) \\", r"\midrule",
    ]
    for r in g:
        k = round(r["exec_accuracy"] * r["n"])
        lo, hi = wilson(k, r["n"])
        lat = r.get("latency", {})
        lbl = str(r["label"]).replace("_", r"\_")
        tex.append(
            f"{lbl} & {k}/{r['n']} & {lo*100:.1f}--{hi*100:.1f}\\% & "
            f"{r['valid_rate']*100:.1f}\\% & {r['retry_rate']*100:.1f}\\% & "
            f"{lat.get('total', {}).get('mean', 0)} \\\\"
        )
    tex += [r"\bottomrule", r"\end{tabular}", r"\end{table}", ""]

(HERE / "results" / "comparison.md").write_text("\n".join(md), encoding="utf-8")
(HERE / "results" / "comparison.tex").write_text("\n".join(tex), encoding="utf-8")

print(f"merged {len(runs)} run(s) into {len(groups)} question set(s): "
      + ", ".join(f"{q} ({len(v)} model(s))" for q, v in sorted(groups.items())))
print("wrote results/comparison.md and results/comparison.tex")
