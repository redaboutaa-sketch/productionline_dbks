-- Silver: GNK metal movements, typed, mapped to the ARMP line registry and to a shift.
-- Source  : {src}.metal_movements  (Site_FK = 3)
-- Depends : {tgt}.gnk_machine_map, {tgt}.gnk_line_registry  (written by views.py)
--
-- Conventions
--   * weight_t        = Weight / 1000 (source is kg)
--   * elapsed_min     = wall-clock time on the line (End - Start)
--   * rot_min         = DurationROT, real operation time without stops
--   * shift_*         = attributed on the END timestamp (production is booked when the coil leaves the line)
--   * Packing machines only count OperationNameLocal IN ('PCKI','PCKG','PCK') (see mapping comments)
--   * Coil keys (Genk, validated on one end-to-end example, see docs/aperam/README_GNK.md):
--       coil_id     = ProductId_Legacy = year + 7-digit coil number + part number. Stable from black-coil
--                     reception to packing for one physical piece: THE tracking key.
--       coil_legacy = Metal_Legacy = year + coil number: groups the parts (pieces) of one coil.
--     ProductId changes at every operation, MetalPart alone is not unique, Metal (UDM) is informational.
CREATE OR REPLACE VIEW {tgt}.gnk_movements AS
SELECT
  mm.ProductId_Legacy                            AS coil_id,
  mm.Metal_Legacy                                AS coil_legacy,
  mm.ProductId                                   AS product_id,
  mm.Metal                                       AS metal,
  mm.MetalPart                                   AS metal_part,
  mm.MetalHeat                                   AS heat,
  mm.Machine                                     AS machine_code,
  mm.Machine_Indication                          AS machine_indication,
  mm.Asset_Tree                                  AS armp_l3,
  mm.ARMP_Rank                                   AS armp_rank,
  COALESCE(map.line_armp, mm.Asset_Tree)         AS line_armp,
  reg.line_name,
  reg.workshop,
  reg.stage,
  reg.stage_name,
  reg.kind,
  COALESCE(reg.is_logistic, map.is_movement_only, false) AS is_logistic,
  mm.StartDateTimeMachineSpecific                AS ts_start,
  mm.EndDateTimeMachineSpecific                  AS ts_end,
  timestampdiff(SECOND, mm.StartDateTimeMachineSpecific, mm.EndDateTimeMachineSpecific) / 60.0 AS elapsed_min,
  CAST(mm.DurationROT AS DOUBLE)                 AS rot_min,
  mm.OperationStatus                             AS op_status,
  mm.OperationNameLocal                          AS operation,
  mm.WorkshopNameLocal                           AS workshop_local,
  mm.FinishCode                                  AS finish_code,
  mm.Grade                                       AS grade,
  mm.shape                                       AS shape_fk,
  CAST(mm.SpeedSetpoint AS DOUBLE)               AS speed_setpoint_m_min,
  CAST(mm.SpeedRealised AS DOUBLE)               AS speed_realised_m_min,
  CAST(mm.Length AS DOUBLE)                      AS length_m,
  CAST(mm.Width AS DOUBLE)                       AS width_mm,
  CAST(mm.Thickness AS DOUBLE)                   AS thickness_mm,
  mm.Weight / 1000.0                             AS weight_t,
  CAST(mm.NumberOfPasses AS INT)                 AS passes_planned,
  CAST(mm.NumberOfPassesProcessed AS INT)        AS passes_done,
  CAST(mm.UnderWindEntry AS INT)                 AS underwind_entry,
  CAST(mm.UnderWindExit AS INT)                  AS underwind_exit,
  CAST(mm.HasBeginEndChanged AS INT)             AS begin_end_changed,
  try_cast(mm.Rank AS INT)                       AS route_rank,
  mm.MU_rank                                     AS mu_rank,
  mm.SlabPosition                                AS slab_position,
  mm.ProductionLine_FK                           AS production_line_fk,
  mm.Operation_FK                                AS operation_fk,
  mm.MetalMovement_BK                            AS movement_bk,
  CASE
    WHEN hour(mm.EndDateTimeMachineSpecific) >= 6  AND hour(mm.EndDateTimeMachineSpecific) < 14 THEN 'morning'
    WHEN hour(mm.EndDateTimeMachineSpecific) >= 14 AND hour(mm.EndDateTimeMachineSpecific) < 22 THEN 'afternoon'
    ELSE 'night'
  END                                            AS shift_name,
  CASE
    WHEN hour(mm.EndDateTimeMachineSpecific) < 6 THEN date_sub(to_date(mm.EndDateTimeMachineSpecific), 1)
    ELSE to_date(mm.EndDateTimeMachineSpecific)
  END                                            AS shift_date
FROM {src}.metal_movements mm
LEFT JOIN {tgt}.gnk_machine_map   map ON mm.Machine = map.machine_code
LEFT JOIN {tgt}.gnk_line_registry reg ON COALESCE(map.line_armp, mm.Asset_Tree) = reg.line_armp
WHERE mm.Site_FK = 3
  AND (mm.Machine NOT IN ('PACK', 'PACKS') OR mm.OperationNameLocal IN ('PCKI', 'PCKG', 'PCK'));

-- Checks on the tracking key, to confirm on the full history what was validated on one example.
CREATE OR REPLACE VIEW {tgt}.gnk_coil_key_check AS
WITH m AS (SELECT * FROM {tgt}.gnk_movements WHERE shape_fk IS NULL OR shape_fk IN (3, 4, 6)),
per_id AS (
  SELECT coil_id, count(DISTINCT coil_legacy) AS legacies,
         max(CASE WHEN line_armp = 'GN-CR-RCP1' THEN 1 ELSE 0 END) AS seen_at_reception,
         max(CASE WHEN line_armp = 'GN-RP-PCK1' THEN 1 ELSE 0 END) AS seen_at_packing
  FROM m GROUP BY coil_id
),
per_coil AS (
  SELECT coil_legacy,
         max(CASE WHEN line_armp = 'GN-CR-RCP1' THEN 1 ELSE 0 END) AS seen_at_reception,
         max(CASE WHEN line_armp = 'GN-RP-PCK1' THEN 1 ELSE 0 END) AS seen_at_packing
  FROM m GROUP BY coil_legacy
)
SELECT
  (SELECT count(*) FROM m)                                                       AS movement_rows,
  (SELECT count(*) FROM m WHERE coil_id IS NULL)                                 AS rows_without_coil_id,
  (SELECT count(*) FROM m WHERE NOT (coil_id RLIKE '^[0-9]+$' AND length(coil_id) > 11)) AS rows_coil_id_bad_format,
  (SELECT count(*) FROM m WHERE left(coil_id, length(coil_legacy)) <> coil_legacy) AS rows_coil_id_not_prefixed_by_legacy,
  (SELECT count(*) FROM per_id)                                                  AS distinct_coil_ids,
  (SELECT count(*) FROM per_coil)                                                AS distinct_coils,
  (SELECT count(*) FROM per_id WHERE legacies > 1)                               AS coil_ids_with_several_legacies,
  (SELECT count(*) FROM per_id WHERE seen_at_reception = 1 AND seen_at_packing = 1) AS coil_ids_reception_to_packing,
  (SELECT count(*) FROM per_coil WHERE seen_at_reception = 1 AND seen_at_packing = 1) AS coils_reception_to_packing,
  (SELECT count(*) FROM per_coil WHERE seen_at_packing = 1)                      AS coils_seen_at_packing;
