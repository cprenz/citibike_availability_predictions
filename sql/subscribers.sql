CREATE TABLE IF NOT EXISTS subscribers (
    id              SERIAL PRIMARY KEY,
    email           TEXT,
    phone           TEXT,
    station_id      VARCHAR(50),
    horizon_minutes INTEGER,
    threshold       FLOAT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT subscribers_contact_check CHECK (email IS NOT NULL OR phone IS NOT NULL)
);
