-- Schema for cleaned NYC 311 heat / hot-water complaints.
-- One row per 311 service request that passed the reliability checks in etl.py.

DROP TABLE IF EXISTS complaints;
CREATE TABLE complaints (
    unique_key       TEXT PRIMARY KEY,
    created_date     TEXT NOT NULL,      -- ISO-8601 timestamp
    closed_date      TEXT,               -- NULL if open or if it failed the date-order check
    complaint_type   TEXT NOT NULL,
    descriptor       TEXT,
    incident_address TEXT,
    borough          TEXT,
    incident_zip     TEXT,
    bbl              TEXT NOT NULL,      -- 10-digit Borough-Block-Lot building identifier
    latitude         REAL,
    longitude        REAL,
    status           TEXT,
    geo_flag         INTEGER NOT NULL DEFAULT 0  -- 1 = coordinates missing or outside NYC
);

CREATE INDEX idx_complaints_bbl_date ON complaints (bbl, created_date);

-- Audit trail: every reliability check, how many rows it touched, and what was done.
DROP TABLE IF EXISTS data_quality_log;
CREATE TABLE data_quality_log (
    run_at      TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    n_rows      INTEGER NOT NULL,
    action      TEXT NOT NULL
);
