#!/usr/bin/env python3
"""
pgstress - analyze a PostgreSQL query, seed every table it touches with
synthetic rows, then benchmark and stress-test the query.

    python pgstress.py analyze --dsn URL -q "SELECT ..."      # also CALL, DO, functions, scripts
    python pgstress.py seed    --dsn URL -f query.sql --rows 1m --rows users=50k
    python pgstress.py bench   --dsn URL -f query.sql -c 1,8,32 -d 30
    python pgstress.py all     --dsn URL -f query.sql

Requires PostgreSQL 12+ and psycopg 3 (pip install "psycopg[binary]").
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

try:
    import psycopg
except ImportError:
    sys.exit('pgstress needs psycopg 3:  pip install "psycopg[binary]"')


class PgStressError(Exception):
    def __init__(self, message, position=None):
        super().__init__(message)
        self.position = position   # 0-based offset in the user's script, when known


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ql(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def fmt_int(n) -> str:
    return f"{int(n):,}"


def parse_count(text: str) -> int:
    text = text.strip().lower().replace(",", "").replace("_", "")
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(text[-1:], 1)
    if mult != 1:
        text = text[:-1]
    return int(float(text) * mult)


# --------------------------------------------------------------------------
# Catalog model
# --------------------------------------------------------------------------

@dataclass
class Column:
    name: str
    sqltype: str          # format_type() output, used for the final cast
    base: str             # base type name (domains resolved)
    typtype: str          # b = base, e = enum, ...
    category: str         # pg_type.typcategory of the base type
    typmod: int
    notnull: bool
    default: str | None
    identity: str
    generated: str
    enum_labels: list = field(default_factory=list)
    check_pool: list = field(default_factory=list)


@dataclass
class ForeignKey:
    cols: list
    parent: tuple
    parent_cols: list


@dataclass
class Table:
    schema: str
    name: str
    columns: dict
    uniques: list
    fks: list
    checks: list
    rows: int = 0

    @property
    def key(self):
        return (self.schema, self.name)

    @property
    def fq(self):
        return f"{qi(self.schema)}.{qi(self.name)}"

    @property
    def label(self):
        return label_of(self.key)


def label_of(key) -> str:
    return key[1] if key[0] == "public" else f"{key[0]}.{key[1]}"


@dataclass
class Statement:
    text: str
    offset: int     # where the statement starts in the original script (for error positions)
    kind: str       # query | exec | session | setup | ignored

    @property
    def short(self) -> str:
        text = re.sub(r"^(?:\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/))+", "", self.text, flags=re.S)
        one = " ".join((text or self.text).split())
        return one if len(one) <= 90 else one[:87] + "..."


@dataclass
class Routine:
    name: str
    kind: str              # function | procedure | trigger function | DO block
    language: str
    tables: list           # table keys the body reads or writes
    writes: list           # table keys the body writes
    txn_control: bool = False
    dynamic_sql: bool = False
    source: str = field(default="", repr=False)


@dataclass
class QueryInfo:
    tables: list          # table keys the script touches (plans first, then routine bodies)
    refs: dict            # key -> set of referenced column names
    literals: dict        # (key, col) -> [("eq"|"like", value)]
    conditions: list      # (kind, text)
    is_write: bool
    statements: list = field(default_factory=list)
    routines: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    written: set = field(default_factory=set)
    traced: bool = False
    txn_control: bool = False
    seen_routines: set = field(default_factory=set, repr=False)
    seen_trigger_tables: set = field(default_factory=set, repr=False)
    condition_shapes: set = field(default_factory=set, repr=False)

    def of_kind(self, *kinds) -> list:
        return [s for s in self.statements if s.kind in kinds]

    @property
    def work(self) -> list:
        """Statements that are executed (and timed) on every stress-test iteration."""
        return self.of_kind("query", "exec")


# --------------------------------------------------------------------------
# Scripts: splitting into statements
# --------------------------------------------------------------------------

DOLLAR_RE = re.compile(r"\$([A-Za-z_\x80-￿][\w\x80-￿]*)?\$")
QUERY_WORDS = {"select", "with", "insert", "update", "delete", "merge", "values", "table"}
SESSION_WORDS = {"set", "reset"}
SETUP_WORDS = {"create", "alter", "drop", "comment", "grant", "revoke", "reindex", "vacuum",
               "analyze", "analyse", "cluster", "refresh", "security"}
TXN_WORDS = {"begin", "start", "commit", "end", "rollback", "savepoint", "release", "abort"}
# DDL that is cheap and matters for discovery, so analysis runs it inside a rolled-back transaction
CHEAP_SETUP_RE = re.compile(
    r"^\s*create\s+(?:or\s+replace\s+)?(?:(?:temp|temporary|unlogged)\s+)?"
    r"(?:function|procedure|view|type|domain|trigger|constraint\s+trigger|schema|sequence|table|aggregate|cast)\b",
    re.I,
)


def _segments(text):
    """Yield (kind, start, end) for code, 'single', "double", $dollar$ quoted runs and comments."""
    i, n, code_start = 0, len(text), 0
    while i < n:
        c, kind, end = text[i], None, None
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            kind, end = "comment", n if j < 0 else j
        elif c == "/" and text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            kind, end = "comment", j
        elif c == "'":
            escaped = i > 0 and text[i - 1] in "eE" and (i < 2 or not (text[i - 2].isalnum() or text[i - 2] == "_"))
            j = i + 1
            while j < n:
                if escaped and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            kind, end = "single", min(j + 1, n)
        elif c == '"':
            j = text.find('"', i + 1)
            kind, end = "double", n if j < 0 else j + 1
        elif c == "$" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            m = DOLLAR_RE.match(text, i)
            if m:
                j = text.find(m.group(0), m.end())
                kind, end = "dollar", n if j < 0 else j + len(m.group(0))
        if kind:
            if code_start < i:
                yield "code", code_start, i
            yield kind, i, end
            i = code_start = end
        else:
            i += 1
    if code_start < n:
        yield "code", code_start, n


def strip_sql(text: str, keep_dollar=True) -> str:
    """Remove comments and string literals. Dollar-quoted bodies (functions, DO) are kept unless told not to."""
    parts = []
    for kind, a, b in _segments(text):
        seg = text[a:b]
        if kind in ("code", "double"):
            parts.append(seg)
        elif kind == "single":
            parts.append("''")
        elif kind == "dollar":
            if keep_dollar:
                tag = DOLLAR_RE.match(seg).group(0)
                inner = seg[len(tag):-len(tag)] if seg.endswith(tag) and len(seg) >= 2 * len(tag) else seg[len(tag):]
                parts.append(" " + strip_sql(inner) + " ")
            else:
                parts.append("''")
        else:
            parts.append(" ")
    return "".join(parts)


def split_sql(script: str) -> list:
    """Split a script on top-level semicolons. Returns [(statement_text, offset)]."""
    out, start, atomic, last_word = [], 0, False, ""

    def add(a, b):
        piece = script[a:b]
        # leading comments describe the statement but would also become its title in plans: drop them
        lead = re.match(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", piece, re.S).end()
        text = piece[lead:].rstrip()
        if text and strip_sql(text, keep_dollar=False).strip():
            out.append((text, a + lead))

    for kind, a, b in _segments(script):
        if kind != "code":
            if kind != "comment":
                last_word = ""
            continue
        for m in re.finditer(r"[A-Za-z_]\w*|\S", script[a:b]):
            tok = m.group(0)
            if tok == ";":
                # SQL-standard function bodies (BEGIN ATOMIC ... END) contain semicolons of their own
                if atomic and last_word != "END":
                    continue
                atomic = False
                add(start, a + m.start())
                start = a + m.end()
                last_word = ""
            elif tok[0].isalpha() or tok[0] == "_":
                word = tok.upper()
                if word == "ATOMIC" and last_word == "BEGIN":
                    atomic = True
                last_word = word
            else:
                last_word = ""
    add(start, len(script))
    return out


def first_word(text: str) -> str:
    m = re.match(r"\s*([A-Za-z]+)", strip_sql(text, keep_dollar=False))
    return m.group(1).lower() if m else ""


def parse_script(script: str) -> list:
    statements = []
    for text, offset in split_sql(script or ""):
        word = first_word(text)
        if word in QUERY_WORDS:
            kind = "query"
        elif word in SESSION_WORDS:
            kind = "session"
        elif word in SETUP_WORDS:
            kind = "setup"
        elif word in TXN_WORDS:
            kind = "ignored"
        else:
            kind = "exec"   # CALL, DO, TRUNCATE, LOCK, NOTIFY, ...
        if kind in ("query", "exec") and re.search(r"\$\d+", strip_sql(text, keep_dollar=False)):
            raise PgStressError("statement uses $n parameters - replace them with literal values", offset)
        statements.append(Statement(text, offset, kind))
    if not statements:
        raise PgStressError("empty query")
    if not any(s.kind in ("query", "exec") for s in statements):
        raise PgStressError("the script has nothing to test - add a SELECT, INSERT/UPDATE/DELETE, CALL or DO statement")
    return statements


def pg_error(e, offset=0, prefix_len=0, what="") -> PgStressError:
    """Turn a psycopg error into a PgStressError with the error position mapped onto the user's script."""
    d = getattr(e, "diag", None)
    parts = [(d.message_primary if d and d.message_primary else str(e).strip().splitlines()[0])]
    if d:
        for label, value in (("DETAIL", d.message_detail), ("HINT", d.message_hint), ("CONTEXT", d.context)):
            if value:
                parts.append(f"{label}: {value}")
    pos = d.statement_position if d else None
    position = max(offset, offset + int(pos) - 1 - prefix_len) if pos else None
    return PgStressError((f"{what}: " if what else "") + "\n".join(parts), position)


