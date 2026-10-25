#!/usr/bin/env python3
"""
pgstress UI - local web app around pgstress.py.

    python pgstress_ui.py            # opens http://127.0.0.1:8765
    python pgstress_ui.py --port 9000 --no-browser

Listens on 127.0.0.1 only. Uses nothing beyond the standard library and psycopg.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

import pgstress as ps

# When frozen by PyInstaller, bundled files live under sys._MEIPASS instead of next to this file.
UI_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "ui"
INDEX_HTML = UI_DIR / "index.html"
STATIC_TYPES = {".js": "application/javascript", ".css": "text/css", ".ttf": "font/ttf", ".svg": "image/svg+xml"}
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, message, position=None):
        super().__init__(message)
        self.position = position


def make_opts(body) -> ps.SeedOptions:
    overrides = {}
    for line in (body.get("overrides") or "").splitlines():
        if line.strip():
            overrides.update(ps.parse_overrides([line.strip()]))
    return ps.SeedOptions(
        match_rate=float(body.get("match_rate", 0.1)),
        null_rate=float(body.get("null_rate", 0.0)),
        batch=int(body.get("batch", 50_000)),
        fk_sample=int(body.get("fk_sample", 100_000)),
        span_days=int(body.get("span_days", 365)),
        overrides=overrides,
    )


def connect(dsn):
    try:
        return psycopg.connect(dsn, connect_timeout=8)
    except psycopg.Error as e:
        raise ApiError(f"cannot connect: {str(e).strip()}") from e


def inspect(conn, script, trace):
    try:
        return ps.analyze_script(conn, script, trace=trace)
    except ps.PgStressError as e:
        raise ApiError(str(e), e.position) from e


# --------------------------------------------------------------------------
# API handlers
# --------------------------------------------------------------------------

def api_schema(body):
    with connect(body["dsn"]) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.table_schema, c.table_name, c.column_name, c.data_type
               FROM information_schema.columns c
               JOIN information_schema.tables t USING (table_schema, table_name)
               WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
               ORDER BY 1, 2, c.ordinal_position"""
        )
        tables = {}
        for schema, table, col, dtype in cur.fetchall():
            label = table if schema == "public" else f"{schema}.{table}"
            tables.setdefault(label, []).append({"name": col, "type": dtype})
        cur.execute(
            """SELECT n.nspname, p.proname, p.prokind, pg_get_function_identity_arguments(p.oid)
               FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE p.prokind IN ('f', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                 AND n.nspname NOT LIKE 'pg\\_%'
                 AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                                 AND d.objid = p.oid AND d.deptype = 'e')
               ORDER BY 1, 2"""
        )
        routines = [{"name": name if schema == "public" else f"{schema}.{name}",
                     "kind": "procedure" if kind == "p" else "function", "args": args}
                    for schema, name, kind, args in cur.fetchall()]
        cur.execute("SELECT current_database(), split_part(version(), ' ', 2)")
        db, version = cur.fetchone()
    return {"database": db, "version": version, "tables": tables, "routines": routines}


def analysis_payload(qinfo, tables, order, opts):
    out = []
    for key in order:
        t = tables[key]
        plan = ps.plan_columns(t, qinfo, opts, ps.describe_fks(t, tables), ps.unique_columns(t), {})
        out.append({
            "label": t.label,
            "role": "query" if key in qinfo.tables else "fk",
            "rows": t.rows,
            "refs": sorted(c for c in qinfo.refs.get(key, ()) if c in t.columns),
            "written": key in qinfo.written,
            "columns": [{"name": c.name, "type": c.sqltype, "how": how} for c, _, how in plan],
        })
    rollback, mode_note = ps.bench_mode(qinfo)
    return {
        "is_write": qinfo.is_write,
        "txn_control": qinfo.txn_control,
        "traced": qinfo.traced,
        "mode_note": mode_note,
        "tables": out,
        "statements": [{"kind": s.kind, "text": s.short} for s in qinfo.statements],
        "routines": [{"name": r.name, "kind": r.kind, "language": r.language,
                      "tables": [ps.label_of(k) for k in r.tables], "writes": [ps.label_of(k) for k in r.writes],
                      "txn_control": r.txn_control, "dynamic_sql": r.dynamic_sql} for r in qinfo.routines],
        "notes": qinfo.notes,
        "conditions": [{"kind": k, "text": t} for k, t in qinfo.conditions],
        "literals": [
            {"table": ps.label_of(key), "column": col, "values": [("LIKE " if k == "like" else "") + v for k, v in vals]}
            for (key, col), vals in qinfo.literals.items()
        ],
    }


