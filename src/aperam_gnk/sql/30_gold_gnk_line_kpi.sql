-- Gold: line KPIs per shift and per day (the GNK equivalent of the upstream line_kpi).
--
-- OEE proxy built from what metal_movements can actually prove:
--   utilisation   = time with a coil on the line / calendar time        (loading)
--   run_efficiency= DurationROT / time with a coil on the line          (stops inside a coil)
--   speed_ratio   = SpeedRealised / SpeedSetpoint, length-weighted       (performance)
--   yield         = weight out / weight in from the previous operation   (quality / metal loss)
--   oee_proxy     = utilisation x run_efficiency x speed_ratio x yield
-- Planned stops (maintenance, roll changes, no order) are NOT separated here: they need the
-- maintenance / stop-reason source. Until then utilisation includes planned downtime.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_line_kpi_shift AS
SELECT
  line_armp,
  first(line_name)                                         AS line_name,
  first(stage)                                             AS stage,
  first(kind)                                              AS kind,
  shift_date,
  shift_name,
  count(*)                                                 AS coils,
  round(sum(weight_t), 1)                                  AS tonnes,
  round(sum(elapsed_min) / 60.0, 2)                        AS busy_h,
  round(sum(rot_min) / 60.0, 2)                            AS rot_h,
  least(1.0, sum(elapsed_min) / 480.0)                     AS utilisation,
  CASE WHEN sum(elapsed_min) > 0 THEN least(1.0, sum(rot_min) / sum(elapsed_min)) END AS run_efficiency,
  CASE WHEN sum(speed_setpoint_m_min * length_m) > 0
       THEN sum(speed_realised_m_min * length_m) / sum(speed_setpoint_m_min * length_m) END AS speed_ratio,
  CASE WHEN sum(CASE WHEN weight_in_t > 0 THEN weight_in_t END) > 0
       THEN sum(CASE WHEN weight_in_t > 0 THEN weight_t END) / sum(CASE WHEN weight_in_t > 0 THEN weight_in_t END) END AS yield,
  CASE WHEN sum(rot_min) > 0 THEN sum(weight_t) / (sum(rot_min) / 60.0) END AS throughput_t_per_rot_h,
  avg(thickness_mm)                                        AS avg_thickness_mm,
  avg(thickness_reduction_pct)                             AS avg_thickness_reduction_pct
FROM {tgt}.gold_gnk_coil_ops
GROUP BY line_armp, shift_date, shift_name;

CREATE OR REPLACE VIEW {tgt}.gold_gnk_line_kpi_shift_oee AS
-- Batch annealing (bell furnaces) holds many coils in parallel for ~30 h: a line-occupancy
-- OEE is meaningless there, so it is left NULL. Note Spark's least() skips NULLs, hence the CASEs.
SELECT
  *,
  CASE WHEN kind = 'batch_annealing' THEN NULL ELSE
    utilisation
    * coalesce(run_efficiency, 1.0)
    * CASE WHEN speed_ratio IS NULL THEN 1.0 ELSE least(speed_ratio, 1.05) END
    * CASE WHEN yield IS NULL THEN 1.0 ELSE least(yield, 1.0) END
  END AS oee_proxy
FROM {tgt}.gold_gnk_line_kpi_shift;

CREATE OR REPLACE VIEW {tgt}.gold_gnk_line_kpi_daily AS
SELECT
  line_armp,
  first(line_name)                                         AS line_name,
  first(stage)                                             AS stage,
  shift_date                                               AS prod_date,
  sum(coils)                                               AS coils,
  round(sum(tonnes), 1)                                    AS tonnes,
  round(sum(busy_h), 2)                                    AS busy_h,
  round(sum(rot_h), 2)                                     AS rot_h,
  least(1.0, sum(busy_h) / 24.0)                           AS utilisation,
  CASE WHEN sum(busy_h) > 0 THEN least(1.0, sum(rot_h) / sum(busy_h)) END AS run_efficiency,
  CASE WHEN sum(rot_h) > 0 THEN sum(tonnes) / sum(rot_h) END AS throughput_t_per_rot_h
FROM {tgt}.gold_gnk_line_kpi_shift
GROUP BY line_armp, shift_date;

-- Demonstrated rate: what each line has actually proven it can do, per thickness class,
-- over the last 90 days. Used as the performance reference where the site poster gives none.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_line_reference_rate AS
SELECT
  line_armp,
  CASE WHEN thickness_mm < 1 THEN '<1mm'
       WHEN thickness_mm < 2 THEN '1-2mm'
       WHEN thickness_mm < 4 THEN '2-4mm'
       ELSE '>=4mm' END                                    AS thickness_class,
  count(*)                                                 AS coils,
  percentile_approx(throughput_t_per_rot_h, 0.5)           AS p50_t_per_h,
  percentile_approx(throughput_t_per_rot_h, 0.9)           AS p90_t_per_h,
  percentile_approx(speed_realised_m_min, 0.9)             AS p90_speed_m_min
FROM {tgt}.gold_gnk_coil_ops
WHERE ts_end >= current_timestamp() - INTERVAL 90 DAYS
  AND throughput_t_per_rot_h IS NOT NULL
GROUP BY 1, 2;
