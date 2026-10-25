SET search_path = sales, public;

-- a customer buys 2 units of product 7 (stock, payment, loyalty points and stock log all change)
CALL place_order(42, 7, 2, 'card');

-- refresh the totals of the last week's paid orders
CALL recalc_totals(7);

-- regional sales report: monthly revenue per region and segment, the region's best-selling
-- hardware product, rank within the region, and the lifetime value of the segment's top customer
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
  SELECT r.name AS region, c.segment, date_trunc('month', l.ordered_at) AS month,
         count(DISTINCT l.order_id) AS orders, sum(l.amount) AS revenue
  FROM lines l
  JOIN customers c ON c.id = l.customer_id
  JOIN regions r ON r.id = l.region_id
  GROUP BY 1, 2, 3
),
ranked AS (
  SELECT *, rank() OVER (PARTITION BY region, month ORDER BY revenue DESC) AS rank_in_region
  FROM by_region
)
SELECT rk.region, rk.segment, rk.month, rk.orders, rk.revenue, rk.rank_in_region,
       top.sku AS best_hardware, top.units,
       vip.email AS top_customer, customer_ltv(vip.id) AS top_customer_ltv, vip.points
FROM ranked rk
LEFT JOIN LATERAL (
  SELECT p.sku, sum(l2.qty) AS units
  FROM lines l2
  JOIN products p ON p.id = l2.product_id
  JOIN regions r2 ON r2.id = l2.region_id
  WHERE r2.name = rk.region
    AND date_trunc('month', l2.ordered_at) = rk.month
    AND p.category = 'hardware'
  GROUP BY p.sku
  ORDER BY units DESC
  LIMIT 1
) top ON true
LEFT JOIN LATERAL (
  SELECT s.id, s.email, s.points
  FROM v_customer_summary s
  WHERE s.region = rk.region AND s.segment = rk.segment
  ORDER BY s.points DESC NULLS LAST
  LIMIT 1
) vip ON true
WHERE rk.rank_in_region <= 3
ORDER BY rk.month DESC, rk.region, rk.rank_in_region;
