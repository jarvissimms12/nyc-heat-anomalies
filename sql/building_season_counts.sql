-- Complaint-days per building per heating season.
--
-- Heating season (NYC Housing Maintenance Code): October 1 - May 31.
-- A season is labeled by the year it starts, e.g. Oct 2024 - May 2025 = 2024.
--
-- Why complaint-DAYS rather than raw complaints: a single outage in a large
-- building can generate dozens of calls from different tenants on the same day.
-- Counting distinct days with >= 1 complaint measures how often a building has a
-- problem, and keeps building size from dominating the count.

WITH in_season AS (
    SELECT
        bbl,
        borough,
        DATE(created_date) AS complaint_day,
        CAST(strftime('%Y', created_date) AS INTEGER)
          - CASE WHEN CAST(strftime('%m', created_date) AS INTEGER) < 10 THEN 1 ELSE 0 END
          AS season
    FROM complaints
    WHERE CAST(strftime('%m', created_date) AS INTEGER) IN (10, 11, 12, 1, 2, 3, 4, 5)
)
SELECT
    bbl,
    MAX(borough)                  AS borough,
    season,
    COUNT(DISTINCT complaint_day) AS complaint_days,
    COUNT(*)                      AS raw_complaints
FROM in_season
GROUP BY bbl, season
ORDER BY bbl, season;
