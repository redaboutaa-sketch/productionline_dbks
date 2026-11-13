"""Aperam GNK mode of the agent: prompt switch, context derivation, trimming, tools."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest

from agent_server.aperam_gnk import GNK_DOMAIN, is_gnk
from agent_server.aperam_gnk import context as gctx
from agent_server.aperam_gnk.prompts import GNK_PROMPTS
from agent_server.prompts import loader
from agent_server.prompts.catalog import CATALOG


@pytest.fixture
def gnk_mode(monkeypatch):
    monkeypatch.setenv("COPILOT_DOMAIN", GNK_DOMAIN)
    yield
    monkeypatch.delenv("COPILOT_DOMAIN", raising=False)


# ── prompt switch ────────────────────────────────────────────────────────

def test_upstream_prompts_unchanged_without_the_switch(monkeypatch):
    monkeypatch.delenv("COPILOT_DOMAIN", raising=False)
    assert not is_gnk()
    assert "Aperam" not in loader.prompt_text("downtime_analyst_system")


def test_gnk_prompts_win_with_the_switch(gnk_mode):
    assert is_gnk()
    for slug, text in GNK_PROMPTS.items():
        assert loader.prompt_text(slug) == text


def test_gnk_prompts_cover_every_specialist_and_keep_template_variables():
    specs = {s.slug: s for s in CATALOG}
    for slug in ("orchestrator_system", "response_synthesis", "general_system", "downtime_analyst_system",
                 "quality_specialist_system", "supply_chain_advisor_system", "oee_coach_system",
                 "schedule_optimizer_system", "maintenance_predictor_system", "strategic_advisor_system",
                 "shift_briefing_system"):
        assert slug in GNK_PROMPTS, slug
        text = GNK_PROMPTS[slug]
        for var in specs[slug].variables:
            assert "{" + var + "}" in text, (slug, var)
        if specs[slug].variables:  # templates must render with exactly the call-site variables
            text.format(**{v: "x" for v in specs[slug].variables})


def test_gnk_prompts_name_only_gnk_tables():
    text = "\n".join(GNK_PROMPTS.values())
    assert "gold_gnk_wip_buffers" in text and "gnk_movements" in text and "gold_gnk_oee_day" in text
    # bottling-line tables may only appear in the explicit 'do not use' warning
    for dead in ("gold_history_machine_state", "quality_checks_bronze", "machine_bom"):
        assert dead not in text


def test_specialist_prompts_keep_the_ui_response_contract():
    for slug in ("downtime_analyst_system", "quality_specialist_system", "oee_coach_system"):
        t = GNK_PROMPTS[slug]
        for heading in ("## Root Cause", "## Situation", "## Recommendation", "## Confidence"):
            assert heading in t, (slug, heading)
        assert "FILLER" not in t and "Crown caps" not in t


# ── context derivation ───────────────────────────────────────────────────

NOW = datetime(2026, 11, 13, 10, 0, 0)


def test_shift_resolution():
    assert gctx.shift_of(datetime(2026, 11, 13, 10, 0))[:2] == (date(2026, 11, 13), "morning")
    assert gctx.shift_of(datetime(2026, 11, 13, 23, 0))[:2] == (date(2026, 11, 13), "night")
    d, name, start, end = gctx.shift_of(datetime(2026, 11, 14, 3, 0))
    assert (d, name, start.hour, end) == (date(2026, 11, 13), "night", 22, datetime(2026, 11, 14, 6, 0))
    assert gctx.resolve_shift("2026-11-10_afternoon", NOW)[:2] == (date(2026, 11, 10), "afternoon")
    assert gctx.resolve_shift("shift_1_dev", NOW)[:2] == (date(2026, 11, 13), "morning")


def _rows():
    kpi = [
        {"line_armp": "GN-CR-APL3", "coils": 9, "tonnes": 190.0, "busy_h": 6.0, "utilisation": 0.75,
         "run_efficiency": 0.9, "speed_ratio": 0.80, "yield": 0.985, "oee_proxy": 0.53, "throughput_t_per_rot_h": 105},
        {"line_armp": "GN-CR-CRM2", "coils": 2, "tonnes": 40.0, "busy_h": 1.5, "utilisation": 0.19,
         "run_efficiency": 0.95, "speed_ratio": 0.93, "yield": 0.99, "oee_proxy": 0.17, "throughput_t_per_rot_h": 30},
        {"line_armp": "GN-RP-PCK1", "coils": 20, "tonnes": 300.0, "busy_h": 2.0, "utilisation": 0.25,
         "run_efficiency": 0.8, "speed_ratio": None, "yield": 0.99, "oee_proxy": 0.2},
    ]
    wip = [
        {"line_armp": "GN-CR-CRM2", "line_name": "MKW2", "coils_waiting": 31, "tonnes_waiting": 648.8,
         "coils_blocked": 0, "tonnes_blocked": 0.0, "avg_age_h": 17.4, "max_age_h": 25.1},
        {"line_armp": "GN-CR-SKP1", "line_name": "SKP1", "coils_waiting": 2, "tonnes_waiting": 37.1,
         "coils_blocked": 1, "tonnes_blocked": 14.8, "avg_age_h": 27.2, "max_age_h": 46.5},
    ]
    on_line = [{"line_armp": "GN-CR-APL3", "coil_id": "20264100001100001"}]
    last = [
        {"line_armp": "GN-CR-APL3", "last_end": NOW - timedelta(minutes=5)},
        {"line_armp": "GN-CR-CRM2", "last_end": NOW - timedelta(minutes=320)},
        {"line_armp": "GN-CR-BAL1", "last_end": NOW - timedelta(minutes=90)},
    ]
    alerts = [{"armp_l3": "GN-CR-BAL1", "tag_id": "GN-CR-BAL1.ATM.DEWPOINT", "description": "dew point",
               "unit": "degC", "range_lower_limit": -70, "range_upper_limit": -40, "out_of_range": 175,
               "samples": 240, "min_value": -52, "max_value": -25, "first_hour": NOW - timedelta(hours=6)}]
    blocked = [{"coil_id": "20264100009100009", "coil_legacy": "20264100009", "last_line_armp": "GN-CR-BAL1", "next_line_armp": "GN-CR-SKP1",
                "weight_t": 14.8, "age_h": 46.5}]
    return kpi, wip, on_line, last, alerts, blocked


def test_line_state_status_vocabulary():
    kpi, wip, on_line, last, alerts, _ = _rows()
    state = {m["machine_id"]: m for m in gctx.derive_line_state(kpi, wip, on_line, last, alerts, NOW)}
    assert state["GN-CR-APL3"]["status"] == "degraded"  # running but speed ratio 0.80
    assert state["GN-CR-CRM2"]["status"] == "stopped"  # idle 320 min with 31 coils waiting
    assert state["GN-CR-CRM2"]["idle_min"] == 320
    assert state["GN-CR-BAL1"]["status"] == "starved"  # idle, nothing waiting
    assert state["GN-CR-BAL1"]["sensor_alert"] is True
    positions = [m["line_position"] for m in state.values()]
    assert positions == sorted(positions)


def test_current_kpi_is_weighted_and_in_percent():
    kpi, *_ = _rows()
    k = gctx.derive_current_kpi(kpi)
    assert 0 < k["oee_to_now"] < 100
    assert k["tonnes_packed"] == 300.0 == k["cumulative_cases"]
    assert k["tonnes_processed_cold_side"] == 530.0


def test_active_events_are_open_and_ranked():
    kpi, wip, on_line, last, alerts, blocked = _rows()
    state = gctx.derive_line_state(kpi, wip, on_line, last, alerts, NOW)
    events = gctx.derive_active_events(state, blocked, alerts, NOW)
    types = {(e["machine_id"], e["event_type"]) for e in events}
    assert ("GN-CR-CRM2", "line_idle_with_queue") in types
    assert ("GN-CR-SKP1", "quality_hold") in types
    assert ("GN-CR-BAL1", "sensor_out_of_range") in types
    assert all(e["timestamp_end"] is None for e in events)
    assert events[0]["severity"] == "high"


def test_build_gnk_context_end_to_end(monkeypatch):
    kpi, wip, on_line, last, alerts, blocked = _rows()
    fake = {"kpi": kpi, "kpi_prev": [], "kpi_7d": [], "wip": wip, "on_line": on_line, "blocked": blocked,
            "last_activity": last, "gaps": [], "sensor_alerts": alerts, "oee_days": [{"line_armp": "GN-CR-CRM2", "oee": 0.6}], "losses_7d": [], "registry": [{"line_armp": "GN-CR-APL3"}]}
    import agent_server.context_builder as cb

    monkeypatch.setattr(cb, "_parallel_param_queries", lambda queries: {k: fake[k] for k in queries})
    monkeypatch.setattr(gctx, "site_now", lambda: NOW)
    ctx = gctx.build_gnk_context("shift_1_dev", "2025-01-01T14:00:00")
    assert ctx["domain"] == "aperam_gnk"
    assert ctx["scenario"]["shift_id"] == "2026-11-13_morning"
    assert ctx["scenario"]["elapsed_minutes"] == 240
    assert ctx["current_kpi"]["oee_to_now"] > 0
    assert ctx["active_events"]
    assert ctx["current_kpi"]["kpi_scope"] == "current shift"
    assert ctx["oee_days_7d"][0]["oee"] == 0.6


def test_kpi_rescaled_to_elapsed_time_for_a_shift_in_progress():
    rows = [{"line_armp": "GN-CR-APL3", "busy_h": 0.5, "run_efficiency": 1.0, "speed_ratio": 1.0, "yield": 1.0,
             "utilisation": 0.0625, "oee_proxy": 0.0625}]
    r = gctx.rescale_to_elapsed(rows, 60)[0]
    assert r["utilisation"] == 0.5 and r["oee_proxy"] == 0.5
    assert gctx.rescale_to_elapsed(rows, 480) is rows


def test_previous_shift():
    assert gctx.previous_shift(date(2026, 11, 13), "morning") == (date(2026, 11, 12), "night")
    assert gctx.previous_shift(date(2026, 11, 13), "night") == (date(2026, 11, 13), "afternoon")


def test_trim_and_snapshot_carry_gnk_slices(monkeypatch, gnk_mode):
    from agent_server import agent

    kpi, wip, on_line, last, alerts, blocked = _rows()
    state = gctx.derive_line_state(kpi, wip, on_line, last, alerts, NOW)
    ctx = {
        "domain": "aperam_gnk", "scenario": {"shift_id": "2026-11-13_morning", "scenario_name": "GNK",
                                              "current_timestamp": NOW.isoformat(), "remaining_minutes": 240},
        "current_kpi": gctx.derive_current_kpi(kpi), "line_state": state, "production_plan": {},
        "active_events": gctx.derive_active_events(state, blocked, alerts, NOW),
        "wip_buffers": wip, "blocked_coils": blocked, "sensor_alerts": alerts, "recent_gaps": [],
        "line_kpi_shift": kpi, "line_kpi_7d": [],
    }
    slim = agent._trim_context(ctx, "quality_specialist")
    assert slim["blocked_coils"] and slim["wip_buffers"] and slim["sensor_alerts"]
    snap = agent._quick_snapshot(ctx)
    assert "Biggest queue: **GN-CR-CRM2**" in snap
    assert "Quality holds" in snap
    assert "OEE:" in snap


# ── tools ────────────────────────────────────────────────────────────────

def test_tools_validate_inputs():
    from agent_server.aperam_gnk.tools import gnk_coil_trace, gnk_line_status, gnk_sensor_scan

    assert "error" in gnk_line_status.invoke({"line_armp": "BUL3; DROP TABLE x"})
    assert "error" in gnk_sensor_scan.invoke({"line_armp": "nope"})
    assert "error" in gnk_coil_trace.invoke({"coil": "x' OR 1=1 --"})
    assert "error" in gnk_coil_trace.invoke({"coil": "137002"})  # MetalPart alone is not a coil key
    from agent_server.aperam_gnk.tools import gnk_oee_report

    assert "error" in gnk_oee_report.invoke({"line_or_resource": "RD79' --"})


def test_oee_report_accepts_armp_or_resource(monkeypatch):
    from agent_server.aperam_gnk import tools

    seen = []
    monkeypatch.setattr(tools, "_q", lambda sql, params, label: seen.append((sql, params)) or [])
    tools.gnk_oee_report.invoke({"line_or_resource": "GN-CR-CRM2", "days": 7})
    assert "line_armp = %(k)s" in seen[0][0] and seen[0][1]["k"] == "GN-CR-CRM2"
    seen.clear()
    tools.gnk_oee_report.invoke({"line_or_resource": "RD79"})
    assert "resource = %(k)s" in seen[0][0] and seen[0][1]["k"] == "RD79"


def test_flow_overview_computes_queue_cover(monkeypatch):
    from agent_server.aperam_gnk import tools

    def fake_q(sql, params, label):
        if label == "gnk_tool_flow_wip":
            return [{"line_armp": "GN-CR-CRM2", "tonnes_waiting": 600.0}, {"line_armp": "GN-CR-APL3", "tonnes_waiting": 212.0}]
        if label == "gnk_tool_flow_rates":
            return [{"line_armp": "GN-CR-CRM2", "p50_t_per_h": 30.0}, {"line_armp": "GN-CR-APL3", "p50_t_per_h": 106.0}]
        return []

    monkeypatch.setattr(tools, "_q", fake_q)
    out = tools.gnk_flow_overview.invoke({})
    q = out["queues_in_flow_order"]
    assert [w["line_armp"] for w in q] == ["GN-CR-APL3", "GN-CR-CRM2"]  # flow order
    assert q[0]["queue_cover_h"] == 2.0 and q[1]["queue_cover_h"] == 20.0


def test_specialists_get_gnk_tools_only_in_gnk_mode(monkeypatch, gnk_mode):
    import agent_server.specialists.downtime_analyst as da

    captured = {}
    monkeypatch.setattr(da, "ChatDatabricks", lambda endpoint: object())
    monkeypatch.setattr(da, "create_agent", lambda **kw: captured.update(kw) or "agent")
    da.create_downtime_analyst()
    names = {t.name for t in captured["tools"]}
    assert names == {"sql_query", "calculator", "gnk_line_status", "gnk_coil_trace", "gnk_sensor_scan",
                     "gnk_flow_overview", "gnk_oee_report"}
    assert "Aperam Genk" in captured["system_prompt"].content


def test_context_and_tool_sql_only_reference_gnk_objects():
    """Every table named in the GNK context / tool SQL is created by src/aperam_gnk/views.py."""
    import inspect

    from agent_server.aperam_gnk import tools
    from src.aperam_gnk.views import render_statements

    created = set(re.findall(r"CREATE OR REPLACE (?:VIEW|TABLE) t\.t\.(\w+)", "\n".join(render_statements("s.s", "t.t"))))
    src = inspect.getsource(gctx) + inspect.getsource(tools)
    used = set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", src))
    assert used and used <= created, used - created


def test_coil_trace_uses_productid_legacy_or_metal_legacy(monkeypatch):
    from agent_server.aperam_gnk import tools

    seen = []
    monkeypatch.setattr(tools, "_q", lambda sql, params, label: seen.append((label, sql, dict(params))) or [])
    out = tools.gnk_coil_trace.invoke({"coil": "20254152535137002"})
    assert out["coil_legacy"] == "20254152535"
    mv = next(x for x in seen if x[0] == "gnk_tool_trace_movements")
    assert "coil_id = %(c)s" in mv[1]
    ops = next(x for x in seen if x[0] == "gnk_tool_trace_ops")
    assert "coil_legacy = %(l)s" in ops[1] and ops[2]["l"] == "20254152535"
    seen.clear()
    tools.gnk_coil_trace.invoke({"coil": "20254152535"})
    assert "coil_legacy = %(c)s" in next(x for x in seen if x[0] == "gnk_tool_trace_movements")[1]
