-- setup: helper function created by the script itself
CREATE OR REPLACE FUNCTION orders_of(p_status order_status) RETURNS SETOF orders LANGUAGE sql STABLE AS $$
  SELECT * FROM orders WHERE status = p_status;
$$;
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status);
SET work_mem = '16MB';
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM audit_log WHERE note LIKE 'status%';
  UPDATE products SET price = price WHERE id = 1;
END $$;
SELECT count(*) FROM orders_of('cancelled') o JOIN users u ON u.id = o.user_id WHERE u.tier = 'enterprise';
