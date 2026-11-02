import psycopg2
import requests
import os
import json
import logging
import time
import re
import threading
from decimal import Decimal
from datetime import datetime, date, timedelta
from collections import Counter
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from domain import load_domain, SQL_STOP_TOKENS

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "dbname": os.getenv("DB_NAME", "iot_staging"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "postgres"),
    "port": int(os.getenv("DB_PORT", "5432")),
}

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{OLLAMA_HOST}/api/generate"

OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))
_KA = os.getenv("OLLAMA_KEEP_ALIVE", "-1")
OLLAMA_KEEP_ALIVE = int(_KA) if _KA.lstrip("-").isdigit() else _KA
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_SEED = int(os.getenv("OLLAMA_SEED", "42"))
WARMUP_ON_STARTUP = os.getenv("WARMUP_ON_STARTUP", "true").lower() == "true"

DEFAULT_MODEL = os.getenv("MODEL", "qwen2.5-coder:14b")
SQL_MODEL = os.getenv("SQL_MODEL", DEFAULT_MODEL)
ANSWER_MODEL = os.getenv("ANSWER_MODEL", DEFAULT_MODEL)

MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "1000"))
SAMPLE_ROWS_FOR_LLM = int(os.getenv("SAMPLE_ROWS_FOR_LLM", "15"))
SAMPLE_ROWS_MAX = int(os.getenv("SAMPLE_ROWS_MAX", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
CHAT_HISTORY_TURNS = int(os.getenv("CHAT_HISTORY_TURNS", "3"))
DATA_NOW_TTL = int(os.getenv("DATA_NOW_TTL", "300"))
STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "15000"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [nl2sql-api] %(message)s",
)
logger = logging.getLogger("nl2sql-api")


_domain = None
_domain_lock = threading.Lock()


def get_domain(force=False):
    global _domain
    if _domain is not None and not force:
        return _domain
    with _domain_lock:
        if _domain is None or force:
            conn = psycopg2.connect(**DB_CONFIG)
            try:
                _domain = load_domain(conn)
            finally:
                conn.close()
    return _domain


class OllamaError(Exception):
    """Ollama call failed (timeout, connection, HTTP error)."""


class SqlUnrecoverable(Exception):

    def __init__(self, reason, last_sql="", kind="failed"):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.last_sql = last_sql


def call_ollama(prompt, model, timeout=None, num_predict=512, stop=None):
    options = {
        "temperature": 0.0,
        "seed": OLLAMA_SEED,
        "num_ctx": OLLAMA_NUM_CTX,
        "num_predict": num_predict,
        "use_mmap": True,
    }
    if stop:
        options["stop"] = stop
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": options,
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=timeout or OLLAMA_TIMEOUT)
        r.raise_for_status()
    except requests.exceptions.Timeout as e:
        raise OllamaError(
            f"Ollama timed out after {timeout or OLLAMA_TIMEOUT}s (model still loading?)"
        ) from e
    except requests.exceptions.RequestException as e:
        raise OllamaError(f"Ollama request failed: {e}") from e
    return r.json().get("response", "").strip()


_AGG_RE = re.compile(r"\b(COUNT|AVG|SUM|MIN|MAX)\s*\(|\bGROUP\s+BY\b", re.IGNORECASE)
_HAS_LIMIT_RE = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_HAS_ORDER_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_AUTO_LIMIT_RE = re.compile(r"\s+LIMIT\s+\d+\s*/\*\s*auto-limit\s*\*/\s*;?\s*$", re.IGNORECASE)
_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|MERGE|CALL|COPY)\b",
    re.IGNORECASE,
)