# --------------------------------------------------------------------------
# Plans (via EXPLAIN VERBOSE / auto_explain, so the planner does the parsing)
# --------------------------------------------------------------------------

COND_KEYS = ("Filter", "Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond", "Join Filter", "TID Cond")
LIST_KEYS = ("Sort Key", "Group Key", "Presorted Key", "Output")
IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
REF_RE = re.compile(rf"({IDENT})\.({IDENT})")
STRING_RE = re.compile(r"'(?:[^']|'')*'")
LITERAL_TAIL_RE = re.compile(
    r"\)*(?:::[a-z_ ]+?(?:\[\])?)?\)*\s*(=|<>|~~\*?|!~~\*?)\s*(ANY\s*\(\s*)?('(?:[^']|'')*'|-?\d+(?:\.\d+)?)"
)
CHECK_IN_RE = re.compile(rf"\(*({IDENT})\)*(?:::[a-z_ ]+?)?\s*=\s*ANY\s*\(+ARRAY\[(.*?)\]")
CHECK_VAL_RE = re.compile(r"'((?:[^']|'')*)'|(-?\d+(?:\.\d+)?)")
EXPLAIN_PREFIX = "EXPLAIN (VERBOSE, FORMAT JSON) "


def unquote(ident: str) -> str:
    if ident.startswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident


def parse_pg_array(text: str) -> list:
    inner = text.strip()[1:-1]
    return [q if q else bare.strip() for q, bare in re.findall(r'"((?:[^"\\]|\\.)*)"|([^,]+)', inner)]


def explain(conn, query, analyze=False, fmt="JSON"):
    # VERBOSE qualifies every column with its table alias, which the JSON analysis relies on;
    # the TEXT plan is for humans, so keep it compact.
    opts = "VERBOSE, FORMAT JSON" if fmt == "JSON" else "FORMAT TEXT"
    if analyze:
        opts = "ANALYZE, BUFFERS, " + opts
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN ({opts}) {query}")
        rows = cur.fetchall()
    if fmt == "TEXT":
        return "\n".join(r[0] for r in rows)
    doc = rows[0][0]
    if isinstance(doc, str):
        doc = json.loads(doc)
    return doc[0]


def iter_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from iter_nodes(child)


def resolve_root(conn, schema, rel):
    """Map a partition back to its partitioned root so we seed the parent."""
    fq = f"{qi(schema)}.{qi(rel)}"
    with conn.cursor() as cur:
        cur.execute(
            """SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.oid = coalesce(pg_partition_root(%s::regclass), %s::regclass)""",
            (fq, fq),
        )
        return tuple(cur.fetchone())


NUMBER_RE = re.compile(r"(?<![\w.$])-?\d+(?:\.\d+)?(?![\w.])")


def literal_values(texts) -> set:
    """Every string and number literal written in the given SQL sources (dollar-quoted bodies included)."""
    found = set()
    for text in texts:
        for kind, a, b in _segments(text):
            if kind == "single":
                found.add(text[a + 1:b - 1].replace("''", "'"))
            elif kind == "dollar":
                tag = DOLLAR_RE.match(text, a).group(0)
                found |= literal_values([text[a + len(tag):max(a + len(tag), b - len(tag))]])
        found.update(NUMBER_RE.findall(strip_sql(text, keep_dollar=False)))
    return found


def collect_literals(text, alias_map, out, known=None):
    for m in REF_RE.finditer(text):
        key = alias_map.get(unquote(m.group(1)))
        tail = LITERAL_TAIL_RE.match(text, m.end()) if key else None
        if not tail:
            continue
        op, any_, lit = tail.groups()
        if op.startswith("<>") or op.startswith("!"):
            continue
        if lit.startswith("'"):
            raw = lit[1:-1].replace("''", "'")
            values = parse_pg_array(raw) if any_ and raw.startswith("{") else [raw]
        else:
            values = [lit]
        kind = "like" if "~~" in op else "eq"
        if known is not None:
            # nested plans carry the runtime values of routine variables; keep only literals from the source
            values = [v for v in values if v in known]
            if not values:
                continue
        bucket = out.setdefault((key, unquote(m.group(2))), [])
        for v in values:
            if (kind, v) not in bucket:
                bucket.append((kind, v))


def add_table(info: QueryInfo, key):
    if key not in info.tables:
        info.tables.append(key)
    info.refs.setdefault(key, set())


def condition_shape(text: str) -> str:
    """A condition with its literals blanked, so per-call variants of one routine query count once."""
    return NUMBER_RE.sub("?", STRING_RE.sub("?", text))


def absorb_plan(conn, plan, info: QueryInfo, known_literals=None):
    """Fold one plan tree (top-level or nested inside a routine) into the analysis."""
    alias_map = {}
    for node in iter_nodes(plan):
        rel = node.get("Relation Name")
        if not rel:
            continue
        key = resolve_root(conn, node.get("Schema", "public"), rel)
        alias_map[node.get("Alias", rel)] = key
        add_table(info, key)
        if node["Node Type"] == "ModifyTable":
            info.is_write = True
            info.written.add(key)

    for node in iter_nodes(plan):
        texts = [(k, node[k]) for k in COND_KEYS if node.get(k)]
        texts += [(k, ", ".join(node[k])) for k in LIST_KEYS if node.get(k)]
        for kind, text in texts:
            for a, c in REF_RE.findall(STRING_RE.sub("''", text)):
                key = alias_map.get(unquote(a))
                if key:
                    info.refs[key].add(unquote(c))
            if kind in COND_KEYS:
                shape = (kind, condition_shape(text))
                if shape not in info.condition_shapes:
                    info.condition_shapes.add(shape)
                    info.conditions.append((kind, text))
                collect_literals(text, alias_map, info.literals, known_literals)


# --------------------------------------------------------------------------
# Routines: functions, procedures, trigger functions and DO blocks
# --------------------------------------------------------------------------

NAME = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
QNAME = rf"{NAME}(?:\s*\.\s*{NAME})?"
REL_REF_RE = re.compile(rf"\b(?:from|join|into|update|table|using|only)\s+({QNAME})", re.I)
WRITE_REF_RE = re.compile(
    rf"\b(?:insert\s+into|update|delete\s+from|merge\s+into|truncate)\s+(?:only\s+|table\s+)?({QNAME})", re.I)
CALL_REF_RE = re.compile(rf"({QNAME})\s*\(")
TXN_CONTROL_RE = re.compile(r"(?:^|[;\s])(?:commit|rollback)\s*(?:and\s+(?:no\s+)?chain\s*)?;", re.I)
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def norm_qname(name: str) -> str:
    return re.sub(r"\s*\.\s*", ".", name)


def split_qname(name: str) -> list:
    return [unquote(p) if p.startswith('"') else p.lower() for p in re.findall(NAME, name)]


def resolve_tables(conn, names) -> dict:
    """name -> [table keys]; views expand to their base tables, partitions to their root."""
    if not names:
        return {}
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT r, c.oid, n.nspname, c.relname, c.relkind
               FROM unnest(%s::text[]) r
               JOIN pg_class c ON c.oid = to_regclass(r)
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname <> ALL(%s)""",
            (sorted(names), list(SYSTEM_SCHEMAS)),
        )
        rows = cur.fetchall()
        for name, oid, schema, rel, relkind in rows:
            if relkind in ("r", "p"):
                out[name] = [resolve_root(conn, schema, rel)]
            elif relkind == "v":
                cur.execute(
                    """WITH RECURSIVE v(oid) AS (
                           SELECT %s::oid
                           UNION
                           SELECT d.refobjid FROM v
                           JOIN pg_rewrite r ON r.ev_class = v.oid
                           JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
                                           AND d.refclassid = 'pg_class'::regclass AND d.refobjid <> v.oid)
                       SELECT n.nspname, c.relname FROM v JOIN pg_class c ON c.oid = v.oid
                       JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p')""",
                    (oid,),
                )
                out[name] = [resolve_root(conn, s, r) for s, r in cur.fetchall()]
    return out


