SELECT u.email, u.tier, count(*) AS orders, sum(oi.qty * p.price) AS revenue
FROM orders o
JOIN users u ON u.id = o.user_id
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
WHERE o.status = 'paid' AND u.tier = 'pro' AND u.email LIKE 'user%'
  AND o.created_at > now() - interval '90 days'
GROUP BY u.email, u.tier
ORDER BY revenue DESC
LIMIT 50;