def clean_sql(raw_sql):
    sql = raw_sql.strip()
    sql = re.sub(r"^```sql\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"```\s*$", "", sql)
    sql = sql.strip()
    if ";" in sql:
        sql = sql.split(";", 1)[0]
    sql = sql.strip().rstrip(";") + ";"

    is_aggregate = bool(_AGG_RE.search(sql))
    if not is_aggregate and not _HAS_LIMIT_RE.search(sql):
        sql = sql.rstrip(";") + f" LIMIT {MAX_RESULT_ROWS} /* auto-limit */;"
    return sql


def strip_auto_limit_marker(sql):
    return sql.replace(" /* auto-limit */", "")


def is_safe_select(sql):
    normalized = sql.strip().lstrip("(").strip()
    head = normalized.upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        return False
    if ";" in normalized.rstrip(";"):
        return False
    return _FORBIDDEN_SQL.search(normalized) is None


def format_chat_history(history, question=""):
    turns = []
    for t in history or []:
        if not isinstance(t, dict):
            continue
        if t.get("role") not in (None, "user"):
            continue
        q = str(t.get("question") or t.get("content") or "").strip()
        if q:
            turns.append((q, t.get("sql")))
    if turns and turns[-1][0] == (question or "").strip():
        turns.pop()
    turns = turns[-CHAT_HISTORY_TURNS:]
    if not turns:
        return ""
    lines = ["CONVERSATION HISTORY (for follow-up context):"]
    for q, sql in turns:
        lines.append(f"  User asked: {q}")
        if sql:
            lines.append(f"  SQL used: {sql}")
    return "\n".join(lines)


_DATA_NOW = {"value": None, "at": 0.0}


def get_data_now():
    dom = get_domain()
    if not dom.time_anchor:
        return None
    now = time.time()
    if _DATA_NOW["value"] is not None and now - _DATA_NOW["at"] < DATA_NOW_TTL:
        return _DATA_NOW["value"]
    table, col = dom.time_anchor
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(f'SELECT MAX("{col}") FROM "{table}";')
        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            _DATA_NOW.update(value=row[0], at=now)
    except Exception as e:
        logger.warning("Could not read time anchor MAX(%s.%s): %s", table, col, e)
    finally:
        if conn is not None:
            conn.close()
    return _DATA_NOW["value"]


def freshness_note():
    dom = get_domain()
    if not dom.static:
        return ""
    data_now = get_data_now()
    if not data_now:
        return ""
    stamp = data_now.isoformat(sep=" ")
    day = data_now.date().isoformat()
    col = dom.time_anchor[1]
    return (
        f"DATA FRESHNESS: the dataset is historical and static. The most recent row "
        f"has {col} = '{stamp}'. Interpret 'τώρα' / 'αυτή την εβδομάδα' / "
        f"'τελευταίες X μέρες' relative to that timestamp "
        f"(e.g. >= TIMESTAMP '{stamp}' - INTERVAL '7 days'). "
        f"'σήμερα' is the calendar day of that row ({col} >= DATE '{day}' AND {col} < "
        f"DATE '{day}' + INTERVAL '1 day') and 'χθες' the calendar day before it "
        f"({col} >= DATE '{day}' - INTERVAL '1 day' AND {col} < DATE '{day}'). "
        f"Do NOT use CURRENT_DATE / NOW()."
    )


def make_json_safe(value):
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _is_number(v):
    if isinstance(v, bool):
        return False
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


def _numeric_col_indices(columns, rows):
    idxs = []
    for i in range(len(columns)):
        vals = [r[i] for r in rows[:120] if r[i] is not None]
        if vals and all(_is_number(v) for v in vals):
            idxs.append(i)
    return idxs


def _row_dict(columns, row):
    return {c: make_json_safe(v) for c, v in zip(columns, row)}


def stats_from_rows(columns, rows):
    stats = {}
    for i in _numeric_col_indices(columns, rows):
        pairs = [(float(r[i]), r) for r in rows if r[i] is not None]
        if pairs:
            vals = [v for v, _ in pairs]
            lo = min(pairs, key=lambda p: p[0])[1]
            hi = max(pairs, key=lambda p: p[0])[1]
            stats[columns[i]] = {
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "avg": round(sum(vals) / len(vals), 4),
                "count": len(vals),
                "min_at": _row_dict(columns, lo),
                "max_at": _row_dict(columns, hi),
            }
    return stats


def stats_from_sql(cur, base_sql, columns, rows):
    idxs = _numeric_col_indices(columns, rows)
    names = [columns[i] for i in idxs]
    select = ["COUNT(*)"] + [f'MIN("{n}"), MAX("{n}"), AVG("{n}")' for n in names]
    cur.execute(f"SELECT {', '.join(select)} FROM ({base_sql}) _sub;")
    row = cur.fetchone()
    total = int(row[0])
    stats = {}
    k = 1
    for n in names:
        mn, mx, av = row[k], row[k + 1], row[k + 2]
        k += 3
        if av is not None:
            stats[n] = {
                "min": round(float(mn), 4),
                "max": round(float(mx), 4),
                "avg": round(float(av), 4),
                "count": total,
            }
            for key, direction in (("min_at", "ASC"), ("max_at", "DESC")):
                cur.execute(
                    f'SELECT * FROM ({base_sql}) _sub WHERE "{n}" IS NOT NULL '
                    f'ORDER BY "{n}" {direction} LIMIT 1;'
                )
                hit = cur.fetchone()
                if hit is not None:
                    stats[n][key] = _row_dict([d[0] for d in cur.description], hit)
    return total, stats


def stats_lines(stats):
    lines = []
    for col, s in stats.items():
        def at(key):
            row = s.get(key)
            if not row:
                return ""
            others = ", ".join(f"{k}={v}" for k, v in row.items() if k != col)
            return f" (row: {others})" if others else ""
        lines.append(
            f"\n  {col}: min={round(s['min'], 2)}{at('min_at')}, "
            f"max={round(s['max'], 2)}{at('max_at')}, avg={round(s['avg'], 2)}"
        )
    return "".join(lines)


def _text_col_indices(columns, rows, limit=3):
    idxs = []
    for i in range(len(columns)):
        vals = [r[i] for r in rows[:120] if r[i] is not None]
        if vals and all(isinstance(v, str) for v in vals):
            idxs.append(i)
    return idxs[:limit]


def top_values_from_rows(columns, rows, k=6):
    out = {}
    for i in _text_col_indices(columns, rows):
        counts = Counter(r[i] for r in rows if r[i] is not None)
        if len(counts) >= 2:
            top = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:k]
            out[columns[i]] = {"distinct": len(counts), "top": [[v, n] for v, n in top]}
    return out


def top_values_from_sql(cur, base_sql, columns, rows, k=6):
    out = {}
    for i in _text_col_indices(columns, rows):
        n = columns[i]
        cur.execute(f'SELECT COUNT(DISTINCT "{n}") FROM ({base_sql}) _sub;')
        distinct = int(cur.fetchone()[0])
        if distinct < 2:
            continue
        cur.execute(
            f'SELECT "{n}", COUNT(*) FROM ({base_sql}) _sub WHERE "{n}" IS NOT NULL '
            f'GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {k};'
        )
        out[n] = {"distinct": distinct, "top": [[v, int(c)] for v, c in cur.fetchall()]}
    return out


def top_values_lines(top_values, total):
    lines = []
    for col, info in (top_values or {}).items():
        shown = ", ".join(f"{v}={c}" for v, c in info["top"])
        lines.append(
            f"\n  {col} — most frequent of {info['distinct']} distinct values "
            f"over ALL {total} rows: {shown}"
        )
    return "".join(lines)


_TEMPORAL_NAMES = {
    "date", "day", "hour", "week", "month", "year", "ts", "timestamp",
    "hour_of_day", "day_of_week", "dow", "bucket", "time_bucket", "period",
}


def _looks_temporal(name, value):
    if hasattr(value, "year"):
        return True
    n = (name or "").lower()
    return n in _TEMPORAL_NAMES or n.startswith(("date", "month", "week"))


def _col_is_numeric(rows, idx):
    seen = False
    for r in rows[:80]:
        v = r[idx]
        if v is None:
            continue
        if not _is_number(v):
            return False
        seen = True
    return seen


_BUCKETS = (
    ("hour", "ώρα", lambda t: t.replace(minute=0, second=0, microsecond=0), "%Y-%m-%d %H:00"),
    ("day", "ημέρα", lambda t: t.replace(hour=0, minute=0, second=0, microsecond=0), "%Y-%m-%d"),
    ("week", "εβδομάδα",
     lambda t: (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0),
     "%Y-%m-%d"),
    ("month", "μήνα", lambda t: t.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "%Y-%m"),
)


def _as_dt(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return None


def _bucketize(points, max_points=400):
    dts = [_as_dt(p[0]) for p in points]
    if any(d is None for d in dts):
        return None
    for name, greek, trunc, fmt in _BUCKETS:
        acc = {}
        for d, (_, series, value) in zip(dts, points):
            acc.setdefault((trunc(d), series), []).append(value)
        if len({k[0] for k in acc}) <= max_points:
            out = [
                (k[0].strftime(fmt), k[1], round(sum(v) / len(v), 4))
                for k, v in sorted(acc.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))
            ]
            return out, name, greek
    return None


def _aggregated_info(bucket, greek, from_points):
    return {"bucket": bucket, "bucket_el": greek, "function": "avg", "from_points": from_points}


_BAR_MAX = 60
_BAR_TOP = 30
_RANGE_MAX = 10


def _top_n_bar(labels, values):
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)[:_BAR_TOP]
    info = {"shown": len(order), "total_categories": len(values)}
    return [labels[i] for i in order], [values[i] for i in order], info


def _build_chart_core(columns, rows):
    if not rows or len(rows) < 2 or len(columns) < 2:
        return None
    if (columns[0] or "").lower() == "id":
        return None

    ncols = len(columns)
    numeric_idx = [i for i in range(1, ncols) if _col_is_numeric(rows, i)]
    if not numeric_idx:
        return None
    text_idx = [i for i in range(1, ncols) if i not in numeric_idx]

    label_name = columns[0]
    label_sample = next((r[0] for r in rows if r[0] is not None), rows[0][0])
    temporal = _looks_temporal(label_name, label_sample)
    distinct_labels = len({r[0] for r in rows})

    lname = {i: (columns[i] or "").lower() for i in numeric_idx}
    min_i = next((i for i in numeric_idx if "min" in lname[i]), None)
    max_i = next((i for i in numeric_idx if "max" in lname[i]), None)
    if min_i is not None and max_i is not None and not temporal and distinct_labels > _RANGE_MAX:
        return None
    if min_i is not None and max_i is not None and distinct_labels <= (40 if temporal else _RANGE_MAX):
        pts = [
            {"label": str(r[0]), "min": float(r[min_i]), "max": float(r[max_i])}
            for r in rows if r[min_i] is not None and r[max_i] is not None
        ]
        if pts:
            return {
                "type": "range", "points": pts, "x_label": label_name,
                "y_label": lname[max_i].replace("max_", "").replace("_max", "") or "value",
            }

    def multi_line(raw, y_label, series_label):
        agg = None
        if len(raw) > 400:
            n_orig = len(raw)
            rolled = _bucketize(raw)
            if rolled is None:
                return None
            raw = rolled[0]
            agg = _aggregated_info(rolled[1], rolled[2], n_orig)
        pts = [{"label": str(l), "series": s, "value": round(v, 4)} for l, s, v in raw]
        chart = {
            "type": "multi_line", "points": pts, "x_label": label_name,
            "y_label": y_label, "series_label": series_label,
        }
        if agg:
            chart["aggregated"] = agg
        return chart

    if temporal and len(text_idx) == 1 and len(numeric_idx) == 1:
        cat_i, val_i = text_idx[0], numeric_idx[0]
        cats = {str(r[cat_i]) for r in rows}
        if 2 <= len(cats) <= 6 and len(rows) >= 2 * len(cats):
            raw = [(r[0], str(r[cat_i]), float(r[val_i])) for r in rows if r[val_i] is not None]
            if raw:
                return multi_line(raw, columns[val_i], columns[cat_i])

    if temporal and len(numeric_idx) >= 2:
        raw = [(r[0], columns[i], float(r[i])) for r in rows for i in numeric_idx if r[i] is not None]
        if raw:
            return multi_line(raw, "value", "μέγεθος")

    val_i = numeric_idx[0]
    try:
        used = [r for r in rows if r[val_i] is not None]
        values = [round(float(r[val_i]), 4) for r in used]
    except (ValueError, TypeError):
        return None
    if len(values) < 2:
        return None

    labels = [str(r[0]) for r in used]
    agg = None
    if temporal:
        if len(used) > 800:
            rolled = _bucketize([(r[0], None, float(r[val_i])) for r in used])
            if rolled is None:
                return None
            labels = [p[0] for p in rolled[0]]
            values = [p[2] for p in rolled[0]]
            agg = _aggregated_info(rolled[1], rolled[2], len(used))
        chart_type = "line"
    else:
        chart_type = "bar"
        if len(used) > _BAR_MAX:
            if distinct_labels != len(used):
                return None
            labels, values, top_info = _top_n_bar(labels, values)
    chart = {
        "type": chart_type,
        "labels": labels,
        "values": values,
        "x_label": label_name,
        "y_label": columns[val_i],
    }
    if agg:
        chart["aggregated"] = agg
    if chart_type == "bar" and len(used) > _BAR_MAX:
        chart["top_n"] = top_info
    return chart


def build_chart_data(columns, rows, total=None):
    chart = _build_chart_core(columns, rows)
    if chart and total and total > len(rows):
        chart["partial"] = {"shown": len(rows), "total": total}
    return chart


def _sql_prompt(question, history_block):
    dom = get_domain()
    parts = [
        "You convert user questions in Greek into a single PostgreSQL query.",
        "",
        dom.schema_context,
        "",
        dom.rules_block,
    ]
    if dom.examples_block:
        parts += ["", dom.examples_block]
    fresh = freshness_note()
    if fresh:
        parts += ["", fresh]
    if history_block:
        parts += ["", history_block]
    parts += ["", f"USER QUESTION (in Greek): {question}", "", "SQL QUERY:"]
    return "\n".join(parts)


def generate_sql(question, history, timings):
    start = time.perf_counter()
    prompt = _sql_prompt(question, format_chat_history(history, question))
    logger.debug("SQL prompt: %d chars (~%d tokens)", len(prompt), len(prompt) // 3)
    raw = call_ollama(prompt, model=SQL_MODEL, num_predict=512, stop=SQL_STOP_TOKENS)
    sql = clean_sql(raw)
    timings["sql_generation"] = round(time.perf_counter() - start, 3)
    logger.info("Generated SQL (history turns sent: %d): %s", len(history or []), " ".join(sql.split()))
    return sql


def _fix_prompt(question, bad_sql, error):
    dom = get_domain()
    fresh = freshness_note()
    return (
        f"{dom.schema_context}\n\n{dom.rules_block}\n\n"
        + (f"{fresh}\n\n" if fresh else "")
        + f"The user asked (in Greek): {question}\n\n"
        f"This query failed:\n{bad_sql}\n\nError:\n{error}\n\n"
        "Return ONLY a corrected PostgreSQL SELECT query that answers the question."
    )


_NOW_TS_RE = re.compile(
    r"\b(?:NOW\s*\(\s*\)|CURRENT_TIMESTAMP(?:\s*\(\s*\d*\s*\))?|LOCALTIMESTAMP(?:\s*\(\s*\d*\s*\))?)",
    re.IGNORECASE,
)
_NOW_DATE_RE = re.compile(r"\bCURRENT_DATE\b", re.IGNORECASE)


def apply_time_anchor(sql):
    dom = get_domain()
    if not dom.static or not dom.time_anchor:
        return sql, 0
    table, col = dom.time_anchor
    sql, n_ts = _NOW_TS_RE.subn(f"(SELECT MAX({col}) FROM {table})", sql)
    sql, n_date = _NOW_DATE_RE.subn(f"(SELECT MAX({col})::date FROM {table})", sql)
    return sql, n_ts + n_date


def run_sql(sql, timings, retries_used, question=""):
    start = time.perf_counter()
    current_sql = sql
    last_error = None

    anchor_rewrites = 0
    entity_info = None
    for attempt in range(MAX_RETRIES + 1):
        if is_safe_select(current_sql):
            current_sql, anchor_rewrites = apply_time_anchor(current_sql)
            if anchor_rewrites:
                logger.info("Time anchor: %d CURRENT_DATE/NOW() replaced by the newest row", anchor_rewrites)
        if not is_safe_select(current_sql):
            last_error = "generated text is not a valid SELECT statement"
            logger.warning("Attempt %d not a safe SELECT: %s", attempt + 1, current_sql[:200])
            if attempt < MAX_RETRIES:
                current_sql = clean_sql(
                    call_ollama(
                        _fix_prompt(question, current_sql, last_error),
                        model=SQL_MODEL, num_predict=512, stop=SQL_STOP_TOKENS,
                    )
                )
                logger.info("Corrected SQL after error: %s", current_sql)
                continue
            break

        conn = None
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            conn.set_session(readonly=True)
            cur = conn.cursor()
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS};")
            cur.execute(current_sql)
            if cur.description is None:
                raise RuntimeError("Query returned no result set.")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()

            capped = "/* auto-limit */" in current_sql and len(rows) >= MAX_RESULT_ROWS
            if capped:
                base = _AUTO_LIMIT_RE.sub("", current_sql).rstrip(";")
                if not _HAS_ORDER_RE.search(base):
                    base = base + " ORDER BY 1"
                    cur.execute(f"{base} LIMIT {MAX_RESULT_ROWS};")
                    rows = cur.fetchall()
                    current_sql = f"{base} LIMIT {MAX_RESULT_ROWS} /* auto-limit */;"
                total_count, stats = stats_from_sql(cur, base, columns, rows)
                top_values = top_values_from_sql(cur, base, columns, rows)
                if is_raw_listing(base):
                    entity = _entity_column(columns, rows)
                    if entity:
                        cur.execute(f'SELECT COUNT(DISTINCT "{entity}") FROM ({base}) _t;')
                        entity_info = {"column": entity, "distinct": int(cur.fetchone()[0])}
            else:
                total_count, stats = len(rows), stats_from_rows(columns, rows)
                top_values = top_values_from_rows(columns, rows)
            cur.close()

            timings["sql_execution"] = round(time.perf_counter() - start, 3)
            retries_used.append(attempt)
            summary = {
                "total_count": total_count,
                "truncated": total_count > len(rows),
                "stats": stats,
                "top_values": top_values,
                "anchor_rewrites": anchor_rewrites,
                "entity": entity_info,
                "full_sql": base if capped else None,
            }
            return strip_auto_limit_marker(current_sql), columns, rows, summary
        except HTTPException:
            raise
        except Exception as e:
            last_error = str(e).strip()
            logger.warning("Attempt %d failed: %s", attempt + 1, last_error)
        finally:
            if conn is not None:
                conn.close()

        if attempt < MAX_RETRIES:
            current_sql = clean_sql(
                call_ollama(
                    _fix_prompt(question, current_sql, last_error),
                    model=SQL_MODEL, num_predict=512, stop=SQL_STOP_TOKENS,
                )
            )
            logger.info("Corrected SQL after error: %s", current_sql)

    timings["sql_execution"] = round(time.perf_counter() - start, 3)
    retries_used.append(MAX_RETRIES)
    raise SqlUnrecoverable(last_error or "query failed", current_sql)


ANSWER_SYSTEM_RULES = """Απαντάς σε ερωτήσεις για μια βάση δεδομένων, ΜΟΝΟ στα ελληνικά και ΜΟΝΟ με βάση τα δεδομένα που σου δίνονται.

ΤΙ ΝΑ ΠΕΙΣ:
- Το SQL ερώτημα ΗΔΗ εφάρμοσε όλες τις συνθήκες της ερώτησης στο WHERE — ακόμα κι αν μια στήλη που αναφέρεται στην ερώτηση (π.χ. κίνδυνος, συντήρηση) δεν εμφανίζεται στις στήλες του αποτελέσματος. Οι γραμμές που βλέπεις ΗΔΗ ικανοποιούν αυτές τις συνθήκες. ΜΗΝ πεις ότι λείπουν δεδομένα ή ότι η βάση δεν έχει αυτή την πληροφορία απλά επειδή δεν τη βλέπεις σαν ξεχωριστή στήλη — κοίτα το SQL για να καταλάβεις τι ήδη φιλτραρίστηκε.
- Χρησιμοποίησε τους ΑΚΡΙΒΕΙΣ αριθμούς από τα δεδομένα. Μην εφευρίσκεις ή υποθέτεις τιμές.
- ΑΡΙΘΜΟΙ: γράψε τους όπως είναι στα δεδομένα — τελεία για δεκαδικά (74.86), ΧΩΡΙΣ διαχωριστικό χιλιάδων (100000, όχι 100.000). Οι δεκαδικοί με 2 ψηφία· οι ακέραιοι (πλήθη) χωρίς δεκαδικά (100000, όχι 100000.00).
- Περιέγραψε ακριβώς το μέγεθος που ρωτάει η ερώτηση (κοίτα τα ονόματα στηλών). Μην το μπερδεύεις με άλλο μέγεθος. Ακολούθησε και τη σωστή πράξη (μέσος όρος / σύνολο / μέγιστο / πλήθος).
- Για σύνολα και μέσους όρους, χρησιμοποίησε το μπλοκ "Aggregates over ALL rows" αν υπάρχει — μην τα υπολογίζεις μόνος σου από το δείγμα.
- 2-4 προτάσεις, ή σύντομη λίστα με παύλες για πολλά αποτελέσματα.
- Ανάφερε ΟΛΕΣ τις γραμμές που βλέπεις στα δεδομένα. Αν υπάρχουν κι άλλες, το συνολικό πλήθος το προσθέτει το σύστημα — εσύ μην το γράφεις και μην πεις ότι λείπουν.
- Πλήθος ανά κατηγορία: ανάφερε ΟΛΕΣ τις κατηγορίες με παύλες.
- Χρονοσειρά (ανά ημέρα/ώρα): ΜΗΝ μετράς γραμμές. Μία πρόταση: από πού ξεκίνησε, πού κατέληξε, ελάχιστο/μέγιστο, και η γενική τάση.

ΓΛΩΣΣΑ (πολύ σημαντικό):
- ΜΟΝΟ σωστά ελληνικά. ΠΟΤΕ λέξη που δεν υπάρχει στα ελληνικά. Αν δεν είσαι σίγουρος, βάλε πιο απλή σωστή λέξη.
- Τρίτο πρόσωπο: "Η μέση θερμοκρασία είναι...", όχι "Βρήκα ότι...".
- Ονόματα, κατηγορίες και μονάδες: γράψε τα ΑΚΡΙΒΩΣ όπως είναι στα δεδομένα, χωρίς μετάφραση.
- Σωστές κλίσεις ρημάτων: "είναι", "χρειάζονται", "καταγράφηκαν", "κυμάνθηκε".
- Σωστό γένος: "ο αριθμός", "η μηχανή", "το σύνολο". Σωστή χρήση επιθέτων: "υψηλός κίνδυνος", "μέση θερμοκρασία", "συνολική κατανάλωση".
- Μη γράφεις Python tuples όπως ('κατηγορία', 1829). Γράψε φυσικά.

ΕΞΟΔΟΣ:
- Γράψε ΜΟΝΟ την απάντηση. ΠΟΤΕ μην επαναλαμβάνεις ή παραθέτεις την ερώτηση. Χωρίς εισαγωγικά σχόλια.

ΠΑΡΑΔΕΙΓΜΑΤΑ ΜΟΡΦΗΣ (τα [...] είναι placeholders — δες τις πραγματικές τιμές από τα δεδομένα, ΠΟΤΕ μην αντιγράφεις τα ονόματα των παραδειγμάτων):
Q: μέση τιμή ενός μεγέθους
A: Η μέση [μέγεθος] είναι [αριθμός] [μονάδα].
Q: ποιες οντότητες ικανοποιούν μια συνθήκη
A: Οι [οντότητες] που ικανοποιούν τη συνθήκη είναι: [όνομα] ([τιμή]), [όνομα] ([τιμή]).
Q: πλήθος ανά κατηγορία
A: Το πλήθος ανά [κατηγορία] είναι:
- [κατηγορία]: [αριθμός]
- [κατηγορία]: [αριθμός]
Q: εξέλιξη μέσης τιμής ανά ημέρα
A: Η μέση [μέγεθος] κυμάνθηκε από [min] [μονάδα] ([ημερομηνία]) έως [max] [μονάδα] ([ημερομηνία]), με [ανοδική / καθοδική / γενικά σταθερή] τάση."""


def _answer_rules_text():
    extra = get_domain().answer_rules_block
    return f"{ANSWER_SYSTEM_RULES}\n\n{extra}" if extra else ANSWER_SYSTEM_RULES


def _clean_answer(text, question):
    text = (text or "").strip()
    text = re.sub(r"^\s*(ΑΠΑΝΤΗΣΗ|ANSWER)\s*:?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*Απ[αά]ντ\w*\s*:\s*", "", text, flags=re.IGNORECASE)
    q = question.strip().rstrip("; ·.?").lower()
    for _ in range(2):
        first, _nl, rest = text.partition("\n")
        f = first.strip().lstrip("-•*→ ").rstrip("; ·.?:").lower()
        near_equal = f == q or (q and f in q and len(f) >= 0.7 * len(q)) or (
            q and q in f and len(f) <= 1.25 * len(q)
        )
        if f and near_equal and rest.strip():
            text = rest.strip()
        else:
            break
    return text.strip()


def apply_answer_fixes(text):
    for wrong, right in get_domain().answer_fixes.items():
        text = text.replace(wrong, right)
        if wrong[:1].islower():
            text = text.replace(wrong[:1].upper() + wrong[1:], right[:1].upper() + right[1:])
    return text


def _format_rows(columns, rows):
    if len(rows) == 1:
        pairs = [f"{c} = {make_json_safe(v)}" for c, v in zip(columns, rows[0])]
        return "Result (1 row): " + " | ".join(pairs)
    lines = []
    for i, r in enumerate(rows, 1):
        pairs = [f"{c}={make_json_safe(v)}" for c, v in zip(columns, r)]
        lines.append(f"  {i}. " + " | ".join(pairs))
    return "Rows:\n" + "\n".join(lines)


_CYCLIC_HOUR_NAMES = {"hour_of_day", "hour", "hod"}


def _hour_label(v):
    try:
        return f"{int(float(v)):02d}:00"
    except (TypeError, ValueError):
        return str(v)


def timeseries_summary(columns, rows):
    if len(rows) < 4 or len(columns) < 2:
        return None
    label_sample = next((r[0] for r in rows if r[0] is not None), None)
    if not _looks_temporal(columns[0], label_sample):
        return None
    num = [i for i in range(1, len(columns)) if _col_is_numeric(rows, i)]
    if len(num) != 1:
        return None
    vi = num[0]
    pts = [(str(r[0]), round(float(r[vi]), 2)) for r in rows if r[vi] is not None]
    if len(pts) < 4:
        return None
    cyclic = (columns[0] or "").lower() in _CYCLIC_HOUR_NAMES and not hasattr(label_sample, "year")
    if cyclic:
        pts = [(_hour_label(r[0]), round(float(r[vi]), 2)) for r in rows if r[vi] is not None]
    lo = min(pts, key=lambda p: p[1])
    hi = max(pts, key=lambda p: p[1])
    delta = pts[-1][1] - pts[0][1]
    spread = hi[1] - lo[1]
    if spread == 0:
        trend = "σταθερή"
    elif abs(delta) < 0.34 * spread:
        trend = "γενικά σταθερή"
    elif delta > 0:
        trend = "ανοδική"
    else:
        trend = "καθοδική"
    return {
        "metric": columns[vi], "n": len(pts),
        "start": pts[0], "end": pts[-1], "min": lo, "max": hi, "trend": trend,
        "cyclic": cyclic,
    }


def rows_for_writer(sql, n_rows):
    m = re.search(r"\bLIMIT\s+(\d+)\b", _outer_sql(sql or ""), re.IGNORECASE)
    if m:
        return min(int(m.group(1)), SAMPLE_ROWS_MAX)
    return SAMPLE_ROWS_FOR_LLM


def generate_answer(question, sql, columns, rows, summary, timings, feedback=None):
    start = time.perf_counter()
    sample = rows[:rows_for_writer(sql, len(rows))]
    total = summary["total_count"]

    ts = timeseries_summary(columns, rows)
    if not rows:
        data_block = "The query returned no rows."
    elif ts and ts.get("cyclic"):
        data_block = (
            f"Profile by hour of the day (00:00–23:00) — {ts['metric']}, {ts['n']} hours. "
            "Each value is an average over ALL days of the period asked in the question; "
            "it is NOT a sequence in time, so there is no start, end or trend.\n"
            f"  lowest:  {ts['min'][1]} (at {ts['min'][0]})\n"
            f"  highest: {ts['max'][1]} (at {ts['max'][0]})\n"
            "\nSummarise in ONE Greek sentence: say that this is the average per hour of "
            "the day over the period from the question, then give the highest and the "
            "lowest value with their hours written like «στις 20:00». Do NOT mention a "
            "trend and do NOT list individual hours."
        )
    elif ts:
        window_truncated = total > len(rows)
        data_block = (
            f"Time series — {ts['metric']}, {ts['n']} points"
            + (f" (most recent {ts['n']} of {total} matching rows — this is NOT the full range)"
               if window_truncated else "") + ".\n"
            f"  start:   {ts['start'][1]} ({ts['start'][0]})\n"
            f"  end:     {ts['end'][1]} ({ts['end'][0]})\n"
            f"  lowest{' in this window' if window_truncated else ''}:  {ts['min'][1]} ({ts['min'][0]})\n"
            f"  highest{' in this window' if window_truncated else ''}: {ts['max'][1]} ({ts['max'][0]})\n"
            f"  trend{' in this window' if window_truncated else ''}: {ts['trend']}\n"
        )
        if window_truncated and summary["stats"]:
            data_block += (
                f"\nTRUE min/max/avg over ALL {total} matching rows (the window above is "
                "only a recent slice, NOT the full range) — use THESE for the overall "
                "lowest/highest, not the window ones above:"
            )
            data_block += stats_lines(summary["stats"])
            data_block += top_values_lines(summary.get("top_values"), total)
        data_block += (
            "\nSummarise in ONE Greek sentence using the time-series template. "
            "Do NOT list individual points."
        )
    else:
        data_block = _format_rows(columns, sample)
        if len(rows) > len(sample):
            data_block += (
                f"\n\nThese are {len(sample)} example rows out of {total} total. "
                "Do NOT estimate a total / average / min / max by eyeballing these "
                "examples — if the question asks for one, use the exact aggregates "
                "below instead."
            )
        if len(rows) > len(sample) and (summary["stats"] or summary.get("top_values")):
            data_block += (
                f"\n\nExact aggregates over ALL {total} rows "
                "(use these only if the question asks for a total / average / min / max; \"row:\" names which machine / time holds that min or max):"
            )
            data_block += stats_lines(summary["stats"])
            data_block += top_values_lines(summary.get("top_values"), total)

    prompt = (
        f"{_answer_rules_text()}\n\n"
        f"SQL query that was executed (already applied every filter in the "
        f"question, including any not shown as a column below):\n{sql}\n\n"
        f"Query result:\n{data_block}\n\n"
        f"Question: {question}\n\n"
        + (
            "YOUR PREVIOUS ANSWER WAS REJECTED because of these problems: "
            f"{feedback}. (number / name = a value that does not appear in the data; "
            "mixed_alphabet = a garbled word mixing Greek and Latin letters; "
            "translated_label = a category value from the data that you translated or "
            "rewrote instead of copying it exactly as given.) Rewrite it "
            "in correct Greek, using ONLY numbers and names that appear in the query "
            "result above.\n\n"
            if feedback else ""
        )
        + "Reply in Greek with only the answer (no preamble, do not repeat the question).\n"
        "ΑΠΑΝΤΗΣΗ:"
    )
    raw = call_ollama(prompt, model=ANSWER_MODEL, num_predict=450)
    answer = apply_answer_fixes(_clean_answer(raw, question) or raw.strip())
    timings["answer_generation"] = round(time.perf_counter() - start, 3)
    return answer


_NUM_TOKEN_RE = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*(?![\w])")
_NAME_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")


def _num_candidates(token):
    out = set()
    tk = token.strip()
    try:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", tk):
            out.add(float(tk.replace(".", "")))
        if re.fullmatch(r"\d{1,3}(,\d{3})+", tk):
            out.add(float(tk.replace(",", "")))
        if "," in tk and "." in tk:
            out.add(float(tk.replace(".", "").replace(",", ".")))
        elif re.fullmatch(r"\d+[.,]\d+", tk):
            out.add(float(tk.replace(",", ".")))
        elif tk.isdigit():
            out.add(float(tk))
    except ValueError:
        pass
    return out


def _collect_allowed(question, sql, columns, rows, summary):
    nums, names = set(), set()

    def add_num(x):
        try:
            f = float(x)
        except (TypeError, ValueError):
            return
        nums.update({round(f, 2), round(f, 4), float(round(f))})

    def add_text(s):
        for m in _NUM_TOKEN_RE.findall(s):
            for c in _num_candidates(m):
                nums.add(c)
        for m in _NAME_TOKEN_RE.findall(s):
            names.add(m.lower())

    def add_value(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float, Decimal)):
            add_num(v)
        else:
            add_text(str(make_json_safe(v)))

    for r in rows:
        for v in r:
            add_value(v)
    for c in columns:
        add_text(str(c))
    add_text(question or "")
    add_text(sql or "")
    for s in (summary.get("stats") or {}).values():
        for k in ("min", "max", "avg", "count"):
            add_num(s.get(k))
        for k in ("min_at", "max_at"):
            for v in (s.get(k) or {}).values():
                add_value(v)
    for info in (summary.get("top_values") or {}).values():
        add_num(info.get("distinct"))
        for v, c in info.get("top", []):
            add_value(v)
            add_num(c)
    add_num(summary.get("total_count"))
    add_num(len(rows))
    return nums, names


_GREEK_LETTER_RE = re.compile(r"[Α-Ωα-ωΆ-Ώά-ώ]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def mixed_script_words(text, allowed_names=()):
    out = []
    for tok in sorted(set(re.findall(r"\w+", text or ""))):
        if _GREEK_LETTER_RE.search(tok) and _LATIN_LETTER_RE.search(tok):
            low = tok.lower()
            if not any(n in low for n in allowed_names):
                out.append(tok)
    return out


def required_enum_values(columns, rows, sql):
    if not rows or len(rows) > rows_for_writer(sql, len(rows)):
        return []
    enum_values = get_domain().enum_values
    if not enum_values:
        return []
    required = set()
    for i, col in enumerate(columns):
        known = enum_values.get(col)
        if not known:
            continue
        known_set = set(known)
        for r in rows:
            v = r[i]
            if isinstance(v, str) and v in known_set:
                required.add(v)
    return sorted(required)


def verify_answer(answer, question, sql, columns, rows, summary):
    nums, names = _collect_allowed(question, sql, columns, rows, summary)
    issues = []
    text_lower = (answer or "").lower()
    for label in required_enum_values(columns, rows, sql):
        if label.lower() not in text_lower:
            issues.append({"type": "translated_label", "value": label})
    for tok in _NUM_TOKEN_RE.findall(answer or ""):
        cands = _num_candidates(tok)
        if not cands:
            continue
        if any(abs(c) <= 10 and c == int(c) for c in cands):
            continue
        if not any(abs(c - a) <= 0.0051 for c in cands for a in nums):
            issues.append({"type": "number", "value": tok})
    for tok in sorted(set(_NAME_TOKEN_RE.findall(answer or ""))):
        if tok.lower() not in names:
            issues.append({"type": "name", "value": tok})
    for tok in mixed_script_words(answer, names):
        issues.append({"type": "mixed_alphabet", "value": tok})
    return issues


_AGG_WORDS_RE = re.compile(
    r"\b(GROUP\s+BY|DISTINCT|HAVING|UNION|OVER|COUNT|SUM|AVG|MIN|MAX|STDDEV\w*|"
    r"PERCENTILE\w*|MEDIAN|ARRAY_AGG|STRING_AGG)\b",
    re.IGNORECASE,
)


def _outer_sql(sql):
    out, stack, i = [], [], 0
    while i < len(sql):
        ch = sql[i]
        if ch == "(":
            stack.append(bool(re.match(r"\s*SELECT\b", sql[i + 1:], re.IGNORECASE)))
        elif ch == ")" and stack:
            stack.pop()
            i += 1
            continue
        if not any(stack):
            out.append(ch)
        i += 1
    return "".join(out)


def is_raw_listing(sql):
    return not _AGG_WORDS_RE.search(_outer_sql(sql or ""))


def _entity_column(columns, rows):
    text_cols = [
        c for i, c in enumerate(columns)
        if (c or "").lower() != "id" and not (c or "").lower().endswith("_id")
        and rows and isinstance(rows[0][i], str)
    ]
    named = [c for c in text_cols if "name" in c.lower()]
    return (named or text_cols or [None])[0]


def listing_answer(columns, rows, total, entity=None, n_examples=3):
    text = f"Βρέθηκαν {_fmt_int(total)} εγγραφές"
    if not entity:
        return text + "."
    col, distinct = entity["column"], entity["distinct"]
    noun = get_domain().column_nouns.get(col, col)
    idx = columns.index(col)
    examples = []
    for r in rows:
        if r[idx] not in examples:
            examples.append(r[idx])
        if len(examples) == n_examples:
            break
    return (
        f"{text}, που αφορούν {_fmt_int(distinct)} διαφορετικές τιμές ({noun}). "
        f"Παραδείγματα: {', '.join(str(e) for e in examples)}."
        if noun == col else
        f"{text}, που αφορούν {_fmt_int(distinct)} διαφορετικές {noun}. "
        f"Παραδείγματα: {', '.join(str(e) for e in examples)}."
    )


_GREEK_THOUSANDS_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:\.\d{3})+(?![\w]|[.,]\d)")


def normalize_numbers(text, nums):
    def fix(m):
        tok = m.group(0)
        whole, dec = float(tok.replace(".", "")), float(tok)
        has = lambda x: any(abs(x - a) <= 0.0051 for a in nums)
        return tok.replace(".", "") if has(whole) and not has(dec) else tok
    return _GREEK_THOUSANDS_RE.sub(fix, text or "")


def _plain_number(v):
    v = make_json_safe(v)
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(round(v, 2)) if isinstance(v, float) else str(v)


def _fmt_int(n):
    return str(int(n))


def answer_notes(summary, chart_data, n_rows, answer=""):
    notes = []
    total = summary.get("total_count") or n_rows
    is_series = bool(chart_data) and chart_data.get("type") in ("line", "multi_line")
    is_list = len(re.findall(r"(?m)^\s*[-•*]\s", answer)) >= 2
    if total > 5 and is_list and not is_series and str(total) not in answer and _fmt_int(total) not in answer:
        notes.append(f"Σύνολο: {_fmt_int(total)} αποτελέσματα.")
    if chart_data:
        agg = chart_data.get("aggregated")
        if agg:
            notes.append(
                f"Το γράφημα δείχνει μέσους όρους ανά {agg['bucket_el']} "
                f"({_fmt_int(agg['from_points'])} μετρήσεις συνολικά)."
            )
        top = chart_data.get("top_n")
        if top:
            notes.append(
                f"Το γράφημα δείχνει τις {top['shown']} μεγαλύτερες κατηγορίες "
                f"από {_fmt_int(top['total_categories'])}· οι υπόλοιπες δεν εμφανίζονται."
            )
    return notes


_QUERY_FAILED_MESSAGE = (
    "Δεν κατάφερα να μετατρέψω αυτή την ερώτηση σε σωστό ερώτημα προς τη βάση. "
    "Δοκίμασε να τη διατυπώσεις πιο απλά ή πιο συγκεκριμένα."
)
_REFUSAL_FALLBACK = (
    "Δεν μπορώ να απαντήσω σε αυτή την ερώτηση με τα διαθέσιμα δεδομένα."
)


def _is_unanswerable_sentinel(columns, rows):
    return (
        len(rows) == 1
        and len(columns) == 1
        and str(rows[0][0]).strip().upper() == "UNANSWERABLE"
    )


def generate_refusal(question, timings):
    start = time.perf_counter()
    prompt = (
        f"{_answer_rules_text()}\n\n"
        "The question cannot be answered from the available data — the requested "
        "information is not in the database schema.\n\n"
        f"Question: {question}\n\n"
        "Reply in ONE short Greek sentence: politely say this information is not "
        "available in the data. Do not guess an answer, do not repeat the question, "
        "do not apologise more than once.\n"
        "ΑΠΑΝΤΗΣΗ:"
    )
    try:
        raw = call_ollama(prompt, model=ANSWER_MODEL, num_predict=120)
        answer = _clean_answer(raw, question) or raw.strip()
    except OllamaError:
        answer = ""
    timings["answer_generation"] = round(time.perf_counter() - start, 3)
    return answer or _REFUSAL_FALLBACK


app = FastAPI(title="NL2SQL API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

MODEL_READY = threading.Event()


def warmup():
    try:
        logger.info("Warmup: loading %s ...", SQL_MODEL)
        start = time.perf_counter()
        call_ollama("SELECT 1;", model=SQL_MODEL)
        MODEL_READY.set()
        logger.info("Warmup done in %.1fs", time.perf_counter() - start)
    except OllamaError as e:
        logger.warning("Warmup failed (will retry on first request): %s", e)


@app.on_event("startup")
def on_startup():
    try:
        get_domain()
    except Exception as e:
        logger.error("Domain not loaded at startup (will retry on first request): %s", e)
    if WARMUP_ON_STARTUP:
        threading.Thread(target=warmup, daemon=True).start()


class AskRequest(BaseModel):
    question: str
    history: Optional[list] = None


@app.get("/health")
def health():
    info = {"status": "ok", "sql_model": SQL_MODEL, "answer_model": ANSWER_MODEL,
            "model_ready": MODEL_READY.is_set()}
    try:
        dom = get_domain()
        info["tables"] = dom.tables
        info["static_dataset"] = dom.static
        info["prompt_chars"] = dom.prompt_chars
        info["prompt_tokens_est"] = dom.prompt_chars // 3
        info["num_ctx"] = OLLAMA_NUM_CTX
        info["domain_warnings"] = dom.warnings
        data_now = get_data_now()
        info["data_latest_ts"] = data_now.isoformat(sep=" ") if data_now else None
    except Exception as e:
        info["status"] = "degraded"
        info["domain_error"] = str(e)
    return info


@app.post("/admin/reload")
def admin_reload():
    try:
        dom = get_domain(force=True)
        _DATA_NOW.update(value=None, at=0.0)
        return {"status": "reloaded", "tables": dom.tables,
                "warnings": dom.warnings, "prompt_chars": dom.prompt_chars}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reload failed: {e}")


class ExportRequest(BaseModel):
    sql: str


@app.post("/export")
def export(req: ExportRequest):
    sql = req.sql.strip()
    if not is_safe_select(sql):
        raise HTTPException(status_code=400, detail="Not a safe read-only SELECT.")
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS};")
        cur.execute(sql.rstrip(";") + ";")
        if cur.description is None:
            raise HTTPException(status_code=400, detail="Query returned no result set.")
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if conn is not None:
            conn.close()
    return {
        "columns": columns,
        "rows": [[make_json_safe(v) for v in r] for r in rows],
        "row_count": len(rows),
    }


@app.post("/ask")
def ask(req: AskRequest):
    timings, retries_used = {}, []
    t0 = time.perf_counter()
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    answerable = True
    failure_kind = None
    try:
        sql = generate_sql(question, req.history or [], timings)
        executed_sql, columns, rows, summary = run_sql(
            sql, timings, retries_used, question=question
        )
        code_written = (
            summary["truncated"] and is_raw_listing(executed_sql)
            and timeseries_summary(columns, rows) is None
            and not _is_unanswerable_sentinel(columns, rows)
        )
        if code_written:
            answer = listing_answer(columns, rows, summary["total_count"], summary.get("entity"))
            timings["answer_generation"] = 0.0
        else:
            answer = generate_answer(question, executed_sql, columns, rows, summary, timings)
        chart_data = build_chart_data(columns, rows, summary["total_count"])
        if _is_unanswerable_sentinel(columns, rows):
            raise SqlUnrecoverable("model flagged the question as unanswerable", executed_sql, kind="unanswerable")
        if not code_written:
            answer = normalize_numbers(answer, _collect_allowed(question, executed_sql, columns, rows, summary)[0])
        issues = [] if code_written else verify_answer(answer, question, executed_sql, columns, rows, summary)
        retried = False
        if issues:
            feedback = "; ".join(f"{i['type']} {i['value']}" for i in issues)
            logger.warning("Answer failed verification (%s) — regenerating once", feedback)
            retry_timings = {}
            answer2 = generate_answer(
                question, executed_sql, columns, rows, summary, retry_timings, feedback=feedback
            )
            timings["answer_regeneration"] = retry_timings.get("answer_generation")
            answer2 = normalize_numbers(answer2, _collect_allowed(question, executed_sql, columns, rows, summary)[0])
            issues2 = verify_answer(answer2, question, executed_sql, columns, rows, summary)
            retried = True
            if len(issues2) <= len(issues):
                answer, issues = answer2, issues2
            if issues and any(i["type"] == "number" for i in issues) and len(rows) == 1 and len(columns) == 1:
                answer = f"Το αποτέλεσμα είναι {_plain_number(rows[0][0])}."
                issues, code_written = [], True
        verification = {"issues": issues, "retried": retried, "code_written": bool(code_written)}
        notes = answer_notes(summary, chart_data, len(rows), answer)
        if notes:
            answer = answer.rstrip() + "\n\n" + "\n".join(notes)
    except SqlUnrecoverable as e:
        logger.info("Unanswerable question '%s' (%s)", question, e.reason)
        answerable = False
        failure_kind = e.kind
        executed_sql, columns, rows = e.last_sql, [], []
        summary = {"total_count": 0, "truncated": False, "stats": {}, "top_values": {}, "full_sql": None}
        chart_data = None
        verification, notes = None, []
        if e.kind == "unanswerable":
            answer = generate_refusal(question, timings)
        else:
            answer = _QUERY_FAILED_MESSAGE
            timings["answer_generation"] = 0.0
    except OllamaError as e:
        logger.error("Ollama error on '%s': %s", question, e)
        raise HTTPException(status_code=504, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error on '%s'", question)
        raise HTTPException(status_code=500, detail=str(e))

    MODEL_READY.set()
    timings["total"] = round(time.perf_counter() - t0, 3)
    retries = retries_used[0] if retries_used else 0
    logger.info(
        "Q: '%s' | timings=%s | retries=%s | rows=%s/%s",
        question, timings, retries, len(rows), summary["total_count"],
    )

    return {
        "question": question,
        "answerable": answerable,
        "failure": failure_kind,
        "sql": executed_sql,
        "answer": answer,
        "columns": columns,
        "rows": [[make_json_safe(v) for v in r] for r in rows],
        "row_count": len(rows),
        "total_count": summary["total_count"],
        "truncated": summary["truncated"],
        "stats": summary["stats"],
        "top_values": summary.get("top_values", {}),
        "anchor_rewrites": summary.get("anchor_rewrites", 0),
        "full_sql": summary.get("full_sql"),
        "chart_data": chart_data,
        "verification": verification,
        "notice": "\n".join(notes) or None,
        "timings": timings,
        "retries": retries,
    }
