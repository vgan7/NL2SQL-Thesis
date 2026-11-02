#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

import requests
import yaml
import psycopg2

HERE = Path(__file__).parent
RESULTS = HERE / "results"

_DEFAULT_API = os.getenv("API_URL", "http://localhost:8001")
API_URL = _DEFAULT_API
PG_DSN = os.getenv(
    "PG_DSN",
    "host=localhost port=5433 dbname=iot_staging user=postgres "
    f"password={os.getenv('DB_PASSWORD', 'postgres')}",
)


import datetime as _dt


def _norm_cell(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, _dt.datetime):
        return v.date().isoformat() if v.time() == _dt.time(0) else v.isoformat(sep=" ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 2)
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        s = str(v).strip()
        if s.endswith(" 00:00:00"):
            s = s[:-9]
        return s


def _norm_rows(rows):
    return [tuple(_norm_cell(c) for c in r) for r in rows]


def _match(p, g, mode):
    if mode == "ordered":
        return list(p) == list(g)
    return Counter(map(repr, p)) == Counter(map(repr, g))


def compare(pred_rows, gold_rows, mode):
    p, g = _norm_rows(pred_rows), _norm_rows(gold_rows)
    if not p and not g:
        return True
    if not p or not g:
        return False

    if mode == "scalar":
        gf, pf = [c for r in g for c in r], [c for r in p for c in r]
        if len(pf) == len(gf):
            return Counter(map(repr, gf)) == Counter(map(repr, pf))
        return Counter(map(repr, gf)) <= Counter(map(repr, pf))

    ncp, ncg = len(p[0]), len(g[0])
    if ncp == ncg:
        return _match(p, g, mode)
    if ncp > ncg:
        for idx in combinations(range(ncp), ncg):
            if _match([tuple(r[i] for i in idx) for r in p], g, mode):
                return True
    return False


def run_sql(conn, sql):
    cur = conn.cursor()
    try:
        cur.execute("SET statement_timeout = 20000;")
        cur.execute(sql)
        rows = cur.fetchall()
        conn.rollback()
        return rows, None
    except Exception as e:
        conn.rollback()
        return None, str(e).strip().splitlines()[0]
    finally:
        cur.close()


def ask_api(question, history=None, attempts=2):
    payload = {"question": question}
    if history:
        payload["history"] = history
    last = None
    for i in range(attempts):
        t0 = time.perf_counter()
        try:
            r = requests.post(f"{API_URL}/ask", json=payload, timeout=300)
        except requests.exceptions.RequestException as e:
            last = {"http": 0, "detail": f"{type(e).__name__}: {e}",
                    "wall": round(time.perf_counter() - t0, 3)}
            time.sleep(2)
            continue
        wall = round(time.perf_counter() - t0, 3)
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:200]
            if 500 <= r.status_code < 600 and i + 1 < attempts:
                last = {"http": r.status_code, "detail": detail, "wall": wall}
                time.sleep(2)
                continue
            return {"http": r.status_code, "detail": detail, "wall": wall}
        d = r.json()
        d["http"] = 200
        d["wall"] = wall
        return d
    return last


def evaluate(questions, conn):
    out = []
    for q in questions:
        qid, cat = q["id"], q["category"]
        check = q.get("result_check", "set")
        print(f"  {qid:16} {cat:16} ", end="", flush=True)
        rec = {"id": qid, "category": cat, "question": q["question"], "check": check}

        api = ask_api(q["question"], q.get("history"))
        rec["http"] = api["http"]
        rec["wall_s"] = api["wall"]
        rec["anchor_rewrites"] = api.get("anchor_rewrites", 0)

        if check == "manual":
            rec.update(pred_sql=api.get("sql"), retries=api.get("retries"),
                       timings=api.get("timings", {}), answer=api.get("answer", ""),
                       api_detail=api.get("detail", ""),
                       valid_sql=None, exec_accuracy=None, verdict="manual")
            print("manual" if api["http"] == 200 else f"manual (HTTP {api['http']})")
            out.append(rec)
            continue

        if api["http"] != 200:
            rec.update(pred_sql=None, valid_sql=False, exec_accuracy=False,
                       retries=None, api_detail=api.get("detail", ""),
                       timings={}, verdict="api_error")
            print("API", api["http"])
            out.append(rec)
            continue

        pred_sql = api.get("sql")
        rec["pred_sql"] = pred_sql
        rec["retries"] = api.get("retries")
        rec["timings"] = api.get("timings", {})
        rec["answer"] = api.get("answer", "")

        pred_rows, pred_err = run_sql(conn, pred_sql) if pred_sql else (None, "no sql")
        rec["valid_sql"] = pred_err is None
        rec["pred_error"] = pred_err

        gold_rows, gold_err = run_sql(conn, q["gold_sql"])
        if gold_err:
            rec.update(exec_accuracy=None, verdict="gold_broken", gold_error=gold_err)
            print(f"GOLD BROKEN: {gold_err}")
            out.append(rec)
            continue

        if pred_err is not None:
            rec.update(exec_accuracy=False, verdict="invalid_sql")
            print("invalid")
            out.append(rec)
            continue

        ok = compare(pred_rows, gold_rows, check)
        rec["exec_accuracy"] = ok
        rec["verdict"] = "correct" if ok else "wrong_result"
        rec["pred_rowcount"] = len(pred_rows)
        rec["gold_rowcount"] = len(gold_rows)
        print("OK" if ok else "WRONG")
        out.append(rec)
    return out