def static_scan(conn, text):
    """Tables read/written and routines called by a statement or routine body, found from its text."""
    body = strip_sql(text)
    reads = {norm_qname(m.group(1)) for m in REL_REF_RE.finditer(body)}
    writes = {norm_qname(m.group(1)) for m in WRITE_REF_RE.finditer(body)}
    calls = {norm_qname(m.group(1)) for m in CALL_REF_RE.finditer(body)}
    resolved = resolve_tables(conn, reads | writes)
    tables, written = [], []
    for name in sorted(reads | writes):
        for key in resolved.get(name, []):
            if key not in tables:
                tables.append(key)
            if name in writes and key not in written:
                written.append(key)
    return tables, written, calls


def lookup_routines(conn, names) -> list:
    """OIDs of user-defined SQL/PL routines matching the called names (extension and C functions are skipped)."""
    plain, qualified = set(), set()
    for name in names:
        parts = split_qname(name)
        if len(parts) == 1:
            plain.add(parts[0])
        elif len(parts) == 2:
            qualified.add(".".join(parts))
    if not plain and not qualified:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.oid FROM pg_proc p
               JOIN pg_namespace n ON n.oid = p.pronamespace
               JOIN pg_language l ON l.oid = p.prolang
               WHERE p.prokind IN ('f', 'p') AND l.lanname NOT IN ('internal', 'c')
                 AND n.nspname <> ALL(%s)
                 AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                                 AND d.objid = p.oid AND d.deptype = 'e')
                 AND (p.proname = ANY(%s) OR n.nspname || '.' || p.proname = ANY(%s))""",
            (list(SYSTEM_SCHEMAS), sorted(plain), sorted(qualified)),
        )
        return [r[0] for r in cur.fetchall()]


def trigger_routines(conn, keys) -> list:
    if not keys:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT tgfoid FROM pg_trigger WHERE tgrelid = ANY(%s::regclass[]) AND NOT tgisinternal",
            ([f"{qi(s)}.{qi(r)}" for s, r in keys],),
        )
        return [r[0] for r in cur.fetchall()]


def load_routine(conn, oid):
    body_sql = ("CASE WHEN p.prosqlbody IS NULL THEN p.prosrc ELSE pg_get_functiondef(p.oid) END"
                if conn.info.server_version >= 140000 else "p.prosrc")
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT p.oid::regprocedure::text, p.prokind, l.lanname, {body_sql},
                       p.prorettype = 'trigger'::regtype
                FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang WHERE p.oid = %s""",
            (oid,),
        )
        name, prokind, language, body, is_trigger = cur.fetchone()
        # SQL-standard bodies record real dependencies
        cur.execute(
            """SELECT n.nspname, c.relname FROM pg_depend d
               JOIN pg_class c ON c.oid = d.refobjid JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE d.classid = 'pg_proc'::regclass AND d.objid = %s
                 AND d.refclassid = 'pg_class'::regclass AND c.relkind IN ('r', 'p')""",
            (oid,),
        )
        deps = [resolve_root(conn, s, r) for s, r in cur.fetchall()]
    tables, writes, calls = static_scan(conn, body or "")
    tables += [k for k in dict.fromkeys(deps) if k not in tables]
    kind = "procedure" if prokind == "p" else "trigger function" if is_trigger else "function"
    stripped = strip_sql(body or "")
    routine = Routine(
        name, kind, language, tables, writes,
        txn_control=prokind == "p" and bool(TXN_CONTROL_RE.search(stripped)),
        dynamic_sql=language == "plpgsql" and bool(re.search(r"\bexecute\b", stripped, re.I)),
        source=body or "",
    )
    return routine, calls


def absorb_routine(info: QueryInfo, routine: Routine):
    info.routines.append(routine)
    for key in routine.tables:
        add_table(info, key)
    for key in routine.writes:
        info.written.add(key)
        info.is_write = True
    info.txn_control = info.txn_control or routine.txn_control


def discover_routines(conn, info: QueryInfo, names=()):
    """Follow called functions/procedures and the triggers of written tables, to any depth."""
    pending = set(names)
    while True:
        new = [o for o in lookup_routines(conn, pending) if o not in info.seen_routines]
        fresh_written = [k for k in info.written if k not in info.seen_trigger_tables]
        info.seen_trigger_tables.update(fresh_written)
        new += [o for o in trigger_routines(conn, fresh_written) if o not in info.seen_routines and o not in new]
        if not new:
            return
        pending = set()
        for oid in new:
            info.seen_routines.add(oid)
            routine, calls = load_routine(conn, oid)
            absorb_routine(info, routine)
            pending |= calls


def scan_statements(conn, info: QueryInfo) -> set:
    """Static pass over the script itself. Returns the routine names it calls."""
    calls = set()
    for s in info.work:
        tables, writes, found = static_scan(conn, s.text)
        calls |= found
        if s.kind != "exec":
            continue  # SELECT/INSERT/... tables come from their plans
        word = first_word(s.text)
        if word == "do":
            body = strip_sql(s.text)
            lang = re.search(r"\blanguage\s+(\w+)", strip_sql(s.text, keep_dollar=False), re.I)
            absorb_routine(info, Routine(
                f"DO block at char {s.offset + 1}", "DO block", lang.group(1).lower() if lang else "plpgsql",
                tables, writes, txn_control=bool(TXN_CONTROL_RE.search(body)),
                dynamic_sql=bool(re.search(r"\bexecute\b", body, re.I)), source=s.text))
        else:
            for key in tables:
                add_table(info, key)
            for key in writes:
                info.written.add(key)
                info.is_write = True
    return calls


# --------------------------------------------------------------------------
# auto_explain tracing: plans for statements nested inside routines and triggers
# --------------------------------------------------------------------------

AUTO_EXPLAIN_RE = re.compile(r"^duration: ([\d.]+) ms\s+plan:\s*(.*)$", re.S)
AUTO_EXPLAIN_HELP = ("Plans inside functions/procedures need the auto_explain module: connect as a superuser, "
                     "or add auto_explain to session_preload_libraries.")


def load_auto_explain(cur) -> bool:
    cur.execute("SAVEPOINT pgs_ae")
    try:
        cur.execute("LOAD 'auto_explain'")
        cur.execute("RELEASE SAVEPOINT pgs_ae")
        return True
    except psycopg.Error:
        cur.execute("ROLLBACK TO SAVEPOINT pgs_ae")
        cur.execute("SELECT current_setting('shared_preload_libraries') || ',' || "
                    "current_setting('session_preload_libraries')")
        return "auto_explain" in cur.fetchone()[0]


def traced_run(conn, statements, analyze: bool, fmt: str):
    """Execute statements (inside the caller's transaction) with auto_explain sending every plan,
    nested ones included, back as a NOTICE. Returns (results, error) where results is
    [(statement, elapsed_ms, [(duration_ms, plan)])], or (None, None) if auto_explain is unavailable."""
    cur = conn.cursor()
    if not load_auto_explain(cur):
        return None, None
    flag = "on" if analyze else "off"
    settings = {"log_min_duration": "0", "log_nested_statements": "on", "log_level": "notice",
                "log_format": fmt, "log_verbose": "on" if fmt == "json" else "off", "log_analyze": flag,
                "log_buffers": flag, "log_timing": flag, "log_triggers": flag}
    for k, v in settings.items():
        cur.execute(f"SET LOCAL auto_explain.{k} = {v}")
    cur.execute("SET LOCAL client_min_messages = notice")

    captured = []
    handler = lambda diag: captured.append(diag.message_primary or "")  # noqa: E731
    conn.add_notice_handler(handler)
    results = []
    try:
        for s in statements:
            start = len(captured)
            t0 = time.perf_counter()
            try:
                cur.execute(s.text)
                if cur.description:
                    cur.fetchall()
            except psycopg.Error as e:
                return results, pg_error(e, s.offset)
            elapsed = (time.perf_counter() - t0) * 1000
            plans = []
            for msg in captured[start:]:
                m = AUTO_EXPLAIN_RE.match(msg)
                if m:
                    body = m.group(2).strip()
                    plans.append((float(m.group(1)), json.loads(body[body.find("{"):]) if fmt == "json" else body))
            results.append((s, elapsed, plans))
    finally:
        conn.remove_notice_handler(handler)
    return results, None


