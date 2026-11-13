-- Official OEE time model (Aperam "OEE DWH" definition) from the daily indicators table {indicators}.
-- One row per resource and CALENDAR day (00:00-24:00). The table holds several sites: only resources
-- mapped to site 'GNK' in gnk_resource_map are kept (see gnk_indicators_resources for the full list).
--
-- Aperam time segmentation (TUC codes):
--   T0 band, scheduled downtime outside loading time:
--     SDC plant shutdown, SDM no material, SDE no electricity, SDO1 annual maintenance, SDO2 capital investments
--   T1 band, inside loading time (LT):
--     SDO3 periodic maintenance, SDO4 cleaning, SDP no staff,
--     FT failure time = FTT technical + FTP product/quality + FTO operator + FTS staff + FTW waiting,
--     UOT standard unproductive operating time (changeovers, loading...), ROT real operating time
--   T0 = LT / CT (opening)   T1 = ROT / LT (availability)
--   T2 = operating speed / design speed (performance)   T3 = good products / total output (quality)
--   OEE = T1 x T2 x T3 ;  Aperam capacity OEE = T0 x T1 x T2 x T3
-- Verified on the data: TS = FT + UOT + ROT, TS = LT - SDO3 - SDO4 (SDP assumed inside LT as well, per the
-- definition: no row with SDP was available to check), T1 = ROT / LT, MTTR = FT / N_FT, MTBF = TS / N_FT.
-- FTT sub-codes FTTE / FTTG / FTTI / FTTM, N_FUST ('# Furnace Stops', provisional) and N_STRI ('# Stringers')
-- have no validated definition: they are carried as-is.
CREATE OR REPLACE VIEW {tgt}.gnk_indicators_resources AS
SELECT
  s.resource,
  rm.site,
  rm.line_armp,
  rm.armp_md,
  rm.comment,
  min(to_date(s.periode)) AS first_day,
  max(to_date(s.periode)) AS last_day,
  count(DISTINCT s.periode) AS days
FROM {indicators} s
LEFT JOIN {tgt}.gnk_resource_map rm ON rm.resource = s.resource
GROUP BY s.resource, rm.site, rm.line_armp, rm.armp_md, rm.comment;