def regrade(records, questions, conn):
    gold = {q["id"]: q for q in questions}
    for rec in records:
        q = gold.get(rec["id"])
        if not q:
            continue
        check = q.get("result_check", "set")
        if check == "manual":
            if rec.get("verdict") != "manual":
                rec.update(valid_sql=None, exec_accuracy=None, verdict="manual")
                print(f"  {rec['id']:16} manual (was {rec.get('verdict')})")
            continue
        if rec.get("verdict") == "api_error":
            continue
        pred_sql = rec.get("pred_sql")
        pred_rows, pred_err = run_sql(conn, pred_sql) if pred_sql else (None, "no sql")
        rec["valid_sql"] = pred_err is None
        rec["pred_error"] = pred_err
        gold_rows, gold_err = run_sql(conn, q["gold_sql"])
        if gold_err:
            rec.update(exec_accuracy=None, verdict="gold_broken", gold_error=gold_err)
        elif pred_err is not None:
            rec.update(exec_accuracy=False, verdict="invalid_sql")
        else:
            ok = compare(pred_rows, gold_rows, check)
            rec.update(exec_accuracy=ok, verdict="correct" if ok else "wrong_result")
        print(f"  {rec['id']:16} {rec['verdict']}")
    return records


def summarize(records, label, meta):
    gradable = [r for r in records if r.get("exec_accuracy") is not None]
    n = len(gradable)
    acc = sum(1 for r in gradable if r["exec_accuracy"]) / n if n else 0.0
    valid = [r for r in records if r.get("valid_sql") is not None]
    valid_rate = sum(1 for r in valid if r["valid_sql"]) / len(valid) if valid else 0.0
    retried = [r for r in records if r.get("retries") is not None]
    retry_rate = sum(1 for r in retried if r["retries"]) / len(retried) if retried else 0.0
    anchored = [r for r in records if r.get("anchor_rewrites")]

    lat = {}
    for stage in ("sql_generation", "sql_execution", "answer_generation", "total"):
        xs = [r["timings"].get(stage) for r in records if r.get("timings", {}).get(stage) is not None]
        if xs:
            lat[stage] = {"mean": round(statistics.mean(xs), 2),
                          "p50": round(statistics.median(xs), 2),
                          "p95": round(sorted(xs)[max(0, int(len(xs) * 0.95) - 1)], 2)}

    by_cat = {}
    for r in gradable:
        c = r["category"]
        by_cat.setdefault(c, []).append(bool(r["exec_accuracy"]))
    cat_rows = sorted((c, sum(v), len(v)) for c, v in by_cat.items())

    md = [f"# Benchmark run: {label}", "",
          f"- date: {dt.datetime.now().isoformat(timespec='seconds')}",
          f"- sql_model: `{meta.get('sql_model', '?')}`  ·  answer_model: `{meta.get('answer_model', '?')}`",
          f"- questions: {meta.get('questions_file', '?')} ({meta.get('n_questions', '?')})  ·  prompt ~{meta.get('prompt_tokens_est', '?')} tok",
          f"- questions graded: **{n}**",
          f"- **execution accuracy: {acc:.1%}**",
          f"- valid-SQL rate: {valid_rate:.1%}",
          f"- retry rate: {retry_rate:.1%}",
          f"- questions where CURRENT_DATE/NOW() was rewritten to the data anchor: "
          f"{len(anchored)} of {len(records)}", ""]
    md.append("## Accuracy by category\n")
    md.append("| category | correct | total | accuracy |")
    md.append("|---|---|---|---|")
    for c, ok, tot in cat_rows:
        md.append(f"| {c} | {ok} | {tot} | {ok/tot:.0%} |")
    md.append("\n## Latency (seconds)\n")
    md.append("| stage | mean | p50 | p95 |")
    md.append("|---|---|---|---|")
    for stage, v in lat.items():
        md.append(f"| {stage} | {v['mean']} | {v['p50']} | {v['p95']} |")

    fails = [r for r in records if r.get("verdict") in ("wrong_result", "invalid_sql", "api_error", "gold_broken")]
    if fails:
        md.append("\n## Failures\n")
        for r in fails:
            md.append(f"### {r['id']} ({r['category']}) — {r['verdict']}")
            md.append(f"- Q: {r['question']}")
            md.append(f"- pred SQL: `{r.get('pred_sql')}`")
            if r.get("pred_error"):
                md.append(f"- error: {r['pred_error']}")
            if r.get("gold_error"):
                md.append(f"- GOLD error: {r['gold_error']}")
            md.append("")
    manual = [r for r in records if r.get("verdict") == "manual"]
    if manual:
        md.append("\n## Manual review (non-answerable)\n")
        for r in manual:
            md.append(f"- **{r['id']}**: {r['question']}")
            md.append(f"  - SQL: `{r.get('pred_sql')}`")
            md.append(f"  - answer: {r.get('answer', '')[:300]}")
    return "\n".join(md), {"label": label, "n": n, "exec_accuracy": acc,
                           "valid_rate": valid_rate, "retry_rate": retry_rate,
                           "anchor_rewrite_count": len(anchored),
                           "by_category": {c: ok / tot for c, ok, tot in cat_rows},
                           "latency": lat}


