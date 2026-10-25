SET search_path = sales, public;

-- fixes suggested by the first run's plans
-- customer_ltv() looks orders up by customer (it ran 239 seq scans of 300k rows)
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_id);
-- ...and then joins payments by order (a seq scan of 250k payments per call)
CREATE INDEX IF NOT EXISTS payments_order_idx ON payments (order_id);
-- recalc_totals() and the report filter orders on status + date
CREATE INDEX IF NOT EXISTS orders_status_ordered_idx ON orders (status, ordered_at);

CALL place_order((1 + floor(random() * 50000))::bigint, (1 + floor(random() * 5000))::int, 1, 'card');
-- recalc_totals is a nightly job: left out of the concurrent test

-- same report; the two per-row LATERAL subqueries are now computed once each
WITH lines AS (
  SELECT o.id AS order_id, o.customer_id, o.ordered_at, w.region_id,
         ol.product_id, ol.qty, ol.qty * ol.unit_price AS amount
  FROM orders o
  JOIN order_lines ol ON ol.order_id = o.id
  JOIN warehouses w ON w.id = o.warehouse_id
  WHERE o.status IN ('paid', 'shipped')
    AND o.ordered_at >= now() - interval '90 days'
),
by_region AS (
  SELECT r.id AS region_id, r.name AS region, c.segment, date_trunc('month', l.ordered_at) AS month,
         count(DISTINCT l.order_id) AS orders, sum(l.amount) AS revenue
  FROM lines l
  JOIN customers c ON c.id = l.customer_id
  JOIN regions r ON r.id = l.region_id
  GROUP BY 1, 2, 3, 4
),
ranked AS (
  SELECT *, rank() OVER (PARTITION BY region, month ORDER BY revenue DESC) AS rank_in_region
  FROM by_region
),
best_hardware AS (
  SELECT DISTINCT ON (l.region_id, date_trunc('month', l.ordered_at))
         l.region_id, date_trunc('month', l.ordered_at) AS month, p.sku, sum(l.qty) AS units
  FROM lines l
  JOIN products p ON p.id = l.product_id
  WHERE p.category = 'hardware'
  GROUP BY l.region_id, date_trunc('month', l.ordered_at), p.sku
  ORDER BY l.region_id, date_trunc('month', l.ordered_at), sum(l.qty) DESC
),
vip AS (
  SELECT DISTINCT ON (region, segment) id, email, points, region, segment
  FROM v_customer_summary
  ORDER BY region, segment, points DESC NULLS LAST
)
SELECT rk.region, rk.segment, rk.month, rk.orders, rk.revenue, rk.rank_in_region,
       bh.sku AS best_hardware, bh.units,
       vip.email AS top_customer, customer_ltv(vip.id) AS top_customer_ltv, vip.points
FROM ranked rk
LEFT JOIN best_hardware bh ON bh.region_id = rk.region_id AND bh.month = rk.month
LEFT JOIN vip ON vip.region = rk.region AND vip.segment = rk.segment
WHERE rk.rank_in_region <= 3
ORDER BY rk.month DESC, rk.region, rk.rank_in_region;
