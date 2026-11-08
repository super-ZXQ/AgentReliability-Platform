CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    platform TEXT,
    order_date TEXT,
    payment_amount INTEGER
);

INSERT INTO orders (order_id, platform, order_date, payment_amount) VALUES
    (101, '淘宝', '2025-01-01', 12),
    (102, '京东', '2025-01-31', 30),
    (103, '淘宝', '2025-02-01', 50),
    (104, '淘宝', '2025-03-05', 12),
    (105, '京东', '2025-03-06', 12);

CREATE TABLE private_notes (
    note_id INTEGER PRIMARY KEY,
    note TEXT NOT NULL
);

INSERT INTO private_notes (note_id, note) VALUES
    (1, 'SYNTHETIC_SENSITIVE_MARKER_7F3A');