# --------------------------------------------------------------------------
# Script analysis
# --------------------------------------------------------------------------

def analyze_script(conn, script: str, trace=True):
    """Analyze a script of one or more statements. Everything runs in a transaction that is rolled
    back, including the cheap setup DDL and (when trace=True) one traced execution of the script.
    Returns (QueryInfo, tables, seed order)."""
    info = QueryInfo([], {}, {}, [], False, statements=parse_script(script))
    conn.rollback()
    cur = conn.cursor()
    try:
        cur.execute("SET LOCAL statement_timeout = '30s'")
        # setup and SET statements run in script order: `SET search_path` usually comes first
        for s in info.of_kind("setup", "session"):
            if s.kind == "setup" and not CHEAP_SETUP_RE.match(strip_sql(s.text, keep_dollar=False)):
                info.notes.append(f"Not run during analysis, applied before the test: {s.short}")
                continue
            try:
                cur.execute(s.text)
            except psycopg.Error as e:
                raise pg_error(e, s.offset, what="setup statement failed" if s.kind == "setup" else "") from e
        if info.of_kind("ignored"):
            info.notes.append("BEGIN/COMMIT/ROLLBACK statements are ignored: pgstress wraps every run itself.")

        for s in info.of_kind("query"):
            try:
                plan = explain(conn, s.text)
            except psycopg.Error as e:
                raise pg_error(e, s.offset, len(EXPLAIN_PREFIX)) from e
            absorb_plan(conn, plan["Plan"], info)
        discover_routines(conn, info, scan_statements(conn, info))

        if trace and info.txn_control:
            info.notes.append("Not traced: a procedure or DO block commits/rolls back, so it can't run "
                              "inside the rolled-back analysis transaction. Tables were found from its source.")
        elif trace:
            cur.execute("SAVEPOINT pgs_trace")
            results, err = traced_run(conn, info.work, analyze=False, fmt="json")
            cur.execute("ROLLBACK TO SAVEPOINT pgs_trace")
            if results is None:
                if info.routines:
                    info.notes.append(AUTO_EXPLAIN_HELP + " Tables inside routines were found from their source instead.")
            else:
                info.traced = True
                if err:
                    info.notes.append(f"Trace stopped early (the script raised an error on the current data): {err}")
                known = literal_values([s.text for s in info.statements] + [r.source for r in info.routines])
                for _, _, plans in results:
                    for _, plan in plans:
                        absorb_plan(conn, plan["Plan"], info, known)
                discover_routines(conn, info)

        dynamic = [r.name for r in info.routines if r.dynamic_sql]
        if dynamic:
            info.notes.append("Dynamic SQL (EXECUTE) in " + ", ".join(dynamic) +
                              ": tables it builds at runtime are only found when tracing reaches them.")
        tables = collect_tables(conn, info.tables)
        return info, tables, seed_order(tables)
    finally:
        conn.rollback()


DUPLICATE_STATES = {"42P07", "42710", "42723", "42P06", "42P04", "42701"}


def apply_setup(conn, info: QueryInfo, say=print):
    """Run the script's DDL for real, once (committed). 'Already exists' errors are skipped."""
    prev = conn.autocommit
    conn.rollback()
    conn.autocommit = True
    try:
        for s in info.of_kind("setup", "session"):
            try:
                conn.execute(s.text)
                if s.kind == "setup":
                    say(f"applied: {s.short}")
            except psycopg.Error as e:
                if e.sqlstate in DUPLICATE_STATES:
                    say(f"skipped, already exists: {s.short}")
                    continue
                raise pg_error(e, s.offset, what="setup statement failed") from e
    finally:
        conn.autocommit = prev


def profile_script(conn, info: QueryInfo):
    """Execute the script once (rolled back) and capture an EXPLAIN ANALYZE plan for every
    statement, including statements inside functions, procedures and triggers.
    Returns (sections, hints, notes); a section is {statement, ms, plans: [{ms, plan}]}."""
    if info.txn_control:
        return [], [], ["Plans not captured: the script commits inside a procedure/DO block, "
                        "so it can't run in a rolled-back transaction. Timings below are still real."]
    sections, hints, notes = [], [], []
    conn.autocommit = False
    try:
        cur = conn.cursor()
        for s in info.of_kind("session"):
            cur.execute(s.text)
        cur.execute("SAVEPOINT pgs_profile")
        text_run, err = traced_run(conn, info.work, analyze=True, fmt="text")
        cur.execute("ROLLBACK TO SAVEPOINT pgs_profile")
        if text_run is not None:
            for s, ms, plans in text_run:
                # a per-row trigger or a loop logs the same statement many times: keep one plan, sum the time
                grouped = {}
                for d, p in plans:
                    if p.startswith("Query Text: \n"):  # BEGIN ATOMIC bodies carry no query text
                        p = "Query Text: (statement inside a SQL-standard function body)" + p[len("Query Text: "):]
                    entry = grouped.setdefault(p.split("\n", 1)[0], {"ms": 0.0, "calls": 0, "plan": p})
                    entry["ms"] += d
                    entry["calls"] += 1
                sections.append({"statement": s.short, "ms": ms, "plans": list(grouped.values())})
            if err:
                notes.append(f"The script failed during profiling: {err}")
            json_run, _ = traced_run(conn, info.work, analyze=True, fmt="json")
            cur.execute("ROLLBACK TO SAVEPOINT pgs_profile")
            for _, _, plans in json_run or []:
                for _, plan in plans:
                    hints += plan_hints(plan["Plan"])
            return sections, merge_hints(hints), notes

        # no auto_explain: EXPLAIN ANALYZE the plain statements, time the rest
        if any(s.kind == "exec" for s in info.work) or info.routines:
            notes.append(AUTO_EXPLAIN_HELP)
        for s in info.work:
            try:
                if s.kind == "query":
                    cur.execute("SAVEPOINT pgs_stmt")
                    text = explain(conn, s.text, analyze=True, fmt="TEXT")
                    cur.execute("ROLLBACK TO SAVEPOINT pgs_stmt")
                    plan = explain(conn, s.text, analyze=True)
                    hints += plan_hints(plan["Plan"])
                    ms = plan.get("Execution Time", 0.0)
                    sections.append({"statement": s.short, "ms": ms, "plans": [{"ms": ms, "calls": 1, "plan": text}]})
                else:
                    t0 = time.perf_counter()
                    cur.execute(s.text)
                    if cur.description:
                        cur.fetchall()
                    sections.append({"statement": s.short, "ms": (time.perf_counter() - t0) * 1000, "plans": []})
            except psycopg.Error as e:
                notes.append(f"The script failed during profiling: {pg_error(e, s.offset)}")
                break
        return sections, merge_hints(hints), notes
    finally:
        conn.rollback()
        conn.autocommit = True


def apply_session(conn, info: QueryInfo):
    """Run the script's SET statements on a connection (e.g. search_path, which triggers fired
    while seeding rely on too). They are committed so they last for the whole session."""
    for s in info.of_kind("session"):
        try:
            conn.execute(s.text)
        except psycopg.Error as e:
            conn.rollback()
            raise pg_error(e, s.offset) from e
    if not conn.autocommit:
        conn.commit()


def run_script_once(conn, info: QueryInfo, rollback: bool):
    """One execution of the work statements on an existing connection (used for warm-up)."""
    with conn.cursor() as cur:
        for s in info.work:
            cur.execute(s.text)
            if cur.description:
                cur.fetchall()
    if rollback:
        conn.rollback()


# --------------------------------------------------------------------------
# Table introspection
# --------------------------------------------------------------------------

