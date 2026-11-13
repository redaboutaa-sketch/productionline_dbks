-- Gold: material flow - where every piece (coil_id = ProductId_Legacy) is now, and the queues (WIP buffers)
-- in front of each line.
-- This is the GNK counterpart of the upstream cascade "buffers": a line whose queue empties
-- will starve, a line whose output queue keeps growing is blocked downstream.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_coil_position AS
WITH last_exec AS (
  SELECT coil_id, coil_legacy, line_armp, line_name, workshop, stage, ts_end, weight_t, grade, finish_code,
         thickness_mm, width_mm,
         row_number() OVER (PARTITION BY coil_id ORDER BY ts_end DESC) AS rn
  FROM {tgt}.gnk_movements
  WHERE op_status = 'EXEC' AND ts_end IS NOT NULL
),
pending AS (
  SELECT coil_id, line_armp, line_name, op_status, route_rank,
         row_number() OVER (PARTITION BY coil_id ORDER BY route_rank NULLS LAST, stage) AS rn
  FROM {tgt}.gnk_movements
  WHERE op_status IN ('COUR', 'DISP', 'NDIS', 'BLOQ')
)
SELECT
  l.coil_id,
  l.coil_legacy,
  l.grade,
  l.finish_code,
  l.thickness_mm,
  l.width_mm,
  round(l.weight_t, 2)                                       AS weight_t,
  l.line_armp                                                AS last_line_armp,
  l.line_name                                                AS last_line_name,
  l.ts_end                                                   AS last_exec_end,
  p.line_armp                                                AS next_line_armp,
  p.line_name                                                AS next_line_name,
  p.op_status                                                AS next_status,
  CASE WHEN p.op_status = 'COUR' THEN 'on_line'
       WHEN p.op_status = 'BLOQ' THEN 'blocked'
       WHEN p.op_status IN ('DISP', 'NDIS') THEN 'waiting'
       WHEN l.stage >= 100 THEN 'finished'
       WHEN l.workshop = 'ST' THEN 'to_hot_rolling'
       ELSE 'unplanned' END                                  AS position_state,
  round(timestampdiff(MINUTE, l.ts_end, {now_local}) / 60.0, 1) AS age_h
FROM last_exec l
LEFT JOIN pending p ON p.coil_id = l.coil_id AND p.rn = 1
WHERE l.rn = 1;

CREATE OR REPLACE VIEW {tgt}.gold_gnk_wip_buffers AS
SELECT
  next_line_armp                                             AS line_armp,
  first(next_line_name)                                      AS line_name,
  count(*)                                                   AS coils_waiting,
  round(sum(weight_t), 1)                                    AS tonnes_waiting,
  sum(CASE WHEN position_state = 'blocked' THEN 1 ELSE 0 END) AS coils_blocked,
  round(sum(CASE WHEN position_state = 'blocked' THEN weight_t ELSE 0 END), 1) AS tonnes_blocked,
  round(avg(age_h), 1)                                       AS avg_age_h,
  round(max(age_h), 1)                                       AS max_age_h
FROM {tgt}.gold_gnk_coil_position
WHERE position_state IN ('waiting', 'blocked')
GROUP BY next_line_armp;

-- Gaps between consecutive coils on the same line: the stops metal_movements can see.
-- A gap is idle time with no coil on the line (no order, breakdown, roll change, planned stop...).
-- The in-coil stop time is elapsed_min - rot_min on gold_gnk_coil_ops.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_line_gaps AS
WITH seq AS (
  SELECT line_armp, line_name, coil_legacy, ts_start, ts_end, shift_date, shift_name,
         lag(ts_end)      OVER (PARTITION BY line_armp ORDER BY ts_start) AS prev_end,
         lag(coil_legacy) OVER (PARTITION BY line_armp ORDER BY ts_start) AS prev_coil
  FROM {tgt}.gold_gnk_coil_ops
)
SELECT
  line_armp, line_name, prev_coil, coil_legacy AS next_coil,
  prev_end AS gap_start, ts_start AS gap_end,
  round(timestampdiff(SECOND, prev_end, ts_start) / 60.0, 1) AS gap_min,
  shift_date, shift_name
FROM seq
WHERE prev_end IS NOT NULL
  AND timestampdiff(SECOND, prev_end, ts_start) >= 15 * 60;
