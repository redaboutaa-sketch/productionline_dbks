"""Context bundle for the GNK copilot, read from the GNK gold layer.

It keeps the keys the upstream agent pipeline already understands, so routing,
the quick snapshot, the synthesis pass and the UI keep working:

    scenario        shift framing (site, shift id, clock)
    current_kpi     cold-side KPIs for the current shift (oee_to_now, ... in %)
    line_state      one row per line, machine_id = ARMP level-3 code, status in the
                    upstream vocabulary (running / stopped / starved / degraded)
    active_events   OPEN situations only: lines idle with work waiting, coils on hold,
                    sensors out of range while producing
    production_plan {} - no plan source connected yet (see docs/aperam/README_GNK.md)
    constraints     the published envelope of each line (gnk_line_registry)

plus GNK-only slices that ``gnk_trim_slices`` hands to the specialists:

    wip_buffers     queue in front of each line (coils, tonnes, age, holds)
    blocked_coils   coils on BLOQ, oldest first
    recent_gaps     closed stops (gaps between coils) over the last 24 h
    sensor_alerts   tags out of range while producing, last 8 h
    line_kpi_7d     per-line shift KPIs over the last 7 days (baseline for comparisons)
    oee_days_7d     daily OEE, Aperam definition (T0, T1 official; T2, T3 proxies), 7 days
    time_losses_7d  biggest time losses over 7 days by TUC code (SD*, FT*, UOT)
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.aperam_gnk.assets import COLD_ROUTE_ARMP, LINES_BY_ARMP, SHIFTS, SITE_NAME, SITE_TZ

logger = logging.getLogger(__name__)

# A line with no coil for this long is considered idle.
IDLE_AFTER_MIN = 30
# An idle line with work waiting becomes an open event after this long; it is 'high' when the
# queue is at least BIG_QUEUE_COILS coils and the idle time exceeds 2 h.
MIN_IDLE_EVENT_MIN = 60
BIG_QUEUE_COILS = 5
# Below this length-weighted speed ratio a running line is flagged degraded.
DEGRADED_SPEED_RATIO = 0.85

_SHIFT_ID = re.compile(r"^(\d{4}-\d{2}-\d{2})_(morning|afternoon|night)$")

# Lines shown in the line state, in flow order: cold route + main steel-shop units.
STATE_LINES: tuple[str, ...] = (
    "GN-ST-EAF1", "GN-ST-EAF2", "GN-ST-AOD1", "GN-ST-VOD1", "GN-ST-VOD2", "GN-ST-CCS1", *COLD_ROUTE_ARMP,
)


def site_now() -> datetime:
    """Naive local site time (the source data is local, naive)."""
    return datetime.now(ZoneInfo(SITE_TZ)).replace(tzinfo=None, microsecond=0)


def shift_of(ts: datetime) -> tuple[date, str, datetime, datetime]:
    """(shift_date, shift_name, start, end) of the 3x8 shift containing ``ts``.

    The night shift (22:00-06:00) belongs to the date it starts on, matching
    ``shift_date`` in gnk_movements.
    """
    for name, h0, h1 in SHIFTS:
        if h0 < h1 and h0 <= ts.hour < h1:
            start = ts.replace(hour=h0, minute=0, second=0, microsecond=0)
            return start.date(), name, start, start + timedelta(hours=h1 - h0)
    # night
    start_day = ts.date() if ts.hour >= 22 else ts.date() - timedelta(days=1)
    start = datetime.combine(start_day, datetime.min.time()).replace(hour=22)
    return start_day, "night", start, start + timedelta(hours=8)


def resolve_shift(shift_id: str | None, now: datetime | None = None) -> tuple[date, str, datetime, datetime]:
    """Use ``YYYY-MM-DD_<shift>`` when given, otherwise the current shift."""
    now = now or site_now()
    m = _SHIFT_ID.match((shift_id or "").strip())
    if m:
        d = date.fromisoformat(m.group(1))
        name = m.group(2)
        h0 = dict((n, a) for n, a, _ in SHIFTS)[name]
        start = datetime.combine(d, datetime.min.time()).replace(hour=h0)
        return d, name, start, start + timedelta(hours=8)
    return shift_of(now)


def _f(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def previous_shift(shift_date: date, shift_name: str) -> tuple[date, str]:
    names = [n for n, _, _ in SHIFTS]
    i = names.index(shift_name)
    if i == 0:
        return shift_date - timedelta(days=1), names[-1]
    return shift_date, names[i - 1]


def _queries(shift_date: date, shift_name: str, now: datetime) -> dict[str, tuple[str, dict, str]]:
    p_shift = {"d": shift_date.isoformat(), "s": shift_name}
    prev_d, prev_s = previous_shift(shift_date, shift_name)
    return {
        "kpi": (
            "SELECT * FROM gold_gnk_line_kpi_shift_oee WHERE shift_date = %(d)s AND shift_name = %(s)s",
            p_shift, "gnk_kpi_shift",
        ),
        "kpi_prev": (
            "SELECT * FROM gold_gnk_line_kpi_shift_oee WHERE shift_date = %(d)s AND shift_name = %(s)s",
            {"d": prev_d.isoformat(), "s": prev_s}, "gnk_kpi_prev_shift",
        ),
        "kpi_7d": (
            "SELECT line_armp, count(*) AS shifts, round(avg(tonnes), 1) AS avg_tonnes_per_shift,"
            " round(avg(oee_proxy), 3) AS avg_oee_proxy, round(avg(speed_ratio), 3) AS avg_speed_ratio,"
            " round(avg(run_efficiency), 3) AS avg_run_efficiency, round(avg(yield), 4) AS avg_yield"
            " FROM gold_gnk_line_kpi_shift_oee WHERE shift_date >= %(d7)s AND shift_date < %(d)s"
            " GROUP BY line_armp",
            {"d": shift_date.isoformat(), "d7": (shift_date - timedelta(days=7)).isoformat()}, "gnk_kpi_7d",
        ),
        "wip": ("SELECT * FROM gold_gnk_wip_buffers ORDER BY tonnes_waiting DESC", {}, "gnk_wip"),
        "on_line": (
            "SELECT next_line_armp AS line_armp, metal, grade, finish_code, thickness_mm, width_mm, weight_t"
            " FROM gold_gnk_coil_position WHERE position_state = 'on_line'",
            {}, "gnk_on_line",
        ),
        "blocked": (
            "SELECT metal, grade, finish_code, last_line_armp, next_line_armp, weight_t, age_h"
            " FROM gold_gnk_coil_position WHERE position_state = 'blocked' ORDER BY age_h DESC LIMIT 30",
            {}, "gnk_blocked",
        ),
        "last_activity": (
            "SELECT line_armp, max(ts_end) AS last_end, max(ts_start) AS last_start"
            " FROM gold_gnk_coil_ops WHERE ts_end >= %(since)s GROUP BY line_armp",
            {"since": (now - timedelta(days=7)).isoformat(sep=" ")}, "gnk_last_activity",
        ),
        "gaps": (
            "SELECT line_armp, line_name, prev_metal, next_metal, gap_start, gap_end, gap_min"
            " FROM gold_gnk_line_gaps WHERE gap_end >= %(since)s ORDER BY gap_min DESC LIMIT 20",
            {"since": (now - timedelta(hours=24)).isoformat(sep=" ")}, "gnk_gaps",
        ),
        "sensor_alerts": (
            "SELECT h.armp_l3, h.tag_id, t.description, t.unit, t.range_lower_limit, t.range_upper_limit,"
            " sum(h.out_of_range_nonzero) AS out_of_range, sum(h.samples) AS samples,"
            " round(min(h.min_value), 3) AS min_value, round(max(h.max_value), 3) AS max_value,"
            " min(CASE WHEN h.out_of_range_nonzero > 0 THEN h.hour END) AS first_hour,"
            " max(CASE WHEN h.out_of_range_nonzero > 0 THEN h.hour END) AS last_hour"
            " FROM gold_gnk_sensor_health_hourly h LEFT JOIN gnk_sensor_tags t ON t.tag_id = h.tag_id"
            " WHERE h.hour >= %(since)s"
            " GROUP BY h.armp_l3, h.tag_id, t.description, t.unit, t.range_lower_limit, t.range_upper_limit"
            " HAVING sum(h.out_of_range_nonzero) > 0 ORDER BY out_of_range DESC LIMIT 20",
            {"since": (now - timedelta(hours=8)).isoformat(sep=" ")}, "gnk_sensor_alerts",
        ),
        "oee_days": (
            "SELECT resource, line_armp, line_name, prod_date, round(oee_t1t2t3, 3) AS oee_t1t2t3,"
            " round(oee_t0t1t2t3, 3) AS oee_t0t1t2t3, round(t0_opening, 3) AS t0_opening,"
            " round(t1_availability, 3) AS t1_availability, round(t2_proxy, 3) AS t2_proxy,"
            " round(t3_proxy, 4) AS t3_proxy, round(loading_h, 1) AS loading_h, round(rot_h, 1) AS rot_h,"
            " round(ft_h, 1) AS ft_h, round(uot_unproductive_h, 1) AS uot_h,"
            " round(scheduled_in_lt_h, 1) AS scheduled_in_lt_h, n_ft, round(mtbf_h, 1) AS mtbf_h,"
            " round(mttr_min) AS mttr_min, n_coil"
            " FROM gold_gnk_oee_day WHERE prod_date >= %(d7)s ORDER BY line_armp, prod_date",
            {"d7": (shift_date - timedelta(days=7)).isoformat()}, "gnk_oee_days",
        ),
        "losses_7d": (
            "SELECT line_armp, resource, band, tuc_code, tuc_label, round(sum(loss_h), 1) AS loss_h"
            " FROM gold_gnk_time_losses_day WHERE prod_date >= %(d7)s"
            " GROUP BY line_armp, resource, band, tuc_code, tuc_label ORDER BY loss_h DESC LIMIT 15",
            {"d7": (shift_date - timedelta(days=7)).isoformat()}, "gnk_losses_7d",
        ),
        "registry": (
            "SELECT line_armp, line_name, workshop, stage, kind, machine_codes, width_min_mm, width_max_mm,"
            " thickness_in_min_mm, thickness_in_max_mm, thickness_out_min_mm, max_speed_m_min, max_coil_t,"
            " capacity_t_h, capacity_t, cycle_min, spec_notes, remarks FROM gnk_line_registry WHERE NOT is_logistic",
            {}, "gnk_registry",
        ),
    }


def _to_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(v).replace("Z", ""))
    except ValueError:
        return None


def derive_line_state(
    kpi_rows: list[dict], wip_rows: list[dict], on_line_rows: list[dict], last_rows: list[dict],
    alert_rows: list[dict], now: datetime,
) -> list[dict]:
    """One row per line in flow order, status in the upstream vocabulary.

    running  : a coil is on the line, or the last coil left < IDLE_AFTER_MIN ago
    degraded : running, but speed ratio this shift < DEGRADED_SPEED_RATIO or a sensor out of range
    stopped  : idle with coils waiting in front of it (breakdown, planned stop, crew...)
    starved  : idle with an empty queue (nothing to process)
    """
    kpi = {r["line_armp"]: r for r in kpi_rows}
    wip = {r["line_armp"]: r for r in wip_rows}
    on_line: dict[str, list[dict]] = {}
    for r in on_line_rows:
        on_line.setdefault(r.get("line_armp"), []).append(r)
    last = {r["line_armp"]: r for r in last_rows}
    alerting = {r.get("armp_l3") for r in alert_rows}

    out = []
    for pos, armp in enumerate(STATE_LINES, start=1):
        line = LINES_BY_ARMP.get(armp)
        k = kpi.get(armp, {})
        w = wip.get(armp, {})
        last_end = _to_dt(last.get(armp, {}).get("last_end"))
        idle_min = (now - last_end).total_seconds() / 60 if last_end else None
        coils_on_line = on_line.get(armp, [])
        waiting = int(w.get("coils_waiting") or 0)
        speed_ratio = _f(k.get("speed_ratio"))

        if coils_on_line or (idle_min is not None and idle_min < IDLE_AFTER_MIN):
            status = "running"
            if (speed_ratio is not None and speed_ratio < DEGRADED_SPEED_RATIO) or armp in alerting:
                status = "degraded"
        elif waiting > 0:
            status = "stopped"
        else:
            status = "starved"

        out.append({
            "machine_id": armp,
            "machine_name": line.name if line else armp,
            "line_position": pos,
            "stage": line.stage_name if line else None,
            "status": status,
            "idle_min": None if idle_min is None else round(idle_min),
            "coil_on_line": coils_on_line[0].get("metal") if coils_on_line else None,
            "coils_this_shift": int(k.get("coils") or 0),
            "tonnes_this_shift": _f(k.get("tonnes"), 0.0),
            "throughput_t_per_rot_h": _f(k.get("throughput_t_per_rot_h")),
            "speed_ratio": speed_ratio,
            "oee_proxy": _f(k.get("oee_proxy")),
            "coils_waiting": waiting,
            "tonnes_waiting": _f(w.get("tonnes_waiting"), 0.0),
            "coils_blocked_before": int(w.get("coils_blocked") or 0),
            "sensor_alert": armp in alerting,
        })
    return out


def rescale_to_elapsed(kpi_rows: list[dict], shift_minutes: float) -> list[dict]:
    """The gold view divides busy time by the full 8 h shift; for a shift in progress use elapsed time."""
    if shift_minutes >= 480:
        return kpi_rows
    out = []
    for r in kpi_rows:
        r = dict(r)
        busy_min = (_f(r.get("busy_h"), 0.0) or 0.0) * 60
        util = min(1.0, busy_min / max(shift_minutes, 30.0))
        r["utilisation"] = round(util, 3)
        if r.get("oee_proxy") is not None:
            r["oee_proxy"] = round(util * (_f(r.get("run_efficiency"), 1.0) or 1.0)
                                   * min(_f(r.get("speed_ratio"), 1.0) or 1.0, 1.05)
                                   * min(_f(r.get("yield"), 1.0) or 1.0, 1.0), 3)
        out.append(r)
    return out


def derive_current_kpi(kpi_rows: list[dict]) -> dict:
    """Cold-side KPIs for the shift, weighted by busy time, in % like upstream."""
    cold = [r for r in kpi_rows if str(r.get("line_armp", "")).startswith(("GN-CR", "GN-RP"))
            and r.get("oee_proxy") is not None]
    packed = sum(_f(r.get("tonnes"), 0.0) for r in kpi_rows if r.get("line_armp") == "GN-RP-PCK1")
    if not cold:
        return {"tonnes_packed": round(packed, 1), "cumulative_cases": round(packed, 1)}
    wsum = sum(_f(r.get("busy_h"), 0.0) for r in cold) or 1.0

    def wavg(key: str) -> float | None:
        vals = [(_f(r.get(key)), _f(r.get("busy_h"), 0.0)) for r in cold if r.get(key) is not None]
        if not vals:
            return None
        return sum(v * w for v, w in vals) / (sum(w for _, w in vals) or wsum)

    avail = [(_f(r.get("utilisation"), 0.0) * _f(r.get("run_efficiency"), 1.0), _f(r.get("busy_h"), 0.0)) for r in cold]
    return {
        "oee_to_now": round(100 * (wavg("oee_proxy") or 0.0), 1),
        "availability_to_now": round(100 * sum(a * w for a, w in avail) / wsum, 1),
        "performance_to_now": round(100 * (wavg("speed_ratio") or 0.0), 1),
        "quality_to_now": round(100 * (wavg("yield") or 0.0), 1),
        "tonnes_processed_cold_side": round(sum(_f(r.get("tonnes"), 0.0) for r in cold), 1),
        "tonnes_packed": round(packed, 1),
        # read by the upstream snapshot / synthesis as the output counter
        "cumulative_cases": round(packed, 1),
        "kpi_definition": "OEE proxy = utilisation x run efficiency (ROT/elapsed) x speed ratio x metal yield",
    }


def derive_active_events(line_state: list[dict], blocked: list[dict], alerts: list[dict], now: datetime) -> list[dict]:
    """Open situations only, in the upstream event shape (timestamp_end None = open)."""
    events: list[dict] = []
    for m in line_state:
        if m["status"] == "stopped" and (m.get("idle_min") or 0) >= MIN_IDLE_EVENT_MIN:
            sev = "high" if m["idle_min"] >= 120 and m["coils_waiting"] >= BIG_QUEUE_COILS else "medium"
            events.append({
                "machine_id": m["machine_id"], "event_type": "line_idle_with_queue", "severity": sev,
                "timestamp_start": (now - timedelta(minutes=m["idle_min"])).isoformat(), "timestamp_end": None,
                "duration_minutes": m["idle_min"],
                "description": f"{m['machine_name']} idle for {m['idle_min']} min with {m['coils_waiting']} coils "
                               f"({m['tonnes_waiting']} t) waiting in front of it",
                "root_cause": None,
            })
    by_line: dict[str, list[dict]] = {}
    for c in blocked:
        by_line.setdefault(c.get("next_line_armp") or c.get("last_line_armp"), []).append(c)
    for armp, coils in by_line.items():
        oldest = max(_f(c.get("age_h"), 0.0) for c in coils)
        events.append({
            "machine_id": armp, "event_type": "quality_hold", "severity": "high" if len(coils) >= 3 else "medium",
            "timestamp_start": (now - timedelta(hours=oldest)).isoformat(), "timestamp_end": None,
            "duration_minutes": round(oldest * 60),
            "description": f"{len(coils)} coil(s) on BLOQ before {armp} "
                           f"({sum(_f(c.get('weight_t'), 0.0) for c in coils):.0f} t), last processed on "
                           f"{', '.join(sorted({str(c.get('last_line_armp')) for c in coils}))}",
            "root_cause": None,
        })
    for a in alerts:
        first = _to_dt(a.get("first_hour")) or now
        events.append({
            "machine_id": a.get("armp_l3"), "event_type": "sensor_out_of_range",
            "severity": "medium" if (_f(a.get("out_of_range"), 0) or 0) < 30 else "high",
            "timestamp_start": first.isoformat(), "timestamp_end": None,
            "duration_minutes": None,
            "description": f"{a.get('tag_id')} ({a.get('description') or 'n/a'}) out of range "
                           f"{a.get('out_of_range')}/{a.get('samples')} samples, min {a.get('min_value')} "
                           f"max {a.get('max_value')} {a.get('unit') or ''} vs limits "
                           f"[{a.get('range_lower_limit')}, {a.get('range_upper_limit')}]",
            "root_cause": None,
        })
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    events.sort(key=lambda e: (sev_rank.get(e["severity"], 3), -(e.get("duration_minutes") or 0)))
    return events


def build_gnk_context(shift_id: str | None, timestamp: str | None = None, machine_id: str | None = None) -> dict:
    """Assemble the GNK bundle. Never raises: missing tables yield empty slices."""
    from agent_server.context_builder import _parallel_param_queries, _safe_rows

    now = site_now()
    shift_date, shift_name, start, end = resolve_shift(shift_id, now)
    ref_now = min(now, end)
    rows = {k: _safe_rows(v) for k, v in _parallel_param_queries(_queries(shift_date, shift_name, ref_now)).items()}

    line_state = derive_line_state(rows["kpi"], rows["wip"], rows["on_line"], rows["last_activity"],
                                   rows["sensor_alerts"], ref_now)
    elapsed = max(0.0, min(480.0, (ref_now - start).total_seconds() / 60))
    rows["kpi"] = rescale_to_elapsed(rows["kpi"], elapsed)
    current_kpi = derive_current_kpi(rows["kpi"])
    if "oee_to_now" not in current_kpi and rows["kpi_prev"]:
        # start of shift: nothing finished yet, show the previous shift and say so
        prev_d, prev_s = previous_shift(shift_date, shift_name)
        current_kpi = {**derive_current_kpi(rows["kpi_prev"]),
                       "kpi_scope": f"previous shift {prev_d.isoformat()}_{prev_s} (no coil finished yet this shift)"}
    else:
        current_kpi["kpi_scope"] = "current shift"
    context = {
        "domain": "aperam_gnk",
        "scenario": {
            "shift_id": f"{shift_date.isoformat()}_{shift_name}",
            "scenario_name": f"{SITE_NAME} - {shift_name} shift {shift_date.isoformat()} (live from metal_movements)",
            "site": "GNK",
            "shift_start": start.isoformat(),
            "shift_end": end.isoformat(),
            "current_timestamp": ref_now.isoformat(),
            "elapsed_minutes": round(elapsed),
            "remaining_minutes": round(480 - elapsed),
            "clock": f"site local time ({SITE_TZ})",
        },
        "current_kpi": current_kpi,
        "line_topology": list(STATE_LINES),
        "line_state": line_state,
        "active_events": derive_active_events(line_state, rows["blocked"], rows["sensor_alerts"], ref_now),
        "production_plan": {},
        "constraints": rows["registry"],
        "wip_buffers": rows["wip"],
        "blocked_coils": rows["blocked"],
        "recent_gaps": rows["gaps"],
        "sensor_alerts": rows["sensor_alerts"],
        "line_kpi_shift": rows["kpi"] or rows["kpi_prev"],
        "line_kpi_7d": rows["kpi_7d"],
        # official time-model OEE (daily indicators table), last 7 days, and its biggest losses
        "oee_days_7d": rows["oee_days"],
        "time_losses_7d": rows["losses_7d"],
        "active_incidents": [],
    }
    if machine_id:
        context["machine_id"] = machine_id
    logger.info("GNK context: shift=%s lines=%d events=%d wip=%d", context["scenario"]["shift_id"],
                len(line_state), len(context["active_events"]), len(rows["wip"]))
    return context


def gnk_trim_slices(context: dict, specialist_name: str) -> dict:
    """GNK-only slices added to the upstream per-specialist trim (token budget aware)."""
    out: dict = {}
    out["wip_buffers"] = context.get("wip_buffers", [])[:12]
    if specialist_name in ("downtime_analyst", "maintenance_predictor", "shift_briefing", "oee_coach"):
        out["recent_gaps"] = context.get("recent_gaps", [])[:10]
    if specialist_name in ("downtime_analyst", "maintenance_predictor", "quality_specialist", "shift_briefing"):
        out["sensor_alerts"] = context.get("sensor_alerts", [])[:10]
    if specialist_name in ("quality_specialist", "supply_chain_advisor", "schedule_optimizer", "shift_briefing"):
        out["blocked_coils"] = context.get("blocked_coils", [])[:15]
    if specialist_name in ("oee_coach", "strategic_advisor", "schedule_optimizer", "shift_briefing"):
        out["line_kpi_shift"] = context.get("line_kpi_shift", [])[:25]
        out["line_kpi_7d"] = context.get("line_kpi_7d", [])[:25]
    if specialist_name in ("oee_coach", "strategic_advisor", "maintenance_predictor", "downtime_analyst",
                           "shift_briefing"):
        out["oee_days_7d"] = context.get("oee_days_7d", [])[:40]
        out["time_losses_7d"] = context.get("time_losses_7d", [])[:10]
    if specialist_name in ("schedule_optimizer", "supply_chain_advisor", "strategic_advisor"):
        out["line_state"] = context.get("line_state", [])
    return out
