-- Drop legacy tables so the schema matches the new PostgreSQL layout.
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS bookings CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS service_order CASCADE;

-- Users taken from the screenshot (email, password, name, lastname, username, is_admin).
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password TEXT NOT NULL,
    name VARCHAR(120),
    lastname VARCHAR(120),
    username VARCHAR(120),
    is_admin BOOLEAN NOT NULL DEFAULT FALSE
);

-- Core product catalogue (id, name, description, price, image, quantity).
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    price NUMERIC(10,2) NOT NULL DEFAULT 0,
    image TEXT,
    quantity INTEGER NOT NULL DEFAULT 0
);

-- Customer orders pulled from the screenshot (order_id, user_email, guest info, totals, phone, payment state).
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    user_email VARCHAR(255) REFERENCES users(email),
    first_name VARCHAR(120),
    last_name VARCHAR(120),
    address TEXT,
    order_date TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
    payment_status VARCHAR(64),
    phone VARCHAR(64),
    viewed BOOLEAN NOT NULL DEFAULT FALSE
);

-- Line items for each order (item_id, order_id, product_id, quantity, price).
CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL DEFAULT 1,
    price NUMERIC(10,2) NOT NULL DEFAULT 0
);

-- Bookings list (id, name, address, service_type, date plus optional metadata shown in the screenshot).
CREATE TABLE bookings (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(64),
    address TEXT,
    service_type VARCHAR(120),
    date DATE,
    time VARCHAR(64),
    frequency VARCHAR(64),
    notes TEXT,
    price NUMERIC(10,2),
    status VARCHAR(64) DEFAULT 'pending',
    user_id INTEGER REFERENCES users(id)
);

-- Table used for paid service bookings created from the services checkout flow.
CREATE TABLE service_order (
    id SERIAL PRIMARY KEY,
    full_name VARCHAR(200),
    email VARCHAR(255),
    phone VARCHAR(64),
    address TEXT,
    service_type VARCHAR(120),
    frequency VARCHAR(64),
    date DATE,
    time_window VARCHAR(64),
    notes TEXT,
    price NUMERIC(10,2),
    status VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Optional helper indexes for reporting speed.
CREATE INDEX IF NOT EXISTS idx_orders_user_email ON orders (user_email);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status);

-- ---------------------------------------------------------------------------
-- Seed data converted from the legacy SQL Server database
-- ---------------------------------------------------------------------------

INSERT INTO users (id, email, password, name, lastname, username, is_admin) VALUES
    (1, 'admin@cleanx.com', 'scrypt:32768:8:1$bHkElCqrOJclAhzn$f17688067a8afaf34c30181e9dd89d66793184b08c1e6fd057b7dc2c15b150bf9e14a558236a2f50f8def1f6611543e6398faad40052d265685f607bd656d564', 'Admin', 'User', 'admin', TRUE),
    (2, 'percy@cleanx.com', 'scrypt:32768:8:1$OEFtf7DDdoxJ7lbx$0cc1d20fd0f47c5cdbb92978b58d62f31e19aca6f5d95b2e0d9cf1570d549242cc0e57980868d7d99c02632839fbd1e498de2786dc83324a8ef016070c165bbe', 'Percy', 'Masuhla', 'percy', FALSE),
    (3, 'cynthia@cleanx.com', 'scrypt:32768:8:1$ogNHxp5pysPm1Gbh$a63e3f56052e1de4df069c1ea2068906021498e2e1cd318b86b785d004a257695d2f5f154350b9cc2832381e9c0a68fecfd0e308081ed598d13d1c2abebcbca5', 'Cynthia', 'Ndlovu', 'cynthia', FALSE),
    (4, 'guest@cleanx.com', 'scrypt:32768:8:1$bDAX3Sq3n22UbqmL$f59626b40e5b4ee87e076bf4d31bbed16fc8bb6422e4d86fc463b9876487947f854b3ff465d44d8e9edb4da43d9c422bf78a42e4a9e8cd6bf560ed7c7ed476f2', 'Guest', 'Customer', 'guest', FALSE);

