-- Official OEE time model, from the daily indicators table {indicators} (one row per resource and day).
--
-- Decoded from the data (identities verified on real rows, see docs/aperam/README_GNK.md):
--   CT  calendar time        = SD_out + LT          SD_out = SDC + SDE + SDM + SDP + SDO1 + SDO2 (scheduled, outside loading)
--   LT  loading time         = SDO3 + SDO4 + TS     (column LT, seconds)
--   TS  service time         = FT + UOT + ROT
--   FT  failure time         = FTO + FTP + FTS + FTT + FTW ;  FTT = FTTE + FTTG + FTTI + FTTM
--   ROT real operating time ; UOT unproductive operating time
--   T0 = LT / CT  (loading rate)   T1 = ROT / LT  (operating rate on loading time, the time-based OEE)
--   MTTR = FT / N_FT (s)           MTBF = TS / N_FT (s)
-- Ratios are reported against CT, LT or TS (suffix divCT / divLT / divTS); absolute times are rebuilt here.
-- Duplicated loads of the same (resource, periode) keep the most recent _ingested_at.
CREATE OR REPLACE VIEW {tgt}.gnk_indicators_day AS
WITH src AS (
  SELECT *, row_number() OVER (PARTITION BY resource, periode ORDER BY _ingested_at DESC) AS rn
  FROM {indicators}
),
base AS (
  SELECT
    s.*,
    CAST(s.LT AS DOUBLE)                                                        AS lt_s,
    CAST(s.LT AS DOUBLE) * (1 - coalesce(CAST(s.SDO3plus4divLT AS DOUBLE), 0))  AS ts_s,
    CASE WHEN CAST(s.T0 AS DOUBLE) > 0 THEN CAST(s.LT AS DOUBLE) / CAST(s.T0 AS DOUBLE) END AS ct_s
  FROM src s
  WHERE s.rn = 1
)
SELECT
  b.resource,
  rm.line_armp,
  reg.line_name,
  to_date(b.periode)                                       AS prod_date,
  b.ct_s / 3600.0                                          AS calendar_h,
  b.lt_s / 3600.0                                          AS loading_h,
  b.ts_s / 3600.0                                          AS service_h,
  -- scheduled downtime outside loading time (h)
  coalesce(b.SDCdivCT, 0) * b.ct_s / 3600.0                AS sd_c_h,
  coalesce(b.SDEdivCT, 0) * b.ct_s / 3600.0                AS sd_e_h,
  coalesce(b.SDMdivCT, 0) * b.ct_s / 3600.0                AS sd_m_h,
  coalesce(b.SDPdivCT, 0) * b.ct_s / 3600.0                AS sd_p_h,
  coalesce(b.SDO1divCT, 0) * b.ct_s / 3600.0               AS sd_o1_h,
  coalesce(b.SDO2divCT, 0) * b.ct_s / 3600.0               AS sd_o2_h,
  -- organisational stops inside loading time (h)
  coalesce(b.SDO3divLT, 0) * b.lt_s / 3600.0               AS sd_o3_h,
  coalesce(b.SDO4divLT, 0) * b.lt_s / 3600.0               AS sd_o4_h,
  -- service time breakdown (h)
  coalesce(b.FTdivTS, 0)  * b.ts_s / 3600.0                AS failure_h,
  coalesce(b.FTOdivTS, 0) * b.ts_s / 3600.0                AS failure_o_h,
  coalesce(b.FTPdivTS, 0) * b.ts_s / 3600.0                AS failure_p_h,
  coalesce(b.FTSdivTS, 0) * b.ts_s / 3600.0                AS failure_s_h,
  coalesce(b.FTTdivTS, 0) * b.ts_s / 3600.0                AS failure_t_h,
  coalesce(b.FTTEdivTS, 0) * b.ts_s / 3600.0               AS failure_t_e_h,
  coalesce(b.FTTGdivTS, 0) * b.ts_s / 3600.0               AS failure_t_g_h,
  coalesce(b.FTTIdivTS, 0) * b.ts_s / 3600.0               AS failure_t_i_h,
  coalesce(b.FTTMdivTS, 0) * b.ts_s / 3600.0               AS failure_t_m_h,
  coalesce(b.FTWdivTS, 0) * b.ts_s / 3600.0                AS failure_w_h,
  coalesce(b.UOTdivTS, 0) * b.ts_s / 3600.0                AS unproductive_h,
  coalesce(b.ROTdivTS, 0) * b.ts_s / 3600.0                AS rot_h,
  -- ratios as reported
  CAST(b.T0 AS DOUBLE)                                     AS t0_loading_rate,
  CAST(b.T1 AS DOUBLE)                                     AS t1_rot_over_lt,
  CAST(b.ROTdivTS AS DOUBLE)                               AS rot_over_ts,
  CASE WHEN b.FTdivTS IS NOT NULL THEN 1 - CAST(b.FTdivTS AS DOUBLE) END AS availability_ts,
  CAST(b.N_FT AS INT)                                      AS n_failures,
  CAST(b.N_FUST AS INT)                                    AS n_fust,
  CAST(b.MTBF AS DOUBLE) / 3600.0                          AS mtbf_h,
  CAST(b.MTTR AS DOUBLE) / 60.0                            AS mttr_min,
  CAST(b.N_COIL AS INT)                                    AS n_coils,
  CAST(b.N_STRI AS INT)                                    AS n_stri,
  b._ingested_at                                           AS ingested_at
