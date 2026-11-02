import logging
import os

import yaml

logger = logging.getLogger("nl2sql-api")

DOMAIN_FILE = os.getenv("DOMAIN_FILE", "domain.yaml")
DB_SCHEMA = os.getenv("DB_SCHEMA", "public")

EXAMPLE_Q_PREFIX = "Q:"
EXAMPLE_A_PREFIX = "A:"
SQL_STOP_TOKENS = [f"\n{EXAMPLE_Q_PREFIX}", "\nUSER QUESTION"]

_KNOWN_TOP_KEYS = {"dataset", "tables", "synonyms", "rules", "examples", "answer_rules", "answer_fixes", "column_nouns"}
_KNOWN_TABLE_KEYS = {"description", "columns", "values"}

_PG_TYPE_ALIASES = {
    "double precision": "float",
    "numeric": "float",
    "real": "float",
    "integer": "int",
    "bigint": "int",
    "smallint": "int",
    "character varying": "text",
    "text": "text",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "date": "date",
    "boolean": "bool",
}


def _short_type(pg_type):
    return _PG_TYPE_ALIASES.get(pg_type, pg_type)


class Domain:
    def __init__(self, cfg, introspected):
        self.cfg = cfg
        self._introspected = introspected
        ds = cfg.get("dataset", {})
        self.name = ds.get("name", "database")
        self.static = bool(ds.get("static", False))
        anchor = ds.get("time_anchor") or {}
        self.time_anchor = (
            (anchor.get("table"), anchor.get("column"))
            if anchor.get("table") and anchor.get("column")
            else None
        )
        self.tables = list(introspected.keys())
        self.synonyms = cfg.get("synonyms", {}) or {}
        self.warnings = self._validate()
        self.schema_context = self._build_schema_context()
        self.examples_block = self._build_examples_block()
        self.rules_block = self._build_rules_block()
        self.answer_rules_block = self._build_answer_rules_block()
        self.column_nouns = {
            str(k): str(v) for k, v in (cfg.get("column_nouns") or {}).items() if k
        }
        self.enum_values = {}
        for t_meta in (cfg.get("tables") or {}).values():
            for col, vals in ((t_meta or {}).get("values") or {}).items():
                if isinstance(vals, list):
                    self.enum_values.setdefault(col, []).extend(str(v) for v in vals)
        self.answer_fixes = {
            str(k): str(v) for k, v in (cfg.get("answer_fixes") or {}).items() if k
        }
        self.prompt_chars = len(self.schema_context) + len(self.examples_block) + len(self.rules_block)

    def _validate(self):
        w = []
        for k in self.cfg:
            if k not in _KNOWN_TOP_KEYS:
                w.append(f"unknown top-level key '{k}' in {DOMAIN_FILE}")
        for tname, tmeta in (self.cfg.get("tables") or {}).items():
            if tname not in self._introspected:
                w.append(f"table '{tname}' in {DOMAIN_FILE} not found in schema '{DB_SCHEMA}'")
                continue
            real_cols = {c["name"] for c in self._introspected[tname]["columns"]}
            for k in (tmeta or {}):
                if k not in _KNOWN_TABLE_KEYS:
                    w.append(f"{tname}: unknown key '{k}'")
            for col in list((tmeta or {}).get("columns", {})) + list((tmeta or {}).get("values", {})):
                if col not in real_cols:
                    w.append(f"{tname}: documented column '{col}' does not exist in the table")
        anchor = (self.cfg.get("dataset") or {}).get("time_anchor")
        if anchor and self.time_anchor and self.time_anchor[0] not in self._introspected:
            w.append(f"time_anchor.table '{self.time_anchor[0]}' not in schema")
        for msg in w:
            logger.warning("domain.yaml: %s", msg)
        return w

    def _build_schema_context(self):
        tcfg = self.cfg.get("tables", {}) or {}
        out = [f"DATABASE: {self.name} (schema introspected live from information_schema)"]
        for table in self.tables:
            meta = self._introspected[table]
            t_meta = tcfg.get(table, {}) or {}
            col_desc = t_meta.get("columns", {}) or {}
            val_map = t_meta.get("values", {}) or {}
            desc = t_meta.get("description")
            out.append("")
            out.append(f"TABLE {table}" + (f"  — {desc}" if desc else ""))
            for col in meta["columns"]:
                name = col["name"]
                bits = [f"  - {name} ({_short_type(col['type'])}"]
                if name in meta["pk"]:
                    bits.append(", pk")
                if col["not_null"] and name not in meta["pk"]:
                    bits.append(", not null")
                bits.append(")")
                line = "".join(bits)
                if name in col_desc:
                    line += f" — {col_desc[name]}"
                if name in val_map:
                    vals = ", ".join(f"'{v}'" for v in val_map[name])
                    line += f" — values: {vals}"
                out.append(line)
        rels = []
        for table in self.tables:
            for fk in self._introspected[table]["fks"]:
                rels.append(f"  - {table}.{fk['column']} -> {fk['ref_table']}.{fk['ref_column']}")
        if rels:
            out.append("")
            out.append("RELATIONSHIPS (foreign keys — use for JOINs):")
            out.extend(rels)
        if self.synonyms:
            out.append("")
            out.append("GREEK SYNONYMS (term -> column / value):")
            for term, target in self.synonyms.items():
                out.append(f'  - "{term}" -> {target}')
        return "\n".join(out)

    def _build_examples_block(self):
        examples = self.cfg.get("examples", []) or []
        if not examples:
            return ""
        lines = ["EXAMPLES:"]
        for ex in examples:
            q, sql = ex.get("q", "").strip(), ex.get("sql", "").strip()
            if q and sql:
                lines.append(f"Q: {q}")
                lines.append(f"A: {sql}")
        return "\n".join(lines)

    def _build_answer_rules_block(self):
        rules = [r.strip() for r in (self.cfg.get("answer_rules") or []) if r and r.strip()]
        if not rules:
            return ""
        return "ΕΙΔΙΚΟΙ ΚΑΝΟΝΕΣ ΓΙΑ ΑΥΤΗ ΤΗ ΒΑΣΗ ΔΕΔΟΜΕΝΩΝ:\n" + "\n".join(f"- {r}" for r in rules)

    def _build_rules_block(self):
        rules = self.cfg.get("rules", []) or []
        base = [
            "Return ONLY one PostgreSQL SELECT statement (a leading WITH is allowed). "
            "No explanation, no markdown, no backticks.",
        ]
        rules = base + [r.strip() for r in rules if r and r.strip()]
        return "RULES:\n" + "\n".join(f"- {r}" for r in rules)