def load_table(conn, key) -> Table:
    fq = f"{qi(key[0])}.{qi(key[1])}"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), bt.typname, bt.typtype, bt.typcategory,
                   CASE WHEN t.typtype = 'd' THEN t.typtypmod ELSE a.atttypmod END,
                   a.attnotnull OR (t.typtype = 'd' AND t.typnotnull),
                   pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated, bt.oid
            FROM pg_attribute a
            JOIN pg_type t ON t.oid = a.atttypid
            JOIN pg_type bt ON bt.oid = CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE t.oid END
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum""",
            (fq,),
        )
        columns = {}
        for name, sqltype, base, typtype, cat, typmod, notnull, default, ident, gen, base_oid in cur.fetchall():
            col = Column(name, sqltype, base, typtype, cat, typmod, notnull, default, ident, gen)
            if typtype == "e":
                cur.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = %s ORDER BY enumsortorder", (base_oid,))
                col.enum_labels = [r[0] for r in cur.fetchall()]
            columns[name] = col

        cur.execute(
            """
            SELECT ARRAY(SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n ORDER BY k.o)
            FROM pg_index i
            WHERE i.indrelid = %s::regclass AND i.indisunique AND i.indexprs IS NULL""",
            (fq,),
        )
        uniques = [tuple(r[0]) for r in cur.fetchall() if r[0]]

        cur.execute(
            """
            SELECT c.contype,
                   ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.o),
                   fn.nspname, fc.relname,
                   ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.o),
                   pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            LEFT JOIN pg_class fc ON fc.oid = c.confrelid
            LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
            WHERE c.conrelid = %s::regclass AND c.contype IN ('f', 'c')""",
            (fq,),
        )
        fks, checks = [], []
        for contype, cols, pschema, prel, pcols, definition in cur.fetchall():
            if contype == "f":
                fks.append(ForeignKey(list(cols), resolve_root(conn, pschema, prel), list(pcols)))
            else:
                checks.append(definition)
                for m in CHECK_IN_RE.finditer(definition):
                    col = columns.get(unquote(m.group(1)))
                    if col:
                        col.check_pool = [q.replace("''", "'") if q else n for q, n in CHECK_VAL_RE.findall(m.group(2))]

        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass", (fq,))
        estimate = cur.fetchone()[0]
        if estimate < 0 or estimate < 1_000_000:
            cur.execute(f"SELECT count(*) FROM {fq}")
            estimate = cur.fetchone()[0]
    return Table(key[0], key[1], columns, uniques, fks, checks, estimate)


def collect_tables(conn, roots) -> dict:
    """Load the query's tables plus every FK parent they (transitively) need."""
    tables, stack = {}, list(roots)
    while stack:
        key = stack.pop()
        if key in tables:
            continue
        tables[key] = load_table(conn, key)
        stack.extend(fk.parent for fk in tables[key].fks if fk.parent not in tables)
    return tables


def seed_order(tables) -> list:
    deps = {k: {fk.parent for fk in t.fks if fk.parent != k and fk.parent in tables} for k, t in tables.items()}
    order = []
    while deps:
        ready = sorted(k for k, d in deps.items() if not d)
        if not ready:  # FK cycle: break it at the table with the fewest unmet deps
            ready = [min(deps, key=lambda k: (len(deps[k]), k))]
        for k in ready:
            order.append(k)
            del deps[k]
        for d in deps.values():
            d.difference_update(ready)
    return order


# --------------------------------------------------------------------------
# Value generators
# --------------------------------------------------------------------------

INT_LIMITS = {"int2": 32_767, "int4": 2_147_483_647, "int8": 9_223_372_036_854_775_807}
RANDOM_NUMBER_TOP = 10_000
ORDERABLE = set(INT_LIMITS) | {"numeric", "float4", "float8", "date", "timestamp", "timestamptz"}


@dataclass
class SeedOptions:
    match_rate: float = 0.1
    null_rate: float = 0.0
    batch: int = 50_000
    fk_sample: int = 100_000
    span_days: int = 365
    overrides: dict = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])


def pool_expr(values) -> str:
    return f"(ARRAY[{', '.join(ql(v) for v in values)}])[1 + floor(random() * {len(values)})::int]"


def numeric_bounds(typmod):
    if typmod < 4:
        return None, 2
    precision, scale = ((typmod - 4) >> 16) & 0xFFFF, (typmod - 4) & 0xFFFF
    return precision, scale


def base_generator(col: Column, unique: bool, anchor, opts: SeedOptions):
    """SQL expression for a column with no FK / override. `g` is the row's sequence number."""
    b, name = col.base, col.name.lower()
    span = opts.span_days
    if col.typtype == "e":
        return pool_expr(col.enum_labels), f"enum pool ({len(col.enum_labels)} labels)"
    if col.check_pool and not unique:
        return pool_expr(col.check_pool), f"CHECK pool {col.check_pool[:5]}"
    if b in INT_LIMITS:
        if unique:
            return f"(g + {int(anchor or 0)})", "unique sequence"
        # kept modest: quantities and prices get multiplied and summed, and must fit typed results
        top = min(RANDOM_NUMBER_TOP, INT_LIMITS[b])
        return f"(1 + floor(random() * {top}))", f"random 1..{fmt_int(top)}"
    if b == "numeric":
        precision, scale = numeric_bounds(col.typmod)
        if unique:
            return f"(g + {anchor or 0})", "unique sequence"
        top = RANDOM_NUMBER_TOP if precision is None else min(RANDOM_NUMBER_TOP, 10 ** (precision - scale) - 1)
        # never exactly 0, so the common CHECK (amount > 0) holds
        step = f"1e-{scale}" if scale else "1"
        return f"(round((random() * {top})::numeric, {scale}) + {step})", f"random {step}..{fmt_int(top)}"
    if b in ("float4", "float8"):
        return ("(g + {})".format(anchor or 0), "unique sequence") if unique else ("(random() * 1000)", "random 0..1000")
    if b == "bool":
        return "(random() < 0.5)", "random true/false"
    if b == "uuid":
        return "md5(random()::text || g::text)::uuid", "random uuid"
    if b == "date":
        if unique:
            return f"(DATE {ql(anchor or '2000-01-01')} + g::int)", "unique sequence of days"
        return f"(current_date - floor(random() * {span})::int)", f"random date in last {span} days"
    if b in ("timestamp", "timestamptz"):
        if unique:
            return f"(TIMESTAMPTZ {ql(anchor or '2000-01-01')} + g * interval '1 second')", "unique sequence of seconds"
        return f"(now() - random() * interval '{span} days')", f"random time in last {span} days"
    if b in ("time", "timetz"):
        return "(time '00:00' + random() * interval '1 day')", "random time of day"
    if b == "interval":
        return "(random() * interval '30 days')", "random interval < 30 days"
    if b in ("json", "jsonb"):
        return "jsonb_build_object('n', g, 'tag', md5(random()::text))", "small json object"
    if b == "bytea":
        return "decode(md5(random()::text), 'hex')", "16 random bytes"
    if b in ("inet", "cidr"):
        return "('10.' || (g / 65536 % 256) || '.' || (g / 256 % 256) || '.' || (g % 256))", "10.x.x.x address"
    if b == "money":
        return "(random() * 1000)::numeric(12,2)", "random 0..1000"
    if b == "tsvector":
        return "to_tsvector('simple', md5(random()::text) || ' ' || md5(random()::text))", "random tsvector"
    if b == "point":
        return "point(random() * 100, random() * 100)", "random point"
    if col.category == "A":
        return "'{}'", "empty array"
    if col.category == "S":  # text, varchar, char, citext, name ...
        if "email" in name:
            if unique:
                return f"('user_{opts.run_id}_' || g || '@example.com')", "unique email"
            return "('user' || (1 + floor(random() * 1000000))::int || '@example.com')", "random email"
        if unique:
            prefix = f"{col.name[:12]}_{opts.run_id}_"
            limit = col.typmod - 4 if col.typmod > 4 else None
            if limit and limit < len(prefix) + 10:
                return "g::text", "unique number as text"
            return f"({ql(prefix)} || g)", "unique text"
        return "md5(random()::text)", "random text"
    return None, f"unsupported type {col.sqltype}"


def like_expr(pattern: str) -> str:
    parts = [ql(p) for p in pattern.replace("_", "x").split("%")]
    return " || left(md5(random()::text), 6) || ".join(parts)


def plan_columns(table: Table, qinfo: QueryInfo, opts: SeedOptions, fk_exprs: dict, unique_cols: set, anchors: dict):
    """Decide how every column gets its value. Returns [(column, sql_expr | None, description)]."""
    refs = qinfo.refs.get(table.key, set())
    plan = []
    for col in table.columns.values():
        override = opts.overrides.get((table.label, col.name)) or opts.overrides.get((table.name, col.name))
        literals = qinfo.literals.get((table.key, col.name), [])
        if override:
            plan.append((col, f"({override})::{col.sqltype}", f"override: {override}"))
            continue
        if col.generated:
            plan.append((col, None, "generated column"))
            continue
        if col.identity:
            plan.append((col, None, "identity"))
            continue
        if col.name in fk_exprs:
            expr, desc = fk_exprs[col.name]
            plan.append((col, expr, desc))
            continue
        if col.default and col.default.startswith("nextval("):
            plan.append((col, None, "serial (DEFAULT)"))
            continue
        if col.default and col.name not in refs and not literals:
            plan.append((col, None, f"DEFAULT {col.default}"))
            continue

        unique = col.name in unique_cols
        expr, desc = base_generator(col, unique, anchors.get(col.name), opts)
        if expr is None:
            if col.default:
                plan.append((col, None, f"DEFAULT {col.default} ({desc})"))
            elif not col.notnull:
                plan.append((col, f"NULL::{col.sqltype}", f"NULL ({desc})"))
            else:
                plan.append((col, None, f"!! {desc} - NOT NULL without default, use --set {table.label}.{col.name}=EXPR"))
            continue
        if literals and not unique and opts.match_rate > 0:
            picks = [ql(v) if kind == "eq" else like_expr(v) for kind, v in literals]
            lit_sql = picks[0] if len(picks) == 1 else f"(ARRAY[{', '.join(picks)}])[1 + floor(random() * {len(picks)})::int]"
            expr = f"CASE WHEN random() < {opts.match_rate} THEN ({lit_sql})::{col.sqltype} ELSE ({expr})::{col.sqltype} END"
            shown = ", ".join(("LIKE " if k == "like" else "") + v for k, v in literals[:4])
            desc += f" + query literal(s) [{shown}] at {opts.match_rate:.0%}"
        if opts.null_rate > 0 and not col.notnull and not unique:
            expr = f"CASE WHEN random() < {opts.null_rate} THEN NULL ELSE ({expr})::{col.sqltype} END"
            desc += f", {opts.null_rate:.0%} NULL"
        plan.append((col, f"({expr})::{col.sqltype}", desc))
    return plan


