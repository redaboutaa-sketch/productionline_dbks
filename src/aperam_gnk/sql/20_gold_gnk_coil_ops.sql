-- Gold: one row per executed operation run of a coil on a production line.
-- Grain: coil_legacy (Metal_Legacy) x line x run (start minute). The pieces (coil_id = ProductId_Legacy)
-- produced in the run are listed in coil_ids and re-aggregated, so weight in / weight out compare like
-- with like even when a slitter or cut-to-length line splits the coil.
--
-- Entry state of a run (thickness / weight in):
--   * pieces that keep their coil_id: the previous operation of the SAME coil_id (exact, piece level);
--   * pieces created by the run (split -> new part numbers): the previous run of the coil (coil level).
CREATE OR REPLACE VIEW {tgt}.gold_gnk_coil_ops AS
WITH pieces AS (
  SELECT
    coil_id,
    coil_legacy,
    line_armp,
    date_trunc('MINUTE', ts_start)          AS run_key,
    first(metal, true)                      AS metal,
    first(line_name)                        AS line_name,
    first(stage)                            AS stage,
    first(kind)                             AS kind,
    first(armp_l3, true)                    AS armp_l3,
    first(machine_code)                     AS machine_code,
    first(machine_indication, true)         AS machine_indication,
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
    sum(width_mm)                           AS width_mm,
    avg(thickness_mm)                       AS thickness_mm,
    sum(weight_t)                           AS weight_t,
    max(passes_planned)                     AS passes_planned,
    max(passes_done)                        AS passes_done,
    max(shift_date)                         AS shift_date,
    max_by(shift_name, ts_end)              AS shift_name
  FROM {tgt}.gnk_movements
  WHERE op_status = 'EXEC' AND NOT is_logistic
  GROUP BY coil_id, coil_legacy, line_armp, date_trunc('MINUTE', ts_start)
),
pieces_prev AS (
  SELECT p.*,
         lag(p.weight_t)     OVER (PARTITION BY p.coil_id ORDER BY p.ts_start) AS piece_weight_in_t,
         lag(p.thickness_mm) OVER (PARTITION BY p.coil_id ORDER BY p.ts_start) AS piece_thickness_in_mm
  FROM pieces p
),
runs AS (
  SELECT
    coil_legacy,
    line_armp,
    run_key,
    first(metal, true)                      AS metal,
    first(line_name)                        AS line_name,
    first(stage)                            AS stage,
    first(kind)                             AS kind,
    first(armp_l3, true)                    AS armp_l3,
    concat_ws(',', collect_set(machine_code)) AS machine_codes,
    first(machine_indication, true)         AS machine_indication,
    concat_ws(',', sort_array(collect_set(coil_id))) AS coil_ids,
    count(*)                                AS parts_out,
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
    avg(thickness_mm)                       AS thickness_mm,
    sum(weight_t)                           AS weight_t,
    max(passes_planned)                     AS passes_planned,
    max(passes_done)                        AS passes_done,
    max(shift_date)                         AS shift_date,
    max_by(shift_name, ts_end)              AS shift_name,
    -- piece-level entry state only when EVERY piece of the run existed before (no split)
    CASE WHEN count(piece_weight_in_t) = count(*) THEN sum(piece_weight_in_t) END   AS piece_weight_in_t,
    CASE WHEN count(piece_weight_in_t) = count(*) THEN avg(piece_thickness_in_mm) END AS piece_thickness_in_mm
  FROM pieces_prev
  GROUP BY coil_legacy, line_armp, run_key
),
linked AS (
  SELECT
    r.*,
    lag(r.line_armp)    OVER w AS prev_line_armp,
    lag(r.ts_end)       OVER w AS prev_ts_end,
    coalesce(r.piece_thickness_in_mm, lag(r.thickness_mm) OVER w) AS thickness_in_mm,
    coalesce(r.piece_weight_in_t,     lag(r.weight_t) OVER w)     AS weight_in_t
  FROM runs r
  WINDOW w AS (PARTITION BY r.coil_legacy ORDER BY r.ts_start)
)
SELECT
  l.*,
  CASE WHEN l.thickness_in_mm > 0 THEN 100.0 * (1 - l.thickness_mm / l.thickness_in_mm) END AS thickness_reduction_pct,
  CASE WHEN l.weight_in_t > 0 THEN 100.0 * l.weight_t / l.weight_in_t END               AS yield_pct,
  timestampdiff(SECOND, l.prev_ts_end, l.ts_start) / 3600.0                            AS wait_before_h,
  CASE WHEN l.rot_min > 0 THEN l.weight_t / (l.rot_min / 60.0) END                     AS throughput_t_per_rot_h,
  CASE WHEN l.speed_setpoint_m_min > 0
       THEN l.speed_realised_m_min / l.speed_setpoint_m_min END                       AS speed_ratio
FROM linked l;