CREATE OR REPLACE VIEW {tgt}.gnk_indicators_day AS
WITH src AS (
  SELECT *, row_number() OVER (PARTITION BY resource, periode ORDER BY _ingested_at DESC) AS rn
  FROM {indicators}
),
base AS (
  SELECT
    s.*,
    CAST(s.LT AS DOUBLE)                                                        AS lt_s,
    CAST(s.LT AS DOUBLE) * (1 - coalesce(CAST(s.SDO3plus4divLT AS DOUBLE), 0)
                              - coalesce(CAST(s.SDPdivLT AS DOUBLE), 0))         AS ts_s,
    CASE WHEN CAST(s.T0 AS DOUBLE) > 0 THEN CAST(s.LT AS DOUBLE) / CAST(s.T0 AS DOUBLE)
         ELSE 86400.0 END                                                       AS ct_s
  FROM src s
  WHERE s.rn = 1
)
SELECT
  b.resource,
  rm.site,
  rm.line_armp,
  rm.armp_md,
  reg.line_name,
  to_date(b.periode)                                       AS prod_date,
  b.ct_s / 3600.0                                          AS calendar_h,
  b.lt_s / 3600.0                                          AS loading_h,
  b.ts_s / 3600.0                                          AS service_h,
  -- T0 band: scheduled downtime outside loading time (h)
  coalesce(b.SDCdivCT, 0)  * b.ct_s / 3600.0               AS sdc_plant_shutdown_h,
  coalesce(b.SDMdivCT, 0)  * b.ct_s / 3600.0               AS sdm_no_material_h,
  coalesce(b.SDEdivCT, 0)  * b.ct_s / 3600.0               AS sde_no_electricity_h,
  coalesce(b.SDO1divCT, 0) * b.ct_s / 3600.0               AS sdo1_annual_maintenance_h,
  coalesce(b.SDO2divCT, 0) * b.ct_s / 3600.0               AS sdo2_capital_investments_h,
  -- T1 band: scheduled downtime inside loading time (h)
  coalesce(b.SDO3divLT, 0) * b.lt_s / 3600.0               AS sdo3_periodic_maintenance_h,
  coalesce(b.SDO4divLT, 0) * b.lt_s / 3600.0               AS sdo4_cleaning_h,
  coalesce(b.SDPdivLT, 0)  * b.lt_s / 3600.0               AS sdp_no_staff_h,
  -- T1 band: failure time by category (h)
  coalesce(b.FTdivTS, 0)   * b.ts_s / 3600.0               AS ft_h,
  coalesce(b.FTTdivTS, 0)  * b.ts_s / 3600.0               AS ftt_technical_h,
  coalesce(b.FTPdivTS, 0)  * b.ts_s / 3600.0               AS ftp_product_quality_h,
  coalesce(b.FTOdivTS, 0)  * b.ts_s / 3600.0               AS fto_operator_h,
  coalesce(b.FTSdivTS, 0)  * b.ts_s / 3600.0               AS fts_staff_h,
  coalesce(b.FTWdivTS, 0)  * b.ts_s / 3600.0               AS ftw_waiting_h,
  -- FTT sub-codes: no validated definition, kept under their codes
  coalesce(b.FTTEdivTS, 0) * b.ts_s / 3600.0               AS ftte_h,
  coalesce(b.FTTGdivTS, 0) * b.ts_s / 3600.0               AS fttg_h,
  coalesce(b.FTTIdivTS, 0) * b.ts_s / 3600.0               AS ftti_h,
  coalesce(b.FTTMdivTS, 0) * b.ts_s / 3600.0               AS fttm_h,
  coalesce(b.UOTdivTS, 0)  * b.ts_s / 3600.0               AS uot_unproductive_h,
  coalesce(b.ROTdivTS, 0)  * b.ts_s / 3600.0               AS rot_h,
  -- ratios as reported
  CAST(b.T0 AS DOUBLE)                                     AS t0_opening,
  CAST(b.T1 AS DOUBLE)                                     AS t1_availability,
  CAST(b.ROTdivTS AS DOUBLE)                               AS rot_div_ts,
  CAST(b.N_FT AS INT)                                      AS n_ft,
  CAST(b.N_FUST AS INT)                                    AS n_fust,
  CAST(b.MTBF AS DOUBLE) / 3600.0                          AS mtbf_h,
  CAST(b.MTTR AS DOUBLE) / 60.0                            AS mttr_min,
  CAST(b.N_COIL AS INT)                                    AS n_coil,
  CAST(b.N_STRI AS INT)                                    AS n_stri,
  -- data check: FT/TS + UOT/TS + ROT/TS should be 1
  coalesce(b.FTdivTS, 0) + coalesce(b.UOTdivTS, 0) + coalesce(b.ROTdivTS, 0) - 1 AS ts_identity_gap,
  b._ingested_at                                           AS ingested_at
FROM base b
JOIN {tgt}.gnk_resource_map rm ON rm.resource = b.resource AND rm.site = 'GNK'
LEFT JOIN {tgt}.gnk_line_registry reg ON reg.line_armp = rm.line_armp;

-- Loss tree, one row per (resource, day, TUC code), in hours, with the Aperam label: the input of every Pareto.
CREATE OR REPLACE VIEW {tgt}.gold_gnk_time_losses_day AS
SELECT resource, line_armp, line_name, prod_date, band, tuc_code, tuc_label, loss_h
FROM (
  SELECT resource, line_armp, line_name, prod_date,
    stack(18,
      'T0_scheduled_downtime', 'SDC',  'Plant shutdown',        sdc_plant_shutdown_h,
      'T0_scheduled_downtime', 'SDM',  'No material',           sdm_no_material_h,
      'T0_scheduled_downtime', 'SDE',  'No electricity',        sde_no_electricity_h,
      'T0_scheduled_downtime', 'SDO1', 'Annual maintenance',    sdo1_annual_maintenance_h,
      'T0_scheduled_downtime', 'SDO2', 'Capital investments',   sdo2_capital_investments_h,
      'T1_scheduled_in_LT',    'SDO3', 'Periodic maintenance',  sdo3_periodic_maintenance_h,
      'T1_scheduled_in_LT',    'SDO4', 'Cleaning',              sdo4_cleaning_h,
      'T1_scheduled_in_LT',    'SDP',  'No staff',              sdp_no_staff_h,
      'T1_failure',            'FTTE', 'FTT sub-code (definition pending)', ftte_h,
      'T1_failure',            'FTTG', 'FTT sub-code (definition pending)', fttg_h,
      'T1_failure',            'FTTI', 'FTT sub-code (definition pending)', ftti_h,
      'T1_failure',            'FTTM', 'FTT sub-code (definition pending)', fttm_h,
      'T1_failure',            'FTT_other', 'Technical, not split in sub-codes',
                               greatest(ftt_technical_h - ftte_h - fttg_h - ftti_h - fttm_h, 0),
      'T1_failure',            'FTP',  'Product / quality',     ftp_product_quality_h,
      'T1_failure',            'FTO',  'Operator',              fto_operator_h,
      'T1_failure',            'FTS',  'Staff',                 fts_staff_h,
      'T1_failure',            'FTW',  'Waiting',               ftw_waiting_h,
      'T1_unproductive',       'UOT',  'Standard unproductive operating time (changeovers, loading...)',
                               uot_unproductive_h
    ) AS (band, tuc_code, tuc_label, loss_h)
  FROM {tgt}.gnk_indicators_day
)
WHERE loss_h > 1e-6;