def unique_columns(table: Table) -> set:
    fk_cols = {c for fk in table.fks for c in fk.cols}
    return {c for u in table.uniques for c in u if c not in fk_cols}


def fk_row_pickers(table: Table, counts: dict) -> dict:
    """SQL that picks a row number in each FK sample table. FK groups that together form a
    unique key are enumerated (mixed radix) instead of drawn at random, so pairs don't collide."""
    pickers = {i: f"(1 + floor(random() * {cnt}))::bigint" for i, cnt in counts.items()}
    enumerated = set()
    for u in table.uniques:
        groups = [i for i, fk in enumerate(table.fks) if i in counts and set(fk.cols) <= set(u)]
        covered = {c for i in groups for c in table.fks[i].cols}
        if not groups or covered != set(u) or enumerated & set(groups):
            continue
        radix = 1
        for i in groups:
            pickers[i] = f"(((g - 1) / {radix}) % {counts[i]} + 1)"
            radix *= counts[i]
        enumerated.update(groups)
    return pickers


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def describe_fks(table: Table, tables: dict) -> dict:
    out = {}
    for fk in table.fks:
        for c, pc in zip(fk.cols, fk.parent_cols):
            out[c] = ("<fk>", f"FK -> {label_of(fk.parent)}.{pc} (sampled from existing rows)")
    return out


def seed_table(conn, table: Table, n_rows: int, tables: dict, qinfo: QueryInfo, opts: SeedOptions,
               on_progress=None, stop=None):
    """Insert n_rows into table. on_progress(done, total, rows_per_s) replaces console output;
    setting the `stop` event ends seeding after the current batch."""
    cur = conn.cursor()
    fk_exprs, joins, counts = {}, [], {}
    for i, fk in enumerate(table.fks):
        if all(opts.overrides.get((table.label, c)) or opts.overrides.get((table.name, c)) for c in fk.cols):
            continue
        parent = tables[fk.parent]
        temp = f"_pgs_fk_{i}"
        picks = ", ".join(f"{qi(pc)} AS c{j}" for j, pc in enumerate(fk.parent_cols))
        not_null = " AND ".join(f"{qi(pc)} IS NOT NULL" for pc in fk.parent_cols)
        cur.execute(f"DROP TABLE IF EXISTS pg_temp.{temp}")
        cur.execute(
            f"CREATE TEMP TABLE {temp} AS SELECT row_number() OVER () AS rn, s.* FROM "
            f"(SELECT {picks} FROM {parent.fq} WHERE {not_null} ORDER BY random() LIMIT {opts.fk_sample}) s"
        )
        cnt = cur.rowcount
        if cnt <= 0:
            if all(not table.columns[c].notnull for c in fk.cols):
                for c in fk.cols:
                    fk_exprs[c] = (f"NULL::{table.columns[c].sqltype}", "NULL (parent empty)")
                continue
            raise PgStressError(
                f"{table.label}({', '.join(fk.cols)}) references {parent.label}, which has no rows. "
                f"Seed {parent.label} too, or use --set {table.label}.{fk.cols[0]}=EXPR"
            )
        cur.execute(f"CREATE INDEX ON pg_temp.{temp} (rn)")
        counts[i] = cnt
        for j, c in enumerate(fk.cols):
            fk_exprs[c] = (f"f{i}.c{j}", f"FK -> {parent.label}")
        joins.append(f"LEFT JOIN pg_temp.{temp} f{i} ON f{i}.rn = s.r{i}")

    uniq = unique_columns(table)
    anchors = {}
    for c in uniq:
        if table.columns[c].base in ORDERABLE:
            cur.execute(f"SELECT max({qi(c)})::text FROM {table.fq}")
            anchors[c] = cur.fetchone()[0]
    plan = plan_columns(table, qinfo, opts, fk_exprs, uniq, anchors)
    broken = [d for _, e, d in plan if d.startswith("!!")]
    if broken:
        raise PgStressError(f"{table.label}: " + "; ".join(broken))

    cols = [(c, e) for c, e, _ in plan if e is not None]
    pickers = fk_row_pickers(table, counts)
    r_cols = "".join(f", {pickers[i]} AS r{i}" for i in counts)
    sql = (
        f"INSERT INTO {table.fq} ({', '.join(qi(c.name) for c, _ in cols)}) "
        f"SELECT {', '.join(e for _, e in cols)} "
        f"FROM (SELECT g{r_cols} FROM generate_series(__PGS_START__::bigint, __PGS_END__::bigint) AS g OFFSET 0) s "
        f"{' '.join(joins)} ON CONFLICT DO NOTHING"
    )
    conn.commit()

    done = inserted = 0
    started = time.perf_counter()
    while done < n_rows and not (stop and stop.is_set()):
        k = min(opts.batch, n_rows - done)
        try:
            cur.execute(sql.replace("__PGS_START__", str(done + 1)).replace("__PGS_END__", str(done + k)))
        except psycopg.Error as e:
            conn.rollback()
            raise PgStressError(f"insert into {table.label} failed: {str(e).strip()}") from e
        inserted += max(cur.rowcount, 0)
        conn.commit()
        done += k
        rate = done / max(time.perf_counter() - started, 1e-9)
        if on_progress:
            on_progress(done, n_rows, rate)
            continue
        print(f"\r  {table.label:<28} {fmt_int(done):>13} / {fmt_int(n_rows)}  ({fmt_int(rate)} rows/s)", end="", flush=True)
    cur.execute(f"ANALYZE {table.fq}")
    conn.commit()
    elapsed = time.perf_counter() - started
    note = "" if inserted == n_rows else f"  ({fmt_int(n_rows - inserted)} skipped by unique conflicts)"
    if not on_progress:
        print(f"\r  {table.label:<28} {fmt_int(inserted):>13} rows in {elapsed:.1f}s{note}" + " " * 20)
    table.rows += inserted
    return inserted


