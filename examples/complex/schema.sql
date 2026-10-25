-- A small sales system: 10 tables, functions, procedures that call each other, triggers and a view.
DROP SCHEMA IF EXISTS sales CASCADE;
CREATE SCHEMA sales;
SET search_path = sales, public;

CREATE TABLE regions (
  id    serial PRIMARY KEY,
  name  text NOT NULL UNIQUE
);

CREATE TABLE customers (
  id          bigserial PRIMARY KEY,
  region_id   int NOT NULL REFERENCES regions(id),
  email       varchar(160) NOT NULL UNIQUE,
  segment     text NOT NULL CHECK (segment IN ('consumer', 'smb', 'enterprise')),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
  id        serial PRIMARY KEY,
  sku       text NOT NULL UNIQUE,
  category  text NOT NULL CHECK (category IN ('hardware', 'software', 'services', 'accessories')),
  price     numeric(10,2) NOT NULL CHECK (price > 0)
);

CREATE TABLE warehouses (
  id         serial PRIMARY KEY,
  region_id  int NOT NULL REFERENCES regions(id),
  name       text NOT NULL
);

CREATE TABLE inventory (
  warehouse_id  int NOT NULL REFERENCES warehouses(id),
  product_id    int NOT NULL REFERENCES products(id),
  qty           int NOT NULL CHECK (qty >= 0),
  PRIMARY KEY (warehouse_id, product_id)
);

CREATE TABLE orders (
  id            bigserial PRIMARY KEY,
  customer_id   bigint NOT NULL REFERENCES customers(id),
  warehouse_id  int NOT NULL REFERENCES warehouses(id),
  status        text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'paid', 'shipped', 'cancelled')),
  ordered_at    timestamptz NOT NULL DEFAULT now(),
  total         numeric(12,2)
);

CREATE TABLE order_lines (
  order_id    bigint NOT NULL REFERENCES orders(id),
  line_no     int NOT NULL,
  product_id  int NOT NULL REFERENCES products(id),
  qty         int NOT NULL CHECK (qty > 0),
  unit_price  numeric(10,2) NOT NULL CHECK (unit_price > 0),
  PRIMARY KEY (order_id, line_no)
);

CREATE TABLE payments (
  id        bigserial PRIMARY KEY,
  order_id  bigint NOT NULL REFERENCES orders(id),
  amount    numeric(12,2) NOT NULL CHECK (amount > 0),
  method    text NOT NULL CHECK (method IN ('card', 'transfer', 'paypal')),
  paid_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE loyalty_points (
  customer_id  bigint PRIMARY KEY REFERENCES customers(id),
  points       int NOT NULL DEFAULT 0
);

CREATE TABLE stock_movements (
  id            bigserial PRIMARY KEY,
  warehouse_id  int NOT NULL,
  product_id    int NOT NULL,
  delta         int NOT NULL,
  moved_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- functions

-- PL/pgSQL: total of one order from its lines
CREATE FUNCTION order_total(p_order bigint) RETURNS numeric LANGUAGE plpgsql STABLE AS $$
DECLARE t numeric;
BEGIN
  SELECT coalesce(sum(qty * unit_price), 0) INTO t FROM order_lines WHERE order_id = p_order;
  RETURN t;
END $$;

-- SQL-standard body: lifetime value of a customer from payments
CREATE FUNCTION customer_ltv(p_customer bigint) RETURNS numeric LANGUAGE sql STABLE
BEGIN ATOMIC
  SELECT coalesce(sum(p.amount), 0)
  FROM payments p JOIN orders o ON o.id = p.order_id
  WHERE o.customer_id = p_customer AND o.status <> 'cancelled';
END;

-- --------------------------------------------------------------- triggers

-- every stock change is logged
CREATE FUNCTION trg_inventory_log() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO stock_movements (warehouse_id, product_id, delta)
  VALUES (NEW.warehouse_id, NEW.product_id, NEW.qty - OLD.qty);
  RETURN NEW;
END $$;
CREATE TRIGGER inventory_log AFTER UPDATE OF qty ON inventory
  FOR EACH ROW EXECUTE FUNCTION trg_inventory_log();

-- every payment earns loyalty points (1 point per 10 currency units)
CREATE FUNCTION trg_payment_points() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO loyalty_points AS lp (customer_id, points)
  SELECT o.customer_id, floor(NEW.amount / 10)::int FROM orders o WHERE o.id = NEW.order_id
  ON CONFLICT (customer_id) DO UPDATE SET points = lp.points + EXCLUDED.points;
  RETURN NEW;
END $$;
CREATE TRIGGER payment_points AFTER INSERT ON payments
  FOR EACH ROW EXECUTE FUNCTION trg_payment_points();

-- ------------------------------------------------------------- procedures

-- place an order from the fullest warehouse in the customer's region, take stock, record the payment
CREATE PROCEDURE place_order(p_customer bigint, p_product int, p_qty int, p_method text DEFAULT 'card')
LANGUAGE plpgsql AS $$
DECLARE
  v_wh int;
  v_price numeric;
  v_order bigint;
BEGIN
  SELECT i.warehouse_id INTO v_wh
  FROM inventory i
  JOIN warehouses w ON w.id = i.warehouse_id
  JOIN customers c ON c.region_id = w.region_id
  WHERE c.id = p_customer AND i.product_id = p_product AND i.qty >= p_qty
  ORDER BY i.qty DESC
  LIMIT 1;
  IF v_wh IS NULL THEN  -- nothing in the region: fall back to any warehouse with stock
    SELECT warehouse_id INTO v_wh FROM inventory
    WHERE product_id = p_product AND qty >= p_qty ORDER BY qty DESC LIMIT 1;
  END IF;
  IF v_wh IS NULL THEN
    RAISE EXCEPTION 'product % is out of stock', p_product;
  END IF;

  SELECT price INTO v_price FROM products WHERE id = p_product;

  INSERT INTO orders (customer_id, warehouse_id, status) VALUES (p_customer, v_wh, 'paid')
  RETURNING id INTO v_order;
  INSERT INTO order_lines (order_id, line_no, product_id, qty, unit_price)
  VALUES (v_order, 1, p_product, p_qty, v_price);
  UPDATE inventory SET qty = qty - p_qty WHERE warehouse_id = v_wh AND product_id = p_product;
  UPDATE orders SET total = order_total(v_order) WHERE id = v_order;
  INSERT INTO payments (order_id, amount, method) VALUES (v_order, v_price * p_qty, p_method);
END $$;

-- recompute totals of recent paid orders
CREATE PROCEDURE recalc_totals(p_days int) LANGUAGE plpgsql AS $$
BEGIN
  UPDATE orders SET total = order_total(id)
  WHERE status = 'paid' AND ordered_at > now() - make_interval(days => p_days);
END $$;

-- ------------------------------------------------------------------- view

CREATE VIEW v_customer_summary AS
SELECT c.id, c.email, c.segment, r.name AS region, lp.points
FROM customers c
JOIN regions r ON r.id = c.region_id
LEFT JOIN loyalty_points lp ON lp.customer_id = c.id;
