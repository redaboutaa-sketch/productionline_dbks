"""Tests for the Aperam GNK data layer (assets, view rendering, synthetic source data).

The SQL itself is executed end-to-end on local Spark by
``scripts/aperam/validate_gnk_views_local.py`` (needs pyspark + Java); these
tests stay dependency-light so they run in the default CI job.
"""

from __future__ import annotations

import re

import pytest

from src.aperam_gnk import assets
from src.aperam_gnk.synthetic import (
    METAL_MOVEMENTS_COLUMNS,
    METAL_MOVEMENTS_DDL,
    TIMEBASED_SENSOR_COLUMNS,
    TIMEBASED_SENSOR_DDL,
    generate,
    to_spark_rows,
)
from src.aperam_gnk.views import DEFAULT_INDICATORS_TABLE, load_resource_map, render_statements


@pytest.fixture(scope="module")
def synthetic():
    return generate(days=2, coils_per_day=40, sensor_step_s=300, seed=7)


# ── assets ───────────────────────────────────────────────────────────────

def test_every_mapped_production_machine_resolves_to_a_line():
    m2l = assets.machine_to_line()
    for m in assets.load_machine_mapping():
        if m.armp and not m.is_movement_only and m.armp.startswith("GN-"):
            assert m.machine_code in m2l, f"{m.machine_code} ({m.armp}) has no Line in assets.LINES"


def test_line_armp_matches_mapping_csv():
    """A Line must claim only machine codes whose ARMP (truncated) is the Line's ARMP."""
    mapping = {m.machine_code: m.armp for m in assets.load_machine_mapping() if m.armp}
    for line in assets.LINES:
        for code in line.machine_codes:
            if code not in mapping:
                continue  # e.g. EAF1/EAF2 aliases without movements
            armp = mapping[code]
            assert armp.startswith(line.armp) or line.armp.startswith(assets.armp_level3(armp)), (code, armp, line.armp)


def test_routes_only_use_declared_lines_in_stage_order():
    for route in assets.ROUTES:
        stages = [assets.LINES_BY_ARMP[a].stage for a in route.steps]
        assert stages == sorted(stages), route.product
    assert abs(sum(r.share for r in assets.ROUTES) - 1.0) < 1e-9


def test_armp_helpers():
    assert assets.armp_level3("GN-RP-SLT2-02-01") == "GN-RP-SLT2"
    assert assets.line_for_armp("GN-ST-CCS1-40-03").armp == "GN-ST-CCS1-40"
    assert assets.line_for_armp("GN-RP-SLT2-02-02").armp == "GN-RP-SLT2"
    assert assets.line_for_armp("XX-YY") is None


def test_published_specs_are_carried():
    apl2 = assets.LINES_BY_ARMP["GN-CR-APL2"]
    assert apl2.spec.capacity_t_h == 152
    assert assets.LINES_BY_ARMP["GN-CR-CRM3"].spec.thickness_out_mm == (0.4, None)
    assert assets.LINES_BY_ARMP["GN-ST-EAF2"].spec.capacity_t == 120


# ── view rendering ───────────────────────────────────────────────────────

def test_render_statements_substitutes_every_placeholder():
    stmts = render_statements("src_cat.src_sch", "tgt_cat.tgt_sch")
    assert len(stmts) >= 15
    for s in stmts:
        assert "{" not in s and "}" not in s, s[:120]
        assert not s.rstrip().endswith(";")
    text = "\n".join(stmts)
    assert "src_cat.src_sch.metal_movements" in text
    assert "src_cat.src_sch.timebased_sensor" in text
    assert "Site_FK = 3" in text


def test_render_rejects_injection_in_identifiers():
    with pytest.raises(ValueError):
        render_statements("a.b; DROP TABLE x", "c.d")


def test_every_gold_object_is_created():
    text = "\n".join(render_statements("s.s", "t.t"))
    for obj in (
        "gnk_line_registry", "gnk_machine_map", "gnk_sensor_tags", "gnk_movements", "gold_gnk_coil_ops",
        "gold_gnk_line_kpi_shift", "gold_gnk_line_kpi_shift_oee", "gold_gnk_line_kpi_daily",
        "gold_gnk_line_reference_rate", "gold_gnk_coil_position", "gold_gnk_wip_buffers", "gold_gnk_line_gaps",
        "gnk_sensor", "gold_gnk_sensor_health_hourly", "gold_gnk_sensor_by_coil", "gnk_resource_map",
        "gnk_indicators_day", "gold_gnk_time_losses_day", "gold_gnk_oee_day", "gnk_indicators_resources",
    ):
        assert re.search(rf"CREATE OR REPLACE (VIEW|TABLE) t\.t\.{obj}\b", text), obj


# ── synthetic source data ────────────────────────────────────────────────