-- Daily OEE per the Aperam definition. A resource is a local machine code (metal_movements.Machine), so T2 / T3
-- are joined on the machine code (BS2L and BS2Z stay separate) and the calendar date of coil end. T0 and T1 are official (indicators table). T2 and T3 are PROXIES
-- from metal_movements until design speeds and quality outputs are connected:
--   t2_proxy = SpeedRealised / SpeedSetpoint (setpoint, not design speed: slowdowns vs setpoint only,
--              mix impact not captured, so T2 is overestimated)
--   t3_proxy = metal yield, weight out / weight in (not good products / total output)
CREATE OR REPLACE VIEW {tgt}.gold_gnk_oee_day AS
WITH mm AS (
  SELECT machine_codes AS machine_code, to_date(ts_end) AS prod_date,
         count(*) AS coils_mm, sum(weight_t) AS tonnes,
         CASE WHEN sum(speed_setpoint_m_min * length_m) > 0
              THEN sum(speed_realised_m_min * length_m) / sum(speed_setpoint_m_min * length_m) END AS t2_proxy,
         CASE WHEN sum(CASE WHEN weight_in_t > 0 THEN weight_in_t END) > 0
              THEN sum(CASE WHEN weight_in_t > 0 THEN weight_t END) / sum(CASE WHEN weight_in_t > 0 THEN weight_in_t END) END AS t3_proxy
  FROM {tgt}.gold_gnk_coil_ops
  WHERE machine_codes NOT LIKE '%,%'
  GROUP BY 1, 2
),
j AS (
  SELECT
    i.resource, i.line_armp, i.line_name, i.prod_date,
    i.calendar_h, i.loading_h, i.service_h, i.rot_h, i.ft_h, i.uot_unproductive_h,
    i.sdo3_periodic_maintenance_h + i.sdo4_cleaning_h + i.sdp_no_staff_h AS scheduled_in_lt_h,
    i.t0_opening, i.t1_availability, i.n_ft, i.n_fust, i.mtbf_h, i.mttr_min, i.n_coil, i.n_stri,
    mm.tonnes,
    -- least() skips NULLs in Spark, hence the CASEs
    CASE WHEN mm.t2_proxy IS NULL THEN NULL ELSE least(mm.t2_proxy, 1.05) END AS t2_proxy,
    CASE WHEN mm.t3_proxy IS NULL THEN NULL ELSE least(mm.t3_proxy, 1.0) END  AS t3_proxy
  FROM {tgt}.gnk_indicators_day i
  LEFT JOIN mm ON mm.machine_code = i.resource AND mm.prod_date = i.prod_date
)
SELECT
  j.*,
  j.t1_availability * coalesce(j.t2_proxy, 1.0) * coalesce(j.t3_proxy, 1.0)                  AS oee_t1t2t3,
  j.t0_opening * j.t1_availability * coalesce(j.t2_proxy, 1.0) * coalesce(j.t3_proxy, 1.0)   AS oee_t0t1t2t3,
  (j.t2_proxy IS NOT NULL AND j.t3_proxy IS NOT NULL)                                         AS oee_has_t2_t3
FROM j;