def api_analyze(body):
    opts = make_opts(body)
    with connect(body["dsn"]) as conn:
        qinfo, tables, order = inspect(conn, body["query"], bool(body.get("trace")))
    return analysis_payload(qinfo, tables, order, opts)


class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:10]
        self.state = "running"      # running | done | error | cancelled
        self.phase = "starting"
        self.progress = {}
        self.error = None
        self.log = []
        self.seeded = []
        self.profile = []
        self.hints = []
        self.notes = []
        self.stages = []
        self.started = time.time()
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def say(self, line):
        with self.lock:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")

    def snapshot(self):
        with self.lock:
            return {
                "id": self.id, "state": self.state, "phase": self.phase, "progress": dict(self.progress),
                "error": self.error, "log": list(self.log), "seeded": list(self.seeded),
                "profile": list(self.profile), "hints": list(self.hints), "notes": list(self.notes),
                "stages": list(self.stages), "elapsed": time.time() - self.started,
            }


def run_job(job: Job, body):
    try:
        opts = make_opts(body)
        dsn = body["dsn"]
        rows = {k: int(v) for k, v in (body.get("rows") or {}).items()}
        with connect(dsn) as conn:
            job.phase = "analyzing"
            qinfo, tables, order = inspect(conn, body["query"], trace=True)
            if qinfo.of_kind("setup"):
                if not body.get("apply_setup"):
                    raise ApiError("the script has setup statements (CREATE/ALTER/DROP...) that were not approved")
                job.phase = "setup"
                ps.apply_setup(conn, qinfo, say=job.say)
            work = [k for k in order if rows.get(ps.label_of(k), 0) > 0]
            if work:
                job.phase = "seeding"
                with conn.cursor() as cur:
                    cur.execute("SET synchronous_commit = off")
                    if body.get("truncate"):
                        job.say("TRUNCATE " + ", ".join(ps.label_of(k) for k in work) + " CASCADE")
                        cur.execute("TRUNCATE " + ", ".join(tables[k].fq for k in work) + " CASCADE")
                        for k in work:
                            tables[k].rows = 0
                conn.commit()
                for i, key in enumerate(work):
                    if job.stop.is_set():
                        break
                    t, n = tables[key], rows[ps.label_of(key)]
                    job.say(f"seeding {t.label}: {n:,} rows")

                    def progress(done, total, rate, label=t.label, idx=i):
                        job.progress = {"table": label, "done": done, "total": total, "rate": rate,
                                        "step": idx + 1, "steps": len(work)}

                    started = time.perf_counter()
                    inserted = ps.seed_table(conn, t, n, tables, qinfo, opts, on_progress=progress, stop=job.stop)
                    secs = time.perf_counter() - started
                    with job.lock:
                        job.seeded.append({"table": t.label, "requested": n, "inserted": inserted,
                                           "seconds": secs, "total_rows": t.rows})
                    job.say(f"  {inserted:,} rows in {secs:.1f}s")
        if job.stop.is_set():
            job.state, job.phase = "cancelled", "cancelled"
            return

        rollback, mode_note = ps.bench_mode(qinfo, bool(body.get("commit_writes")))
        session = [s.text for s in qinfo.of_kind("session")]
        job.phase, job.progress = "explain", {}
        with ps.connect(dsn) as conn:
            job.profile, job.hints, job.notes = ps.profile_script(conn, qinfo)
            for s in session:
                conn.execute(s)
            conn.autocommit = not rollback
            for _ in range(int(body.get("warmup", 3))):
                ps.run_script_once(conn, qinfo, rollback)
        if mode_note:
            job.notes.append(f"Stress test: {mode_note}.")
        job.say("plans captured")

        levels = [int(c) for c in body.get("concurrency", [1, 4, 16])]
        duration = float(body.get("duration", 20))
        iterations = int(body["iterations"]) if body.get("iterations") else None
        work_sql = [s.text for s in qinfo.work]
        job.phase = "stress"
        for idx, c in enumerate(levels):
            if job.stop.is_set():
                break
            job.say(f"stress stage: {c} client(s)")

            def progress(elapsed, executions, c=c, idx=idx):
                job.progress = {"concurrency": c, "elapsed": elapsed, "duration": duration,
                                "iterations": iterations, "executions": executions,
                                "stage": idx + 1, "stages": len(levels)}

            stage = ps.run_stage(dsn, work_sql, c, duration, iterations, rollback, bool(body.get("prepared")),
                                 stop=job.stop, on_progress=progress, session_sql=session)
            with job.lock:
                job.stages.append(stage)
        job.state = "cancelled" if job.stop.is_set() else "done"
        job.phase = job.state
        job.say(f"finished ({job.state})")
    except ApiError as e:
        job.state, job.error = "error", str(e)
    except ps.PgStressError as e:
        job.state, job.error = "error", str(e)
    except psycopg.Error as e:
        job.state, job.error = "error", str(ps.pg_error(e))
    except Exception as e:  # surface anything unexpected in the UI instead of a silent dead job
        job.state, job.error = "error", f"{type(e).__name__}: {e}"
    finally:
        if job.error:
            job.say(f"error: {job.error}")