def write_outputs(base, records, label, meta):
    with open(f"{base}.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    cols = ["id", "category", "check", "verdict", "exec_accuracy", "valid_sql",
            "retries", "anchor_rewrites", "wall_s", "pred_sql", "pred_error"]
    with open(f"{base}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)

    md, summary = summarize(records, label, meta)
    summary["meta"] = meta
    open(f"{base}.md", "w", encoding="utf-8").write(md)
    json.dump(summary, open(f"{base}.summary.json", "w"), indent=2)

    print("\n" + "=" * 60)
    print(f"execution accuracy : {summary['exec_accuracy']:.1%}  ({summary['n']} graded)")
    print(f"valid-SQL rate     : {summary['valid_rate']:.1%}")
    print(f"retry rate         : {summary['retry_rate']:.1%}")
    print(f"\nwrote {base}.md / .csv / .jsonl / .summary.json")


def _load_jsonl(path):
    lines = [json.loads(l) for l in open(path, encoding="utf-8")]
    meta = lines[0].get("_meta", {}) if lines and "_meta" in lines[0] else {}
    records = [l for l in lines if "_meta" not in l]
    return meta, records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(HERE / "questions.yaml"))
    ap.add_argument("--label", help="name for this run (e.g. model id)")
    ap.add_argument("--api", default=_DEFAULT_API)
    ap.add_argument("--regrade", metavar="JSONL",
                    help="recompute verdicts from a previous run's stored pred_sql "
                         "(no API calls) — for iterating on gold SQL / grading")
    args = ap.parse_args()
    globals()["API_URL"] = args.api

    questions = yaml.safe_load(open(args.questions, encoding="utf-8"))
    conn = psycopg2.connect(PG_DSN)
    conn.set_session(readonly=True)

    if args.regrade:
        meta, records = _load_jsonl(args.regrade)
        label = args.label or meta.get("label", "regrade")
        print(f"Regrading {len(records)} records from {os.path.basename(args.regrade)} ...")
        records = regrade(records, questions, conn)
        conn.close()
        meta["regraded"] = dt.datetime.now().isoformat(timespec="seconds")
        base = str(args.regrade).rsplit(".jsonl", 1)[0]
        write_outputs(base, records, label, meta)
        return

    if not args.label:
        ap.error("--label is required for a normal run")

    meta = {"label": args.label, "api": API_URL,
            "started": dt.datetime.now().isoformat(timespec="seconds"),
            "questions_file": os.path.basename(args.questions), "n_questions": len(questions)}
    try:
        meta.update({k: v for k, v in requests.get(f"{API_URL}/health", timeout=30).json().items()
                     if k in ("sql_model", "answer_model", "static_dataset",
                              "prompt_tokens_est", "num_ctx", "data_latest_ts")})
    except Exception as e:
        print(f"warning: could not read /health for metadata: {e}")

    print(f"Running {len(questions)} questions against {API_URL} "
          f"(sql={meta.get('sql_model','?')} answer={meta.get('answer_model','?')}) ...")
    records = evaluate(questions, conn)
    conn.close()

    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    write_outputs(str(RESULTS / f"{stamp}_{args.label}"), records, args.label, meta)


if __name__ == "__main__":
    sys.exit(main())
