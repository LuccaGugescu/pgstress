# pgstress

Give it PostgreSQL SQL: a query, a `CALL` to a procedure, a function call, a `DO` block, or a whole
script. It works out every table the SQL touches, including tables used inside functions, procedures
and triggers. It fills those tables (and their foreign-key parents) with as many synthetic rows as you
choose, captures a plan for every statement it runs, and then runs a multi-client stress test.

> **Writes data.** Point it at a dev, staging or throwaway database, never production.

## Install

```bash
pip install "psycopg[binary]"
```

Needs Python 3.10+ and PostgreSQL 12+.

## Windows app

### Installer (recommended)

Download **`pgstress-setup-<version>.exe`** from the
[latest release](https://github.com/LuccaGugescu/pgstress/releases/latest) and run it.

- It installs for your user only by default, so it doesn't ask for admin rights. You can pick an
  all-users install on the first page.
- It adds a Start menu entry (and, if you tick the box, a desktop icon) and appears in
  **Settings → Apps** with an uninstaller.
- It starts faster than the portable exe because nothing has to be unpacked at launch.
- Uninstalling also deletes the saved connection string and query in `%LOCALAPPDATA%\pgstress`.
- For unattended installs: `pgstress-setup-0.1.0.exe /VERYSILENT /CURRENTUSER`

To build the installer yourself, install [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install JRSoftware.InnoSetup`) and run **`build-installer.bat`**. The version number comes from
the `VERSION` file.

### Portable exe

**`pgstress.exe`** on the same release page is a single-file Windows app (or build it with
`build-exe.bat`). It doesn't need Python, and you can copy it
anywhere. Double-click it and pgstress opens in its own window, which uses Microsoft Edge WebView2
(built into Windows 10/11). The SQL editor is bundled, so it also works offline. The connection
string, query and options are saved in `%LOCALAPPDATA%\pgstress`.

To rebuild after changing the code, run **`build-exe.bat`**. It installs PyInstaller and pywebview,
then writes `dist\pgstress.exe`.

If WebView2 is missing, the app opens in your default browser instead.

## Web UI (browser)

Double-click **`start-pgstress.bat`**. It installs psycopg if it's missing, starts the server and opens
`http://127.0.0.1:8765` in your browser. You can also run `python pgstress_ui.py` from any OS.

1. Paste a connection string and press **Connect**. The UI remembers it in your browser.
2. Write the query in the editor. This is Monaco, the editor VS Code uses, so you get SQL highlighting
   and autocomplete for your table and column names (type `o.` for an alias's columns). Postgres
   errors show as red squiggles while you type.
3. In the right-hand panel, choose how many rows to add to each table (0 / 10k / 100k / 1M / 10M, or
   type a number such as `250k`). Then pick the concurrency levels and the duration of each stage.
4. Press **Run test** or **Ctrl+Enter**. You get live progress, then latency KPIs, a p50/p95/p99 chart
   per concurrency level, a table of stages, the EXPLAIN ANALYZE plan with index hints, and the data
   generation plan for each column.

The server only listens on 127.0.0.1. The editor is served from `ui/vs`. If that folder is missing,
the editor loads from cdnjs instead.

## Usage (command line)

```bash
export DATABASE_URL=postgresql://user:pass@localhost:5432/mydb

python pgstress.py analyze -f query.sql    # what the SQL touches (routines too) + how each column will be generated
python pgstress.py seed    -f query.sql    # asks how many rows per table, then inserts
python pgstress.py bench   -f query.sql    # plans (nested too) + stress test on current data
python pgstress.py all     -q "CALL nightly_job(); SELECT * FROM report_view;"
python pgstress.py all     -f query.sql    # seed, then bench
```

Use `-q "SELECT ..."` instead of `-f` for an inline query, and `--dsn` instead of `DATABASE_URL`.

### Choosing row counts

When run in a terminal, `seed` asks for a count per table (accepts `250000`, `250k`, `2m`).
To script it:

```bash
python pgstress.py all -f query.sql --rows 1m --rows users=50k --rows countries=0 -y
```

A bare number is the default for every table the query reads. FK-parent tables that already have
rows get 0 new rows by default (existing rows are reused); empty ones get a tenth of the default.

### Stress test

```bash
python pgstress.py bench -f query.sql -c 1,8,32,64 -d 30 --report report.json
```

One stage per concurrency level. Each client has its own connection and runs the query in a loop,
fetching all rows. Each stage reports executions, errors, QPS, avg/p50/p95/p99/max latency and rows
returned. `-n 5000` runs a fixed number of executions per stage instead of a duration.
`--prepared` uses server-side prepared statements.

INSERT/UPDATE/DELETE queries are run inside a transaction that is rolled back every time, so the data
stays the same between runs (`--commit-writes` turns that off).

## Procedures, functions, triggers and scripts

Everything goes through the same steps as a single query: find the tables, seed them, capture plans,
then stress test.

- **Scripts** are split on `;`. Quotes, `$$` bodies, comments and `BEGIN ATOMIC ... END` are handled.
  Each statement gets one of these roles:

  | statement | role |
  |---|---|
  | `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `WITH` | timed in every run |
  | `CALL`, `DO`, `TRUNCATE`, ... | timed in every run |
  | `SET` / `RESET` | run once on each connection |
  | `CREATE`, `ALTER`, `DROP`, `GRANT`, ... | setup: applied once, committed, after you confirm. Objects that already exist are skipped |
  | `BEGIN` / `COMMIT` | ignored, because pgstress wraps each run itself |

  One stress-test execution runs all the timed statements in order.
- **Routines are followed to any depth.** This covers functions called anywhere in the SQL,
  procedures run with `CALL`, `DO` blocks, routines called from inside other routines, and the
  triggers on every table that gets written. Their tables are found in two ways:
  - from the source: PL/pgSQL and SQL bodies, plus the recorded dependencies of `BEGIN ATOMIC`
    bodies, with views expanded to their base tables;
  - from a trace: one rolled-back execution with **`auto_explain`**. This returns the real plan of
    every nested statement, which is analyzed the same way as a top-level `EXPLAIN`, so columns and
    WHERE literals inside routines count too.
- **Query plan view.** Each statement shows its timing, then the `EXPLAIN ANALYZE` plan of every
  statement it ran inside: procedure bodies, function calls and trigger firings. A statement that runs
  many times, such as a per-row trigger, is shown once with its call count.
- **Writes are rolled back** after every run. The exception is a procedure or `DO` block that runs
  `COMMIT` itself: it can't run inside a transaction, so its changes are kept, and the UI and CLI say so.

`auto_explain` ships with PostgreSQL. pgstress loads it per session, which needs a superuser, or
`auto_explain` listed in `session_preload_libraries`. Without it, tables are still found from routine
source code, but the plans of statements inside routines are not shown. Dynamic SQL (`EXECUTE`) is
only seen when the trace reaches it. `--no-trace` skips the analysis execution.

## How data is generated

- **Tables and columns** come from `EXPLAIN (VERBOSE)`, so views, CTEs and subqueries resolve to the
  real base tables. Partitions map back to their partitioned parent.
- **Foreign keys** take values from rows that exist in the parent table, sampling up to `--fk-sample`
  parent rows (100k by default). Parents are seeded before children. When a group of FKs together
  forms a unique key (for example a junction table), the tool walks through the combinations in order
  instead of picking at random, so rows don't collide.
- **Unique and primary-key columns** count upward from the current maximum. Unique text columns get a
  per-run prefix. Serial, identity and generated columns are left to Postgres.
- **Enums and `CHECK (col IN (...))`** columns pick from the allowed values.
- **Literals in the query's WHERE clause** (`status = 'paid'`, `IN (...)`, `LIKE 'abc%'`) go into
  `--match-rate` of the rows (10% by default), so the query has rows to return.
- Columns with a default that the query never touches use the default.
- Everything else gets a random value of the right type (numbers, text, dates within
  `--span-days`, uuid, json, and so on).

### Overriding a column

`g` is the row number within the run:

```bash
--set "orders.status=(ARRAY['new','paid'])[1 + floor(random()*2)::int]"
--set "orders.created_at=now() - (g % 30) * interval '1 day'"
--set "users.country='BR'"
```

Use this when a column has a CHECK constraint the tool can't satisfy, when an extension type isn't
supported, or when you want a particular data distribution (for example skewed values to test index
selectivity).

## Other flags

| flag | default | meaning |
|---|---|---|
| `--batch` | 50k | rows per INSERT statement |
| `--null-rate` | 0 | share of NULLs in nullable columns |
| `--truncate` | off | runs `TRUNCATE ... CASCADE` on the seeded tables first (**deletes their existing rows and rows in tables that reference them**) |
| `--warmup` | 3 | executions before the stress test |
| `-y` | off | skip prompts |

## Limits

- Statements that use `$1` parameters have to be rewritten with literal values (inside function bodies they're fine).
- Partitioned tables only accept generated values that fall inside some partition. Use `--set` to keep
  the partition key in range, or add a default partition.
- Seeding uses `ON CONFLICT DO NOTHING`, so rows that hit unique or exclusion constraints are skipped,
  and the output reports how many.

## Examples

`examples/` has a small schema you can try the tool on. It has users, orders, order items, products,
an enum, CHECK constraints, a self-referencing FK and a composite primary key. It also has routines:
a PL/pgSQL function, a SQL-standard function, a procedure that fires an audit trigger, a procedure
that commits, and a view.

```bash
psql "$DATABASE_URL" -f examples/schema.sql
psql "$DATABASE_URL" -f examples/routines.sql
python pgstress.py all -f examples/script.sql --rows 100k -y
```

## Third-party

The SQL editor is [Monaco Editor](https://github.com/microsoft/monaco-editor) 0.45.0, © Microsoft,
MIT license (`ui/vs/LICENSE`). Only its SQL language files are bundled.

## License

[MIT](LICENSE)