def api_run(body):
    with JOBS_LOCK:
        if any(j.state == "running" for j in JOBS.values()):
            raise ApiError("a run is already in progress")
        job = Job()
        JOBS[job.id] = job
    threading.Thread(target=run_job, args=(job, body), daemon=True).start()
    return {"id": job.id}


def api_cancel(body):
    job = JOBS.get(body.get("id", ""))
    if not job:
        raise ApiError("unknown job")
    job.stop.set()
    job.say("cancel requested")
    return {"ok": True}


ROUTES = {"/api/schema": api_schema, "/api/analyze": api_analyze, "/api/run": api_run, "/api/cancel": api_cancel}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, default=str).encode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            return
        if self.path.startswith("/vs/"):
            target = (UI_DIR / self.path.split("?", 1)[0].lstrip("/")).resolve()
            if target.is_file() and UI_DIR.resolve() in target.parents:
                self.send(200, target.read_bytes(), STATIC_TYPES.get(target.suffix, "application/octet-stream"))
                return
        m = re.fullmatch(r"/api/job/(\w+)", self.path)
        if m and m.group(1) in JOBS:
            self.send_json(200, JOBS[m.group(1)].snapshot())
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        handler = ROUTES.get(self.path)
        if not handler:
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(200, handler(body))
        except ApiError as e:
            self.send_json(400, {"error": str(e), "position": e.position})
        except ps.PgStressError as e:
            self.send_json(400, {"error": str(e), "position": e.position})
        except psycopg.Error as e:
            self.send_json(400, {"error": str(ps.pg_error(e))})
        except Exception as e:
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is already taken,
    # which would defeat the free-port search below.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def start_server(preferred_port=8765):
    """Bind the first free port from preferred_port upward. Returns (server, url); call serve_forever()."""
    for port in range(preferred_port, preferred_port + 20):
        try:
            return Server(("127.0.0.1", port), Handler), f"http://127.0.0.1:{port}"
        except OSError:
            continue
    raise OSError(f"no free port between {preferred_port} and {preferred_port + 19}")


def main():
    parser = argparse.ArgumentParser(description="pgstress web UI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    try:
        server, url = start_server(args.port)
    except OSError as e:
        sys.exit(str(e))
    print(f"pgstress UI running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
