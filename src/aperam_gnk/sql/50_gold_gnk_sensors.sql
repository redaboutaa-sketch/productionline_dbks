-- Gold: sensor health per tag and hour, and sensor profile per coil.
-- Join key between sensors and movements: timebased_sensor.Domain = metal_movements.Asset_Tree
-- (both ARMP level 3), and Timestamp within [StartDateTimeMachineSpecific, EndDateTimeMachineSpecific].
CREATE OR REPLACE VIEW {tgt}.gnk_sensor AS
SELECT
  s.TagId                         AS tag_id,
  s.Timestamp                     AS ts,
  s.Date                          AS dt,
  s.Domain                        AS armp_l3,
  try_cast(s.Value AS DOUBLE)     AS value,
  s.Value                         AS value_raw,
  s.Flag_ValueInRange             AS in_range,
  s.Flag_MasterDataExists         AS has_master_data
FROM {src}.timebased_sensor s
WHERE s.Domain LIKE 'GN-%';

CREATE OR REPLACE VIEW {tgt}.gold_gnk_sensor_health_hourly AS
SELECT
  armp_l3,
  tag_id,
  date_trunc('HOUR', ts)                                   AS hour,
  count(*)                                                 AS samples,
  sum(CASE WHEN in_range = false THEN 1 ELSE 0 END)        AS out_of_range,
  -- Speeds, forces and tensions read 0 when the line is idle and are then flagged out of range,
  -- this column keeps only non-zero readings, a cheap proxy for 'out of range while producing'.
  sum(CASE WHEN in_range = false AND value <> 0 THEN 1 ELSE 0 END) AS out_of_range_nonzero,
  avg(value)                                               AS avg_value,
  min(value)                                               AS min_value,
  max(value)                                               AS max_value,
  stddev(value)                                            AS std_value,
  sum(CASE WHEN value IS NULL THEN 1 ELSE 0 END)           AS non_numeric
FROM {tgt}.gnk_sensor
GROUP BY 1, 2, 3;

-- Materialised (not a view): the interval join is expensive on raw sensor volumes.
-- Refreshed by views.py / the scheduled job over a rolling look-back window.
CREATE OR REPLACE TABLE {tgt}.gold_gnk_sensor_by_coil AS
SELECT
  c.coil_legacy,
  c.coil_ids,
  c.line_armp,
  c.ts_start,
  c.ts_end,
  c.grade,
  c.thickness_mm,
  s.tag_id,
  count(*)                                                 AS samples,
  avg(s.value)                                             AS avg_value,
  min(s.value)                                             AS min_value,
  max(s.value)                                             AS max_value,
  stddev(s.value)                                          AS std_value,
  avg(CASE WHEN s.in_range = false THEN 1.0 ELSE 0.0 END)  AS out_of_range_ratio
FROM {tgt}.gold_gnk_coil_ops c
JOIN {tgt}.gnk_sensor s
  ON s.armp_l3 = c.armp_l3
 AND s.ts BETWEEN c.ts_start AND c.ts_end
WHERE c.ts_end >= {now_local} - INTERVAL {sensor_lookback_days} DAYS
  AND s.ts     >= {now_local} - INTERVAL {sensor_lookback_days} DAYS
GROUP BY c.coil_legacy, c.coil_ids, c.line_armp, c.ts_start, c.ts_end, c.grade, c.thickness_mm, s.tag_id;