_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = %s AND table_name = ANY(%s)
ORDER BY table_name, ordinal_position
"""

_PK_SQL = """
SELECT kcu.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
  AND tc.table_schema = %s AND tc.table_name = ANY(%s)
"""

_FK_SQL = """
SELECT kcu.table_name  AS src_table,
       kcu.column_name AS src_column,
       ccu.table_name  AS ref_table,
       ccu.column_name AS ref_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
"""


def _introspect(conn, schema, table_names):
    cur = conn.cursor()
    cur.execute(_COLUMNS_SQL, (schema, table_names))
    cols = {}
    for table, col, dtype, is_nullable in cur.fetchall():
        cols.setdefault(table, []).append(
            {"name": col, "type": dtype, "not_null": is_nullable == "NO"}
        )
    if not cols:
        cur.close()
        raise RuntimeError(
            f"No tables found in schema '{schema}' matching {table_names}. "
            f"Check DB_SCHEMA and the 'tables:' list in {DOMAIN_FILE}."
        )

    cur.execute(_PK_SQL, (schema, table_names))
    pks = {}
    for table, col in cur.fetchall():
        pks.setdefault(table, set()).add(col)

    cur.execute(_FK_SQL, (schema,))
    fks = {}
    for src_t, src_c, ref_t, ref_c in cur.fetchall():
        if src_t in cols:
            fks.setdefault(src_t, []).append(
                {"column": src_c, "ref_table": ref_t, "ref_column": ref_c}
            )
    cur.close()

    return {
        t: {"columns": cols[t], "pk": pks.get(t, set()), "fks": fks.get(t, [])}
        for t in table_names
        if t in cols
    }


def load_domain(conn):
    with open(DOMAIN_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    table_names = list((cfg.get("tables") or {}).keys())
    if not table_names:
        raise RuntimeError(f"{DOMAIN_FILE} has no 'tables:' entries.")
    introspected = _introspect(conn, DB_SCHEMA, table_names)
    domain = Domain(cfg, introspected)
    logger.info(
        "Domain loaded: %d table(s) %s, static=%s, %d synonyms, %d examples, "
        "prompt ~%d chars (~%d tokens), %d warning(s)",
        len(domain.tables), domain.tables, domain.static,
        len(domain.synonyms), len(cfg.get("examples", []) or []),
        domain.prompt_chars, domain.prompt_chars // 3, len(domain.warnings),
    )
    return domain
