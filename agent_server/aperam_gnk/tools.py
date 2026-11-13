"""Deterministic GNK tools: fixed, parameterised SQL over the GNK gold layer.

They exist so the frequent questions (how is line X doing, where is coil Y, which
sensor drifted, where is metal piling up) are answered from vetted queries rather
than from SQL an LLM wrote. ``sql_query`` stays available for everything else.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from decimal import Decimal

from langchain_core.tools import tool

from agent_server.aperam_gnk import is_gnk
from agent_server.aperam_gnk.context import site_now
from src.aperam_gnk.assets import LINES_BY_ARMP, armp_level3

_ARMP = re.compile(r"^[A-Z]{2}-[A-Z]{2}-[A-Z0-9]{2,6}(-\d{2}){0,2}$")
_METAL = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def _q(sql: str, params: dict, label: str) -> list[dict]:
    from agent_server.context_builder import _parameterized_query, _safe_rows

    return [_clean(r) for r in _safe_rows(_parameterized_query(sql, params, label))]


def _clean(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            v = v.isoformat(sep=" ", timespec="minutes")
        elif isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            v = None
        elif v is not None and not isinstance(v, (int, float, str, bool)):
            v = str(v)
        out[k] = v
    return out


def _check_armp(line_armp: str) -> str | None:
    line_armp = (line_armp or "").strip().upper()
    if not _ARMP.match(line_armp):
        return None
    return line_armp


@tool
def gnk_line_status(line_armp: str, hours: int = 24) -> dict:
    """Status of ONE Genk line over the last `hours` (default 24, max 168).

    line_armp: ARMP level-3 code, e.g. 'GN-CR-APL3' (BUL3), 'GN-CR-CRM2' (MKW2), 'GN-CR-BAL1'.
    Returns: published envelope, KPIs per shift (tonnes, utilisation, run efficiency, speed
    ratio, yield, OEE proxy), the last 10 coils with their times and speeds, stops (gaps between
    coils >= 15 min), the queue waiting in front of the line and its demonstrated rates.
    """
    armp = _check_armp(line_armp)
    if not armp:
        return {"error": f"'{line_armp}' is not an ARMP code like GN-CR-APL3"}
    hours = max(1, min(int(hours or 24), 168))
    now = site_now()
    since = (now - timedelta(hours=hours)).isoformat(sep=" ")
    p = {"a": armp, "since": since, "since_d": (now - timedelta(hours=hours)).date().isoformat()}
    line = LINES_BY_ARMP.get(armp)
    return {
        "line_armp": armp,
        "line_name": line.name if line else None,
        "envelope": _q("SELECT * FROM gnk_line_registry WHERE line_armp = %(a)s", p, "gnk_tool_registry"),
        "kpi_by_shift": _q(
            "SELECT shift_date, shift_name, coils, tonnes, busy_h, rot_h, round(utilisation,3) AS utilisation,"
            " round(run_efficiency,3) AS run_efficiency, round(speed_ratio,3) AS speed_ratio,"
            " round(yield,4) AS yield, round(oee_proxy,3) AS oee_proxy,"
            " round(throughput_t_per_rot_h,1) AS t_per_rot_h, round(avg_thickness_mm,2) AS avg_thickness_mm"
            " FROM gold_gnk_line_kpi_shift_oee WHERE line_armp = %(a)s AND shift_date >= %(since_d)s"
            " ORDER BY shift_date, shift_name", p, "gnk_tool_kpi"),
        "last_coils": _q(
            "SELECT metal, grade, finish_code, ts_start, ts_end, round(elapsed_min) AS elapsed_min, rot_min,"
            " round(weight_t,1) AS weight_t, thickness_in_mm, thickness_mm, round(speed_setpoint_m_min) AS sp_set,"
            " round(speed_realised_m_min) AS sp_real, round(yield_pct,2) AS yield_pct, passes_done,"
            " round(wait_before_h,1) AS wait_before_h"
            " FROM gold_gnk_coil_ops WHERE line_armp = %(a)s AND ts_end >= %(since)s ORDER BY ts_end DESC LIMIT 10",
            p, "gnk_tool_last_coils"),
        "stops": _q(
            "SELECT gap_start, gap_end, gap_min, prev_metal, next_metal FROM gold_gnk_line_gaps"
            " WHERE line_armp = %(a)s AND gap_end >= %(since)s ORDER BY gap_min DESC LIMIT 15", p, "gnk_tool_gaps"),
        "queue": _q("SELECT * FROM gold_gnk_wip_buffers WHERE line_armp = %(a)s", p, "gnk_tool_queue"),
        "demonstrated_rates": _q(
            "SELECT thickness_class, coils, round(p50_t_per_h,1) AS p50_t_per_h, round(p90_t_per_h,1) AS p90_t_per_h,"
            " round(p90_speed_m_min) AS p90_speed_m_min FROM gold_gnk_line_reference_rate WHERE line_armp = %(a)s",
            p, "gnk_tool_rates"),
    }


@tool
def gnk_coil_trace(metal: str) -> dict:
    """Full trace of ONE coil (metal_movements.Metal): route, status of every operation,
    geometry and yield per operation, and the sensor profile recorded while it was on each line.

    Use it for quality holds (BLOQ), customer claims, 'why is this coil late' and genealogy questions.
    """
    m = (metal or "").strip()
    if not _METAL.match(m):
        return {"error": f"'{metal}' is not a valid Metal identifier"}
    p = {"m": m}
    return {
        "metal": m,
        "movements": _q(
            "SELECT machine_code, machine_indication, line_armp, line_name, op_status, route_rank, ts_start, ts_end,"
            " round(weight_t,2) AS weight_t, thickness_mm, width_mm, grade, finish_code, heat, metal_part"
            " FROM gnk_movements WHERE metal = %(m)s ORDER BY coalesce(ts_start, timestamp'2999-01-01'), route_rank",
            p, "gnk_tool_trace_movements"),
        "operations": _q(
            "SELECT line_armp, ts_start, ts_end, round(elapsed_min) AS elapsed_min, rot_min, thickness_in_mm,"
            " thickness_mm, round(thickness_reduction_pct,1) AS reduction_pct, round(yield_pct,2) AS yield_pct,"
            " round(speed_ratio,3) AS speed_ratio, parts_out, round(wait_before_h,1) AS wait_before_h"
            " FROM gold_gnk_coil_ops WHERE metal = %(m)s ORDER BY ts_start", p, "gnk_tool_trace_ops"),
        "sensor_profile": _q(
            "SELECT s.line_armp, s.tag_id, t.description, t.unit, s.samples, round(s.avg_value,3) AS avg_value,"
            " round(s.min_value,3) AS min_value, round(s.max_value,3) AS max_value,"
            " round(s.out_of_range_ratio,3) AS out_of_range_ratio, t.range_lower_limit, t.range_upper_limit"
            " FROM gold_gnk_sensor_by_coil s LEFT JOIN gnk_sensor_tags t ON t.tag_id = s.tag_id"
            " WHERE s.metal = %(m)s ORDER BY s.ts_start, s.tag_id", p, "gnk_tool_trace_sensors"),
    }


@tool
def gnk_sensor_scan(line_armp: str, hours: int = 12) -> dict:
    """Scan every sensor tag of ONE line: last `hours` (default 12) versus its previous 7-day baseline.

    Returns per tag: recent mean / min / max, baseline mean and std, a z-score of the recent
    mean against the baseline, and out-of-range samples while producing. Tags are ranked by
    |z| so precursors of a stop or a quality drift come first.
    """
    armp = _check_armp(line_armp)
    if not armp:
        return {"error": f"'{line_armp}' is not an ARMP code like GN-CR-BAL1"}
    hours = max(1, min(int(hours or 12), 72))
    now = site_now()
    p = {
        "a": armp3 if (armp3 := armp_level3(armp)) else armp,
        "recent": (now - timedelta(hours=hours)).isoformat(sep=" "),
        "base": (now - timedelta(hours=hours) - timedelta(days=7)).isoformat(sep=" "),
    }
    rows = _q(
        "SELECT h.tag_id, t.description, t.unit, t.range_lower_limit, t.range_upper_limit,"
        " CASE WHEN h.hour >= %(recent)s THEN 'recent' ELSE 'baseline' END AS period,"
        " sum(h.samples) AS samples, sum(h.out_of_range_nonzero) AS out_of_range,"
        " sum(h.avg_value * h.samples) / sum(h.samples) AS mean, min(h.min_value) AS min_value,"
        " max(h.max_value) AS max_value, avg(h.std_value) AS std_value"
        " FROM gold_gnk_sensor_health_hourly h LEFT JOIN gnk_sensor_tags t ON t.tag_id = h.tag_id"
        " WHERE h.armp_l3 = %(a)s AND h.hour >= %(base)s"
        " GROUP BY h.tag_id, t.description, t.unit, t.range_lower_limit, t.range_upper_limit, 6",
        p, "gnk_tool_sensor_scan")
    tags: dict[str, dict] = {}
    for r in rows:
        t = tags.setdefault(r["tag_id"], {k: r.get(k) for k in ("tag_id", "description", "unit",
                                                                 "range_lower_limit", "range_upper_limit")})
        t[r["period"]] = {k: r.get(k) for k in ("samples", "out_of_range", "mean", "min_value", "max_value", "std_value")}
    ranked = []
    for t in tags.values():
        rec, base = t.get("recent") or {}, t.get("baseline") or {}
        z = None
        if rec.get("mean") is not None and base.get("mean") is not None:
            std = base.get("std_value") or 0.0
            z = round((rec["mean"] - base["mean"]) / std, 2) if std > 0 else None
        t["z_recent_vs_baseline"] = z
        ranked.append(t)
    ranked.sort(key=lambda t: (-(abs(t["z_recent_vs_baseline"] or 0)), -((t.get("recent") or {}).get("out_of_range") or 0)))
    return {"line_armp": armp, "window_hours": hours, "baseline_days": 7, "tags": ranked[:20],
            "note": "z uses the mean of hourly standard deviations as baseline spread; idle zeros are included"}


@tool
def gnk_flow_overview() -> dict:
    """Material flow of the Genk cold side right now: queue in front of every line (coils, tonnes,
    holds, age) and how many hours of work that queue represents at the line's demonstrated median
    rate. Short cover = the line will starve; long cover or a fast-growing queue = upstream overload
    or a downstream stop. Also returns the coils on quality hold (BLOQ).
    """
    wip = _q("SELECT * FROM gold_gnk_wip_buffers", {}, "gnk_tool_flow_wip")
    rates = _q("SELECT line_armp, sum(p50_t_per_h * coils) / sum(coils) AS p50_t_per_h"
               " FROM gold_gnk_line_reference_rate GROUP BY line_armp", {}, "gnk_tool_flow_rates")
    rate = {r["line_armp"]: r.get("p50_t_per_h") for r in rates}
    for w in wip:
        r = rate.get(w.get("line_armp"))
        w["demonstrated_p50_t_per_h"] = round(r, 1) if r else None
        w["queue_cover_h"] = round(w["tonnes_waiting"] / r, 1) if r and w.get("tonnes_waiting") is not None else None
    wip.sort(key=lambda w: (LINES_BY_ARMP[w["line_armp"]].stage if w.get("line_armp") in LINES_BY_ARMP else 999))
    blocked = _q("SELECT metal, grade, finish_code, last_line_armp, next_line_armp, weight_t, age_h"
                 " FROM gold_gnk_coil_position WHERE position_state = 'blocked' ORDER BY age_h DESC LIMIT 25",
                 {}, "gnk_tool_flow_blocked")
    return {"queues_in_flow_order": wip, "blocked_coils": blocked}


GNK_TOOLS = (gnk_line_status, gnk_coil_trace, gnk_sensor_scan, gnk_flow_overview)

GNK_TOOL_DISPLAY_NAMES = {
    "gnk_line_status": "Reading Genk line status",
    "gnk_coil_trace": "Tracing the coil route",
    "gnk_sensor_scan": "Scanning line sensors",
    "gnk_flow_overview": "Reading material flow and queues",
}


def gnk_tools() -> list:
    """The GNK tools when the GNK domain is active, else nothing (upstream unchanged)."""
    return list(GNK_TOOLS) if is_gnk() else []


def gnk_specialist_tools() -> list:
    """Tool set of every specialist in GNK mode.

    The upstream analytical tools (plan loader, similar cases, anomaly detector, RUL, MILP...) read the
    bottling-line tables, so in GNK mode they are withheld rather than left for the model to misuse.
    """
    from agent_server.tools.calculator import calculator
    from agent_server.tools.sql_query import sql_query

    return [sql_query, calculator, *GNK_TOOLS]