FROM base b
LEFT JOIN {tgt}.gnk_resource_map  rm  ON rm.resource = b.resource
LEFT JOIN {tgt}.gnk_line_registry reg ON reg.line_armp = rm.line_armp;

-- Loss tree, one row per (resource, day, loss), in hours: the input of every Pareto.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_time_losses_day AS
SELECT resource, line_armp, line_name, prod_date, level, loss_code, loss_h
FROM (
  SELECT resource, line_armp, line_name, prod_date,
    stack(17,
      'scheduled_outside_LT', 'SDC', sd_c_h,
      'scheduled_outside_LT', 'SDE', sd_e_h,
      'scheduled_outside_LT', 'SDM', sd_m_h,
      'scheduled_outside_LT', 'SDP', sd_p_h,
      'scheduled_outside_LT', 'SDO1', sd_o1_h,
      'scheduled_outside_LT', 'SDO2', sd_o2_h,
      'organisational_in_LT', 'SDO3', sd_o3_h,
      'organisational_in_LT', 'SDO4', sd_o4_h,
      'failure', 'FTO', failure_o_h,
      'failure', 'FTP', failure_p_h,
      'failure', 'FTS', failure_s_h,
      'failure', 'FTT-E', failure_t_e_h,
      'failure', 'FTT-G', failure_t_g_h,
      'failure', 'FTT-I', failure_t_i_h,
      'failure', 'FTT-M', failure_t_m_h,
      'failure', 'FTW', failure_w_h,
      'operating', 'UOT', unproductive_h
    ) AS (level, loss_code, loss_h)
  FROM {tgt}.gnk_indicators_day
)
WHERE loss_h > 0;

-- Full daily OEE: time model (ROT / LT) x speed ratio x metal yield from metal_movements.
-- Joined on line_armp and calendar date of coil end; performance / quality stay NULL when the
-- resource is not mapped to an ARMP line in gnk_resource_map.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_oee_day AS
WITH mm AS (
  SELECT line_armp, to_date(ts_end) AS prod_date,
         count(*) AS coils_mm, sum(weight_t) AS tonnes,
         CASE WHEN sum(speed_setpoint_m_min * length_m) > 0
              THEN sum(speed_realised_m_min * length_m) / sum(speed_setpoint_m_min * length_m) END AS speed_ratio,
         CASE WHEN sum(CASE WHEN weight_in_t > 0 THEN weight_in_t END) > 0
              THEN sum(CASE WHEN weight_in_t > 0 THEN weight_t END) / sum(CASE WHEN weight_in_t > 0 THEN weight_in_t END) END AS yield
  FROM {tgt}.gold_gnk_coil_ops
  GROUP BY 1, 2
)
SELECT
  i.resource, i.line_armp, i.line_name, i.prod_date,
  i.calendar_h, i.loading_h, i.service_h, i.rot_h, i.failure_h, i.unproductive_h,
  i.sd_o3_h + i.sd_o4_h                                    AS organisational_h,
  i.t0_loading_rate, i.t1_rot_over_lt, i.availability_ts, i.rot_over_ts,
  i.n_failures, i.mtbf_h, i.mttr_min, i.n_coils,
  mm.tonnes, mm.speed_ratio, mm.yield,
  i.t1_rot_over_lt
    * CASE WHEN mm.speed_ratio IS NULL THEN 1.0 ELSE least(mm.speed_ratio, 1.05) END
    * CASE WHEN mm.yield IS NULL THEN 1.0 ELSE least(mm.yield, 1.0) END AS oee,
  (mm.speed_ratio IS NOT NULL OR mm.yield IS NOT NULL)     AS oee_includes_speed_and_yield
FROM {tgt}.gnk_indicators_day i
LEFT JOIN mm ON mm.line_armp = i.line_armp AND mm.prod_date = i.prod_date;