INSERT INTO products (id, name, description, price, image, quantity) VALUES
    (1, 'Queen Panel Bed', 'Solid oak queen panel bed with storage headboard.', 10.99, 'images/product-1.jpeg', 12),
    (2, 'King Panel Bed', 'Spacious king size panel bed with padded frame.', 12.99, 'images/product-2.jpeg', 8),
    (3, 'Single Panel Bed', 'Compact single panel bed perfect for studio rooms.', 12.99, 'images/product-3.jpeg', 10),
    (4, 'Twin Panel Bed', 'Modern twin panel bed for guest rooms.', 22.99, 'images/product-4.jpeg', 6),
    (5, 'Fridge', 'Energy efficient stainless steel refrigerator.', 88.99, 'images/product-5.jpeg', 5),
    (6, 'Dresser', 'Six drawer wooden dresser for bedrooms.', 32.99, 'images/product-6.jpeg', 9),
    (7, 'Couch', 'Three-seater fabric couch with accent pillows.', 45.99, 'images/product-7.jpeg', 7),
    (8, 'Table', 'Contemporary dining table with glass top.', 33.99, 'images/product-8.jpeg', 11);

INSERT INTO orders (id, user_email, first_name, last_name, address, order_date, total_amount, payment_status, phone, viewed) VALUES
    (1, 'percy@cleanx.com', 'Percy', 'Masuhla', '123 Main Street, Johannesburg', '2025-02-04 09:21:00+02', 99.98, 'paid', '+27 82 555 0101', TRUE),
    (2, 'cynthia@cleanx.com', 'Cynthia', 'Ndlovu', '45 Rivonia Road, Sandton', '2025-02-10 14:05:00+02', 91.97, 'paid', '+27 83 234 8876', FALSE),
    (3, NULL, 'Nomsa', 'Nkosi', '12 Summerfields Estate, Centurion', '2025-02-18 11:42:00+02', 66.98, 'pending', '+27 81 888 0099', FALSE);

INSERT INTO order_items (id, order_id, product_id, quantity, price) VALUES
    (1, 1, 1, 1, 10.99),
    (2, 1, 5, 1, 88.99),
    (3, 2, 2, 1, 12.99),
    (4, 2, 6, 1, 32.99),
    (5, 2, 7, 1, 45.99),
    (6, 3, 8, 1, 33.99),
    (7, 3, 6, 1, 32.99);

INSERT INTO bookings (id, name, email, phone, address, service_type, date, time, frequency, notes, price, status, user_id) VALUES
    (1, 'Percy Masuhla', 'percy@cleanx.com', '+27 82 555 0101', '123 Main Street, Johannesburg', 'Deep Clean', '2025-03-01', '09:00', 'Weekly', 'Spring clean before guests arrive.', 1500.00, 'confirmed', 2),
    (2, 'Cynthia Ndlovu', 'cynthia@cleanx.com', '+27 83 234 8876', '45 Rivonia Road, Sandton', 'Move-out Clean', '2025-03-05', '13:00', 'Once-off', 'Need a full handover clean.', 2200.00, 'pending', 3),
    (3, 'Tshepo Makae', 'tshepo.un@gmail.com', '+27 82 000 1122', '67 Muckleneuk, Pretoria', 'Office Clean', '2025-03-12', '08:00', 'Monthly', 'Boardroom and reception detail.', 1800.00, 'completed', 1);

INSERT INTO service_order (id, full_name, email, phone, address, service_type, frequency, date, time_window, notes, price, status, created_at) VALUES
    (1, 'Percy Masuhla', 'percy@cleanx.com', '+27 82 555 0101', '123 Main Street, Johannesburg', 'Premium Deep Clean', 'Weekly', '2025-03-15', '08:00 - 10:00', 'Include carpet shampoo.', 1850.00, 'paid', '2025-02-20 08:30:00+02'),
    (2, 'Nomsa Nkosi', 'nomsa.nkosi@example.com', '+27 81 888 0099', '12 Summerfields Estate, Centurion', 'Post-Construction Clean', 'Once-off', '2025-03-22', '10:00 - 12:00', 'Focus on windows and floors.', 3200.00, 'scheduled', '2025-02-21 10:45:00+02');

-- Keep SERIAL/IDENTITY sequences in sync with the seeded ids
SELECT setval(pg_get_serial_sequence('users', 'id'),       COALESCE((SELECT MAX(id) FROM users), 0));
SELECT setval(pg_get_serial_sequence('products', 'id'),    COALESCE((SELECT MAX(id) FROM products), 0));
SELECT setval(pg_get_serial_sequence('orders', 'id'),      COALESCE((SELECT MAX(id) FROM orders), 0));
SELECT setval(pg_get_serial_sequence('order_items', 'id'), COALESCE((SELECT MAX(id) FROM order_items), 0));
SELECT setval(pg_get_serial_sequence('bookings', 'id'),    COALESCE((SELECT MAX(id) FROM bookings), 0));
SELECT setval(pg_get_serial_sequence('service_order', 'id'), COALESCE((SELECT MAX(id) FROM service_order), 0));
