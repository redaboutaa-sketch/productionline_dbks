-- Gold: one row per executed operation of a coil (Metal) on a production line.
-- Slitting / cut-to-length split one Metal into several MetalParts: the parts of the
-- same run are aggregated back so that weight in / weight out compare like with like.
-- The previous executed operation of the same Metal gives the ENTRY state of the coil
-- (entry thickness, entry weight) -> thickness reduction and metal yield per operation.
--
-- Assumption to validate with the data owners: `Metal` is stable along the cold route
-- (black coil -> packing). If the identifier changes at a given line, chain it through
-- the genealogy table instead of `metal` here.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_coil_ops AS
WITH runs AS (
  SELECT
    metal,
    line_armp,
    first(line_name)                        AS line_name,
    first(stage)                            AS stage,
    first(kind)                             AS kind,
    concat_ws(',', collect_set(machine_code)) AS machine_codes,
    first(machine_indication, true)         AS machine_indication,
    first(armp_l3, true)                    AS armp_l3,
    min(ts_start)                           AS ts_start,
    max(ts_end)                             AS ts_end,
    max(elapsed_min)                        AS elapsed_min,
    max(rot_min)                            AS rot_min,
    first(grade, true)                      AS grade,
    first(finish_code, true)                AS finish_code,
    first(heat, true)                       AS heat,
    avg(speed_setpoint_m_min)               AS speed_setpoint_m_min,
    avg(speed_realised_m_min)               AS speed_realised_m_min,
    max(length_m)                           AS length_m,
    sum(width_mm)                           AS width_mm_total,
    max(width_mm)                           AS width_mm_max,
    avg(thickness_mm)                       AS thickness_mm,
    sum(weight_t)                           AS weight_t,
    count(*)                                AS parts_out,
    max(passes_planned)                     AS passes_planned,
    max(passes_done)                        AS passes_done,
    max(shift_date)                         AS shift_date,
    max_by(shift_name, ts_end)              AS shift_name
  FROM {tgt}.gnk_movements
  WHERE op_status = 'EXEC' AND NOT is_logistic
  GROUP BY metal, line_armp, date_trunc('MINUTE', ts_start)
)
SELECT
  r.*,
  lag(r.line_armp)    OVER w AS prev_line_armp,
  lag(r.thickness_mm) OVER w AS thickness_in_mm,
  lag(r.weight_t)     OVER w AS weight_in_t,
  lag(r.ts_end)       OVER w AS prev_ts_end,
  CASE WHEN lag(r.thickness_mm) OVER w > 0
       THEN 100.0 * (1 - r.thickness_mm / lag(r.thickness_mm) OVER w) END   AS thickness_reduction_pct,
  CASE WHEN lag(r.weight_t) OVER w > 0
       THEN 100.0 * r.weight_t / lag(r.weight_t) OVER w END                  AS yield_pct,
  timestampdiff(SECOND, lag(r.ts_end) OVER w, r.ts_start) / 3600.0           AS wait_before_h,
  CASE WHEN r.rot_min > 0 THEN r.weight_t / (r.rot_min / 60.0) END           AS throughput_t_per_rot_h,
  CASE WHEN r.speed_setpoint_m_min > 0
       THEN r.speed_realised_m_min / r.speed_setpoint_m_min END              AS speed_ratio
FROM runs r
WINDOW w AS (PARTITION BY r.metal ORDER BY r.ts_start);