def plan_row_counts(tables, order, qinfo, args) -> dict:
    default, per_table = 100_000, {}
    for item in args.rows or []:
        if "=" in item:
            name, n = item.rsplit("=", 1)
            per_table[name.strip()] = parse_count(n)
        else:
            default = parse_count(item)
    counts = {}
    for key in order:
        t = tables[key]
        explicit = per_table.get(t.label, per_table.get(t.name))
        if explicit is not None:
            counts[key] = explicit
        elif key in qinfo.tables:
            counts[key] = default
        else:  # FK parent only: reuse existing rows, or create a smaller lookup set
            counts[key] = 0 if t.rows > 0 else max(default // 10, 1_000)

    if sys.stdin.isatty() and not args.yes and not per_table:
        print("\nRows to insert per table (Enter keeps default, accepts 50k / 2m):")
        for key in order:
            t = tables[key]
            role = "query" if key in qinfo.tables else "FK parent"
            while True:
                answer = input(f"  {t.label:<28} [{role}, has ~{fmt_int(t.rows)}] [{fmt_int(counts[key])}]: ").strip()
                try:
                    if answer:
                        counts[key] = parse_count(answer)
                    break
                except ValueError:
                    print("    enter a number like 250000, 250k or 2m")
    return counts


def confirm(prompt: str, args) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise PgStressError("refusing to write without a terminal - pass --yes")
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------
# Benchmark / stress test
# --------------------------------------------------------------------------

def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def connect(dsn, prepared=False, autocommit=True):
    return psycopg.connect(dsn, autocommit=autocommit, prepare_threshold=0 if prepared else None)


def run_stage(dsn, script, concurrency, duration, iterations, rollback, prepared, stop=None, on_progress=None,
              session_sql=()):
    """One stress stage. `script` is a statement or a list of statements; one execution runs all of
    them. session_sql (SET ...) runs once per connection. on_progress(elapsed_s, executions)
    replaces console output; setting the `stop` event ends the stage early."""
    script = [script] if isinstance(script, str) else list(script)
    conns = [connect(dsn, prepared, autocommit=not rollback) for _ in range(concurrency)]
    for c in conns:
        for s in session_sql:
            c.execute(s)
        if not c.autocommit:
            c.commit()
    latencies, errors, rows_seen = [], [], [0]
    done_count = [0]
    lock = threading.Lock()
    remaining = [iterations or 0]
    start_gate = threading.Barrier(concurrency + 1)
    stop = stop or threading.Event()

    def worker(conn):
        local, local_rows = [], 0
        start_gate.wait()
        deadline = time.perf_counter() + duration
        while not stop.is_set():
            if iterations:
                with lock:
                    if remaining[0] <= 0:
                        break
                    remaining[0] -= 1
            elif time.perf_counter() >= deadline:
                break
            t0 = time.perf_counter()
            try:
                with conn.cursor() as cur:
                    for sql in script:
                        cur.execute(sql)
                        local_rows += len(cur.fetchall()) if cur.description else max(cur.rowcount, 0)
                if rollback:
                    conn.rollback()
                local.append(time.perf_counter() - t0)
                done_count[0] += 1
            except psycopg.Error as e:
                with lock:
                    errors.append(str(e).strip().splitlines()[0])
                try:
                    conn.rollback()
                except psycopg.Error:
                    break
        with lock:
            latencies.extend(local)
            rows_seen[0] += local_rows

    threads = [threading.Thread(target=worker, args=(c,), daemon=True) for c in conns]
    for t in threads:
        t.start()
    start_gate.wait()
    started = time.perf_counter()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.25)
                if on_progress:
                    on_progress(time.perf_counter() - started, done_count[0])
                elif not iterations:
                    left = max(0, duration - (time.perf_counter() - started))
                    print(f"\r    c={concurrency:<4} running... {left:5.1f}s left, {fmt_int(len(latencies))} done", end="", flush=True)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join()
    wall = time.perf_counter() - started
    for c in conns:
        c.close()
    print("\r" + " " * 70 + "\r", end="")

    lat = sorted(latencies)
    n = len(lat)
    return {
        "concurrency": concurrency,
        "executions": n,
        "errors": len(errors),
        "first_error": errors[0] if errors else None,
        "wall_s": wall,
        "qps": n / wall if wall else 0,
        "avg_ms": (sum(lat) / n * 1000) if n else 0,
        "min_ms": lat[0] * 1000 if n else 0,
        "p50_ms": percentile(lat, 50) * 1000,
        "p90_ms": percentile(lat, 90) * 1000,
        "p95_ms": percentile(lat, 95) * 1000,
        "p99_ms": percentile(lat, 99) * 1000,
        "max_ms": lat[-1] * 1000 if n else 0,
        "rows_per_exec": rows_seen[0] / n if n else 0,
    }


def merge_hints(hints) -> list:
    """Collapse hints that differ only in numbers (one per call of a routine) into one line."""
    seen = {}
    for h in hints:
        seen.setdefault(NUMBER_RE.sub("#", h.replace(",", "")), []).append(h)
    return [group[0] if len(group) == 1 else f"{group[0]} (seen {len(group)} times)" for group in seen.values()]


def plan_hints(plan) -> list:
    hints = []
    for n in iter_nodes(plan):
        nt, rel = n["Node Type"], n.get("Relation Name", "?")
        loops = n.get("Actual Loops", 1) or 1
        actual = n.get("Actual Rows", 0) * loops
        removed = n.get("Rows Removed by Filter", 0) * loops
        if nt == "Seq Scan" and n.get("Filter") and removed > 10_000 and removed > 10 * actual:
            hints.append(f"Seq Scan on {rel} reads and discards {fmt_int(removed)} rows for {n['Filter']} - "
                         f"an index on the filtered column(s) may help.")
        # repeated scans only matter when they read a lot in total (a 20-row lookup table is fine)
        if nt == "Seq Scan" and loops > 10 and actual + removed > 50_000:
            hints.append(f"Seq Scan on {rel} runs {fmt_int(loops)} times (inner side of a loop) - index the join key.")
        if nt == "Sort" and n.get("Sort Space Type") == "Disk":
            hints.append(f"Sort spilled {fmt_int(n.get('Sort Space Used', 0))} kB to disk - raise work_mem "
                         f"or add an index matching the ORDER BY.")
        if nt == "Hash" and n.get("Hash Batches", 1) > 1:
            hints.append(f"Hash used {n['Hash Batches']} batches (spilled to disk) - raise work_mem.")
        est = n.get("Plan Rows", 0) * loops
        if actual > 1_000 and est and (actual / est > 100 or est / actual > 100):
            hints.append(f"{nt} on {rel}: planner estimated {fmt_int(est)} rows, got {fmt_int(actual)} - "
                         f"stats are off (ANALYZE, or raise statistics target / extended stats).")
    return hints


def bench_mode(info: QueryInfo, commit_writes=False):
    """(rollback_each_run, note). Scripts that commit inside a procedure can't be rolled back."""
    if info.txn_control:
        return False, "the script commits inside a procedure/DO block, so its changes are kept"
    if info.is_write and not commit_writes:
        return True, "writes rolled back after each run"
    return False, "writes committed" if info.is_write else ""


def print_profile(sections, hints, notes):
    for sec in sections:
        print(f"\n  > {sec['statement']}   ({sec['ms']:.2f} ms)")
        for p in sec["plans"]:
            if p["calls"] > 1:
                print(f"    ({p['calls']} calls, {p['ms']:.2f} ms total)")
            print("    " + p["plan"].replace("\n", "\n    "))
    if hints:
        print("\n  Hints:")
        for h in hints:
            print(f"    * {h}")
    for n in notes:
        print(f"\n  Note: {n}")


