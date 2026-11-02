#!/usr/bin/env python3
import os
import sys
from collections import Counter
from pathlib import Path

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from runner import _norm_cell

from make_variants import frozen, opaque_questions
from mapping import DECODE, VALUES, COLS

PW = os.getenv("DB_PASSWORD", "postgres")
DSN = "host=localhost port=5433 user=postgres password={} dbname={}"


def rows(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    out = [tuple(_norm_cell(DECODE.get(v, v) if isinstance(v, str) else v) for v in r)
           for r in cur.fetchall()]
    conn.rollback()
    return out


def is_float_noise(conn, sql, ra, rb, repeats=5):
    if len(ra) != len(rb):
        return False
    for x, y in zip(sorted(ra, key=str), sorted(rb, key=str)):
        for u, v in zip(x, y):
            if u == v:
                continue
            if not (isinstance(u, float) and isinstance(v, float) and abs(u - v) <= 0.0101):
                return False
    runs = {tuple(rows(conn, sql)) for _ in range(repeats)}
    return len(runs) > 1


def main():
    a = psycopg2.connect(DSN.format(PW, "iot_staging"))
    b = psycopg2.connect(DSN.format(PW, "iot_tb"))
    for conn in (a, b):
        conn.set_session(readonly=True)
    coded_cols = {COLS[c] for c in VALUES}
    bad = order_only = total = 0
    unstable = []
    for path in ("benchmark/questions.yaml", "benchmark/questions_mid.yaml"):
        orig = frozen(path)
        for qo, qt in zip(orig, opaque_questions(orig)):
            if not (qo.get("gold_sql") or "").strip():
                continue
            total += 1
            ra, rb = rows(a, qo["gold_sql"]), rows(b, qt["gold_sql"])
            if Counter(ra) != Counter(rb):
                if is_float_noise(b, qt["gold_sql"], ra, rb):
                    unstable.append(qo["id"])
                    print(f"float noise {qo['id']}: same rows, the 2nd decimal varies between "
                          f"repeated runs of the same query (summation order), max diff 0.01")
                    continue
                bad += 1
                print(f"MISMATCH {qo['id']}: {len(ra)} vs {len(rb)} rows\n  {qt['gold_sql']}")
            elif qo["result_check"] == "ordered" and ra != rb:
                sorts_by_code = any(c in qt["gold_sql"].split("ORDER BY")[-1] for c in coded_cols)
                order_only += 1
                tag = "expected (sorted by a coded column)" if sorts_by_code else "UNEXPECTED"
                print(f"order differs {qo['id']}: {tag}")
                bad += 0 if sorts_by_code else 1
    print(f"\n{total} gold queries checked: {total - bad - len(unstable)} identical result sets, "
          f"{order_only} order-only differences, {len(unstable)} float-noise {unstable}, "
          f"{bad} INVALID")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
