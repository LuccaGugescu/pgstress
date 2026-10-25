# Complex example: a sales system

This is a worked example of pgstress on a schema that looks like a real application:

- 10 tables
- 2 functions: `order_total` in PL/pgSQL and `customer_ltv` as a SQL-standard (`BEGIN ATOMIC`) function
- 2 procedures: `place_order` and `recalc_totals`, which calls a function on every row
- 2 triggers: one logs stock changes, the other adds loyalty points for each payment
- 1 view

```bash
psql "$DATABASE_URL" -f examples/complex/schema.sql

python pgstress.py all -f examples/complex/script.sql \
  --rows sales.regions=20 --rows sales.products=5k --rows sales.warehouses=40 --rows sales.customers=50k \
  --rows sales.inventory=200k --rows sales.loyalty_points=20k --rows sales.orders=300k \
  --rows sales.order_lines=900k --rows sales.payments=250k --rows sales.stock_movements=0 \
  --set "sales.order_lines.qty=1 + floor(random()*5)" \
  --set "sales.inventory.qty=50 + floor(random()*500)" \
  --set "sales.loyalty_points.points=floor(random()*2000)" \
  -c 1,4,8 -d 10 -y

python pgstress.py bench -f examples/complex/script_tuned.sql -c 1,4,8 -d 10 -y
```

## What the analysis found

The script ([script.sql](script.sql)) has four statements:
- `SET search_path`
- `CALL place_order(...)`
- `CALL recalc_totals(7)`
- a report query with CTEs, a window function, two `LATERAL` subqueries, a view and a function call per row

From these, pgstress reached all 6 routines and all 10 tables. Four of the tables are never named in
the script:
- `inventory`, read and updated by `place_order`
- `payments`, inserted by `place_order`
- `loyalty_points`, written by the `payment_points` trigger, which fires on that insert
- `stock_movements`, written by the `inventory_log` trigger, which fires on the inventory update

The tables are seeded in FK order. All 1.7M rows took about 55 s, and the triggers fired while
seeding too.

## Where the time went (first run)

| part | time | cause |
|---|---|---|
| `customer_ltv()`, called 239× | 14.1 s | seq scan of `orders` by `customer_id`, then of 250k `payments` by `order_id`, on every call |
| "best hardware" `LATERAL` | ~7 s | re-filters the materialized CTE once for each of the 239 output rows |
| top-customer `LATERAL` | ~4 s | re-scans 50k customers through the view once per output row |
| `recalc_totals(7)` | 0.5 s | seq scan of `orders` on `status` + `ordered_at` |

## Fixes ([script_tuned.sql](script_tuned.sql))

- **Three indexes**, run as setup statements in the script: `orders(customer_id)`,
  `payments(order_id)` and `orders(status, ordered_at)`.
- **Report rewrite:** the two per-row `LATERAL` subqueries are now computed once each, with
  `DISTINCT ON`. The output is the same 239 rows. The only difference is that ties for "best product"
  may pick a different SKU with the same unit count.

| | 1 client | 4 clients | 8 clients |
|---|---|---|---|
| original | 17,038 ms/run | 54,995 ms avg | 75,762 ms avg |
| tuned | 739 ms/run | 2,796 ms avg | 5,200 ms avg |

`customer_ltv()` went from 14,107 ms to 13.5 ms for its 239 calls.

## Remaining limit: lock contention

Even after tuning, throughput stayed at about 1.3 runs/s from 1 to 8 clients. Sampling
`pg_stat_activity` during the 8-client run showed 6 of the 8 sessions waiting on `Lock:tuple`. Every run
calls `place_order(42, 7, ...)`, so they all update the same inventory row, and `recalc_totals` updates
the same recent orders each time.

[script_random.sql](script_random.sql) uses a random customer and product on each run, and leaves out
the nightly `recalc_totals`. It scales to 1.3 → 4.1 → 5.4 runs/s. The plans still show one sort
spilling about 7.5 MB to disk, which a higher `work_mem` would avoid.