def print_stage_table(stages):
    head = f"  {'conc':>5} {'execs':>9} {'errors':>7} {'qps':>9} {'avg':>9} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9} {'rows':>8}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for s in stages:
        print(f"  {s['concurrency']:>5} {fmt_int(s['executions']):>9} {s['errors']:>7} {s['qps']:>9.1f} "
              f"{s['avg_ms']:>7.2f}ms {s['p50_ms']:>7.2f}ms {s['p95_ms']:>7.2f}ms {s['p99_ms']:>7.2f}ms "
              f"{s['max_ms']:>7.1f}ms {s['rows_per_exec']:>8.0f}")
    for s in stages:
        if s["first_error"]:
            print(f"  c={s['concurrency']} first error: {s['first_error']}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_script(args) -> str:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return fh.read()
    return args.query


def parse_overrides(items) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise PgStressError(f"--set expects table.column=SQL_EXPR, got {item!r}")
        target, expr = item.split("=", 1)
        table, col = target.rsplit(".", 1)
        out[(table.strip(), col.strip())] = expr.strip()
    return out


def print_analysis(qinfo, tables, order, opts, show_plan=True):
    print("\nSCRIPT ANALYSIS")
    rollback, mode_note = bench_mode(qinfo)
    print(f"  Type: {'writes data' if qinfo.is_write else 'read-only'}{' - ' + mode_note if mode_note else ''}"
          f"{'  (traced with auto_explain)' if qinfo.traced else ''}")
    print("\n  Statements:")
    labels = {"query": "query", "exec": "run", "session": "per connection", "setup": "setup (once)", "ignored": "ignored"}
    for s in qinfo.statements:
        print(f"    [{labels[s.kind]:<14}] {s.short}")
    if qinfo.routines:
        print("\n  Routines reached (functions, procedures, triggers, DO blocks):")
        for r in qinfo.routines:
            flags = [f for f, on in (("commits", r.txn_control), ("dynamic SQL", r.dynamic_sql)) if on]
            used = ", ".join(label_of(k) + ("*" if k in r.writes else "") for k in r.tables) or "-"
            print(f"    {r.name}  [{r.kind}, {r.language}{', ' + ', '.join(flags) if flags else ''}]  tables: {used}")
        print("    (* = written)")
    print("\n  Tables used:")
    for key in qinfo.tables:
        cols = ", ".join(sorted(qinfo.refs.get(key, ()))) or "-"
        print(f"    {label_of(key):<28} ~{fmt_int(tables[key].rows):>12} rows   columns: {cols}")
    extra = [k for k in order if k not in qinfo.tables]
    if extra:
        print("\n  FK parent tables (needed so foreign keys resolve):")
        for key in extra:
            print(f"    {label_of(key):<28} ~{fmt_int(tables[key].rows):>12} rows")
    if qinfo.conditions:
        print("\n  Conditions in the plan:")
        for kind, text in qinfo.conditions:
            print(f"    {kind + ':':<14} {text}")
    if qinfo.literals:
        print(f"\n  Literals found (injected into {opts.match_rate:.0%} of seeded rows so the query matches):")
        for (key, col), vals in qinfo.literals.items():
            print(f"    {label_of(key)}.{col}: " + ", ".join(("LIKE " if k == "like" else "") + repr(v) for k, v in vals))
    for note in qinfo.notes:
        print(f"\n  Note: {note}")
    if not show_plan:
        return
    print("\n  Seeding order: " + " -> ".join(label_of(k) for k in order))
    print("\n  Generation plan:")
    for key in order:
        t = tables[key]
        print(f"    {t.label}")
        plan = plan_columns(t, qinfo, opts, describe_fks(t, tables), unique_columns(t), {})
        for col, _, desc in plan:
            print(f"      {col.name:<24} {col.sqltype:<28} {desc}")
        for chk in t.checks:
            if not any(c.check_pool for c in t.columns.values() if c.name in chk):
                print(f"      (check) {chk}")


def cmd_seed(conn, args, qinfo, tables, order, opts):
    counts = plan_row_counts(tables, order, qinfo, args)
    work = [k for k in order if counts[k] > 0]
    if not work:
        print("Nothing to insert.")
        return {}
    total = sum(counts[k] for k in work)
    print(f"\nPlan: insert {fmt_int(total)} rows into {len(work)} table(s) on "
          f"database '{conn.info.dbname}' at {conn.info.host}:{conn.info.port}")
    for k in work:
        print(f"    {label_of(k):<28} +{fmt_int(counts[k])}")
    if args.truncate:
        print("\n  WARNING: --truncate will run TRUNCATE ... CASCADE on these tables first. This deletes ALL their")
        print("  existing rows, plus rows in any table that references them. It cannot be undone.")
    if not confirm("Proceed?", args):
        raise PgStressError("aborted")

    apply_session(conn, qinfo)
    with conn.cursor() as cur:
        cur.execute("SET synchronous_commit = off")
        if args.truncate:
            cur.execute("TRUNCATE " + ", ".join(tables[k].fq for k in work) + " CASCADE")
            for k in work:
                tables[k].rows = 0
    conn.commit()

    print("\nSEEDING")
    results = {}
    for k in work:
        results[label_of(k)] = seed_table(conn, tables[k], counts[k], tables, qinfo, opts)
    return results


def cmd_setup(conn, args, qinfo):
    setup = qinfo.of_kind("setup")
    if not setup:
        return
    print(f"\nThe script has {len(setup)} setup statement(s). They will be applied to the database once (committed):")
    for s in setup:
        print(f"    {s.short}")
    if not confirm("Apply them?", args):
        raise PgStressError("aborted")
    apply_setup(conn, qinfo, say=lambda line: print(f"  {line}"))


def cmd_bench(args, qinfo):
    dsn = args.dsn
    rollback, mode_note = bench_mode(qinfo, args.commit_writes)
    session = [s.text for s in qinfo.of_kind("session")]
    with connect(dsn) as conn:
        print("\nPLANS (one execution, rolled back)")
        sections, hints, notes = profile_script(conn, qinfo)
        print_profile(sections, hints, notes)
        for s in session:
            conn.execute(s)
        conn.autocommit = not rollback
        for _ in range(args.warmup):
            run_script_once(conn, qinfo, rollback)

    levels = [int(x) for x in str(args.concurrency).split(",") if x.strip()]
    mode = f"{fmt_int(args.iterations)} executions" if args.iterations else f"{args.duration}s"
    print(f"\nSTRESS TEST ({mode} per stage{'; ' + mode_note if mode_note else ''}; Ctrl+C stops a stage)")
    stages = []
    work = [s.text for s in qinfo.work]
    for c in levels:
        stages.append(run_stage(dsn, work, c, args.duration, args.iterations, rollback, args.prepared,
                                session_sql=session))
    print_stage_table(stages)
    return {"profile": sections, "hints": hints, "notes": notes, "stages": stages}


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""),
                        help="connection string, e.g. postgresql://user:pass@localhost:5432/db "
                             "(default: $DATABASE_URL, then PG* env vars)")
    src = common.add_mutually_exclusive_group(required=True)
    src.add_argument("-q", "--query", help="SQL to test: a query, CALL, DO block or a script of several statements")
    src.add_argument("-f", "--file", help="file containing the SQL script")
    common.add_argument("--no-trace", action="store_true",
                        help="don't execute the script during analysis (rolled back) to trace routines")

    s = common.add_argument_group("seeding")
    s.add_argument("--rows", action="append", metavar="N | TABLE=N",
                   help="rows to insert: a default for every query table (e.g. 1m) or per table (orders=5m). Repeatable.")
    s.add_argument("--set", action="append", dest="overrides", metavar="TABLE.COL=SQL",
                   help="custom SQL expression for a column; `g` is the row number. Repeatable. "
                        "e.g. --set \"orders.status=(ARRAY['new','paid'])[1+floor(random()*2)::int]\"")
    s.add_argument("--match-rate", type=float, default=0.1,
                   help="share of rows that get literal values found in the query's WHERE (default 0.1)")
    s.add_argument("--null-rate", type=float, default=0.0, help="share of NULLs in nullable columns (default 0)")
    s.add_argument("--span-days", type=int, default=365, help="random dates/timestamps fall in the last N days (default 365)")
    s.add_argument("--batch", type=parse_count, default=50_000, help="rows per INSERT statement (default 50k)")
    s.add_argument("--fk-sample", type=parse_count, default=100_000,
                   help="max parent rows sampled to fill foreign keys (default 100k)")
    s.add_argument("--truncate", action="store_true", help="TRUNCATE ... CASCADE the seeded tables first (destructive)")
    s.add_argument("-y", "--yes", action="store_true", help="skip prompts and confirmations")

    b = common.add_argument_group("benchmark")
    b.add_argument("-c", "--concurrency", default="1,4,16", help="comma list of concurrent clients per stage (default 1,4,16)")
    b.add_argument("-d", "--duration", type=float, default=20, help="seconds per stage (default 20)")
    b.add_argument("-n", "--iterations", type=parse_count, help="fixed executions per stage instead of a duration")
    b.add_argument("--warmup", type=int, default=3, help="warm-up executions before the stress test (default 3)")
    b.add_argument("--prepared", action="store_true", help="use server-side prepared statements")
    b.add_argument("--commit-writes", action="store_true", help="commit write queries during bench instead of rolling back")
    b.add_argument("--report", help="write a JSON report to this file")

    parser = argparse.ArgumentParser(prog="pgstress", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("analyze", parents=[common], help="show tables, columns, literals and the data generation plan")
    sub.add_parser("seed", parents=[common], help="analyze + insert synthetic rows")
    sub.add_parser("bench", parents=[common], help="EXPLAIN ANALYZE + stress test the query on current data")
    sub.add_parser("all", parents=[common], help="analyze + seed + bench")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        script = read_script(args)
        opts = SeedOptions(args.match_rate, args.null_rate, args.batch, args.fk_sample,
                           args.span_days, parse_overrides(args.overrides))
        report = {"script": script}
        with psycopg.connect(args.dsn) as conn:
            qinfo, tables, order = analyze_script(conn, script, trace=not args.no_trace)
            if not qinfo.tables:
                print("The script does not touch any table - nothing to seed.")
            print_analysis(qinfo, tables, order, opts, show_plan=args.cmd != "bench")
            report["tables"] = {label_of(k): tables[k].rows for k in order}
            if args.cmd != "analyze":
                cmd_setup(conn, args, qinfo)
            if args.cmd in ("seed", "all") and tables:
                report["seeded"] = cmd_seed(conn, args, qinfo, tables, order, opts)
        if args.cmd in ("bench", "all"):
            report.update(cmd_bench(args, qinfo))
        if args.report:
            with open(args.report, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, default=str)
            print(f"\nReport written to {args.report}")
    except PgStressError as e:
        sys.exit(f"\npgstress: {e}")
    except psycopg.OperationalError as e:
        sys.exit(f"\npgstress: cannot connect: {str(e).strip()}")
    except psycopg.Error as e:  # e.g. the script failing on the seeded data during warm-up
        sys.exit(f"\npgstress: the script failed: {pg_error(e)}")
    except KeyboardInterrupt:
        sys.exit("\npgstress: interrupted")


if __name__ == "__main__":
    main()
