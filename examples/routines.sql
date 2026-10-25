CREATE TABLE IF NOT EXISTS audit_log (id bigserial PRIMARY KEY, order_id bigint, changed_at timestamptz DEFAULT now(), note text);

CREATE OR REPLACE FUNCTION trg_orders_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO audit_log (order_id, note) VALUES (NEW.id, 'status ' || NEW.status);
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS orders_audit ON orders;
CREATE TRIGGER orders_audit AFTER UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION trg_orders_audit();

CREATE OR REPLACE FUNCTION user_revenue(p_user bigint) RETURNS numeric LANGUAGE plpgsql AS $$
DECLARE r numeric;
BEGIN
  SELECT sum(oi.qty * p.price) INTO r
  FROM orders o JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id
  WHERE o.user_id = p_user AND o.status = 'paid';
  RETURN coalesce(r, 0);
END $$;

CREATE OR REPLACE PROCEDURE ship_paid_orders(p_limit int) LANGUAGE plpgsql AS $$
BEGIN
  UPDATE orders SET status = 'shipped'
  WHERE id IN (SELECT id FROM orders WHERE status = 'paid' ORDER BY created_at LIMIT p_limit);
END $$;

CREATE OR REPLACE PROCEDURE nightly_batch() LANGUAGE plpgsql AS $$
BEGIN
  CALL ship_paid_orders(10);
  COMMIT;
END $$;

CREATE OR REPLACE VIEW pro_users AS SELECT * FROM users WHERE tier = 'pro';

CREATE OR REPLACE FUNCTION top_customer() RETURNS bigint LANGUAGE sql
BEGIN ATOMIC
  SELECT user_id FROM orders GROUP BY user_id ORDER BY count(*) DESC LIMIT 1;
END;
