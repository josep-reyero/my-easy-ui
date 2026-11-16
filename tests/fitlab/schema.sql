CREATE TABLE IF NOT EXISTS "activities__rows" (
  "source" TEXT NOT NULL,
  "id" TEXT NOT NULL,
  "date" TEXT,
  "type" TEXT,
  "distance_m" REAL,
  "duration_s" REAL,
  "ascent_m" REAL,
  PRIMARY KEY ("source", "id")
);
CREATE INDEX IF NOT EXISTS "activities__rows_key" ON "activities__rows" ("id");
CREATE TABLE IF NOT EXISTS "days__rows" (
  "source" TEXT NOT NULL,
  "date" TEXT NOT NULL,
  "walk_km" REAL,
  "ride_km" REAL,
  "weather" TEXT,
  PRIMARY KEY ("source", "date")
);
CREATE INDEX IF NOT EXISTS "days__rows_key" ON "days__rows" ("date");
DROP VIEW IF EXISTS "activities";
CREATE VIEW "activities" AS
SELECT "source", "id", "date", "type", "distance_m", "duration_s", "ascent_m"
FROM "activities__rows"
WHERE "source" IN ('fitlab-walks', 'fitlab-rides');
DROP VIEW IF EXISTS "days";
CREATE VIEW "days" AS
SELECT
  (SELECT r."source" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "source",
  k."date" AS "date",
  (SELECT r."walk_km" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') AND r."walk_km" IS NOT NULL ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "walk_km",
  (SELECT r."ride_km" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') AND r."ride_km" IS NOT NULL ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "ride_km",
  (SELECT r."weather" FROM "days__rows" r WHERE r."date" = k."date" AND r."source" IN ('fitlab-walks', 'fitlab-rides') AND r."weather" IS NOT NULL ORDER BY CASE r."source" WHEN 'fitlab-rides' THEN 0 WHEN 'fitlab-walks' THEN 1 END LIMIT 1) AS "weather"
FROM (SELECT DISTINCT "date" FROM "days__rows" WHERE "source" IN ('fitlab-walks', 'fitlab-rides')) k;
DROP VIEW IF EXISTS "weekly";
CREATE VIEW "weekly" AS
SELECT strftime('%Y-%W', date) AS week, type, count(*) AS activities, round(sum(distance_m) / 1000.0, 1) AS km
FROM activities
GROUP BY 1, 2;
