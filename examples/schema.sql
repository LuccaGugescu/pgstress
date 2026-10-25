CREATE TYPE order_status AS ENUM ('new','paid','shipped','cancelled');
CREATE TABLE countries (code char(2) PRIMARY KEY, name text NOT NULL);
CREATE TABLE users (
  id bigserial PRIMARY KEY,
  email varchar(120) NOT NULL UNIQUE,
  country char(2) NOT NULL REFERENCES countries(code),
  tier text NOT NULL CHECK (tier IN ('free','pro','enterprise')),
  manager_id bigint REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE products (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, sku text UNIQUE NOT NULL, price numeric(10,2) NOT NULL CHECK (price >= 0), attrs jsonb);
CREATE TABLE orders (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users(id),
  status order_status NOT NULL DEFAULT 'new',
  total numeric(12,2),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE order_items (
  order_id bigint REFERENCES orders(id),
  product_id int REFERENCES products(id),
  qty int NOT NULL CHECK (qty > 0),
  PRIMARY KEY (order_id, product_id)
);
INSERT INTO countries VALUES ('US','United States'),('BR','Brazil'),('DE','Germany');