def test_synthetic_frames_have_exact_source_columns(synthetic):
    mm, sensors = synthetic
    assert list(mm.columns) == METAL_MOVEMENTS_COLUMNS
    assert list(sensors.columns) == TIMEBASED_SENSOR_COLUMNS
    ddl_names = [p.strip().split(" ")[0] for p in METAL_MOVEMENTS_DDL.split(", ")]
    assert ddl_names == METAL_MOVEMENTS_COLUMNS
    assert [p.strip().split(" ")[0] for p in TIMEBASED_SENSOR_DDL.split(", ")] == TIMEBASED_SENSOR_COLUMNS


def test_synthetic_movements_are_consistent(synthetic):
    mm, _ = synthetic
    assert set(mm.Site_FK) == {3}
    assert set(mm.OperationStatus) <= set(assets.OPERATION_STATUS)
    exe = mm[mm.OperationStatus == "EXEC"]
    assert (exe.EndDateTimeMachineSpecific >= exe.StartDateTimeMachineSpecific).all()
    assert exe.Weight.gt(0).all()
    # Asset_Tree is always the ARMP level 3 and ProductId is the documented concatenation
    assert mm.Asset_Tree.str.count("-").eq(2).all()
    sample = mm.iloc[0]
    assert sample.ProductId == f"{sample.Metal}_{sample.MetalPart}_{sample.ARMP_Rank}_{sample.Asset_Tree}"
    # every machine code used is a known GNK machine
    assert set(mm.Machine) <= set(assets.machine_to_line())


def test_synthetic_sensors_join_movements_on_domain(synthetic):
    mm, sensors = synthetic
    assert set(sensors.Domain) <= set(mm.Asset_Tree)
    assert sensors.Value.map(lambda v: float(v)).notna().all()


def test_to_spark_rows_types(synthetic):
    from decimal import Decimal

    mm, _ = synthetic
    rows = to_spark_rows(mm.head(20), METAL_MOVEMENTS_DDL)
    idx = METAL_MOVEMENTS_COLUMNS.index("Thickness")
    assert all(r[idx] is None or isinstance(r[idx], Decimal) for r in rows)
    idx_w = METAL_MOVEMENTS_COLUMNS.index("Weight")
    assert all(r[idx_w] is None or isinstance(r[idx_w], int) for r in rows)


# ── daily OEE indicators (time model) ────────────────────────────────────

def test_indicators_table_is_wired_and_optional():
    text = "\n".join(render_statements("s.s", "t.t"))
    assert f"FROM {DEFAULT_INDICATORS_TABLE}" in text
    skipped = "\n".join(render_statements("s.s", "t.t", indicators=None))
    assert "gold_gnk_oee_day" not in skipped
    custom = "\n".join(render_statements("s.s", "t.t", indicators="c.r.indicators_day_label",
                                         extra_resource_map=[("SYN_X", "GNK", "GN-CR-APL3", "synthetic")]))
    assert "FROM c.r.indicators_day_label" in custom and "'SYN_X'" in custom


def test_rd79_is_gueugnon_and_excluded_from_gnk():
    rows = {r: (site, armp) for r, site, armp, _ in load_resource_map()}
    assert rows["RD79"] == ("GGN", None)
    text = "\n".join(render_statements("s.s", "t.t"))
    assert "rm.site = 'GNK'" in text  # only GNK resources enter gnk_indicators_day


def test_time_model_uses_the_aperam_labels():
    text = "\n".join(render_statements("s.s", "t.t"))
    for code, label in (("SDC", "Plant shutdown"), ("SDM", "No material"), ("SDE", "No electricity"),
                        ("SDO1", "Annual maintenance"), ("SDO2", "Capital investments"),
                        ("SDO3", "Periodic maintenance"), ("SDO4", "Cleaning"), ("SDP", "No staff"),
                        ("FTP", "Product / quality"), ("FTO", "Operator"), ("FTS", "Staff"), ("FTW", "Waiting")):
        assert f"'{code}'" in text and f"'{label}'" in text, code
    assert text.count("definition pending") >= 4  # FTTE/G/I/M stay undefined


def test_synthetic_indicators_respect_the_time_model(synthetic):
    from src.aperam_gnk.synthetic import INDICATOR_COLUMNS, generate_indicators

    mm, _ = synthetic
    ind = generate_indicators(mm, seed=3)
    assert list(ind.columns) == INDICATOR_COLUMNS and len(ind) > 0
    f = ind.fillna(0)
    # TS = FT + UOT + ROT ; FT = sum of families ; FTT = sum of sub-families ; T1 = ROT / LT
    assert ((f.FTdivTS + f.UOTdivTS + f.ROTdivTS) - 1).abs().max() < 1e-9
    fam = f.FTOdivTS + f.FTPdivTS + f.FTSdivTS + f.FTTdivTS + f.FTWdivTS
    assert (fam - f.FTdivTS).abs().max() < 1e-9
    assert ((f.FTTEdivTS + f.FTTGdivTS + f.FTTIdivTS + f.FTTMdivTS) - f.FTTdivTS).abs().max() < 1e-9
    ts = f.LT * (1 - f.SDO3plus4divLT)
    assert ((f.ROTdivTS * ts / f.LT) - f.T1).abs().max() < 1e-9
