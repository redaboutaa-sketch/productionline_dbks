"""Synthetic GNK data in the EXACT schemas of ``metal_movements`` and ``timebased_sensor``.

Purpose: develop and demo the copilot without production data, and test the
SQL layer locally. Everything here is invented; volumes, rates and incidents
are plausible for a stainless cold-rolling site but are NOT Genk figures.

    python -m src.aperam_gnk.synthetic --days 3 --out ./data/gnk_synthetic   # parquet files
    python -m src.aperam_gnk.synthetic --days 3 --uc-target cat.schema      # Spark only (Databricks)

Injected incidents (so the specialists have something real to find):
  * CRM2_BREAKDOWN   - MKW2 stops ~6 h on day 2 (gap + WIP pile-up in front of it)
  * BAL1_DEWPOINT    - BAL1 atmosphere dew point drifts out of range on day 2 -> BA coils BLOQ
  * APL3_FURNACE_Z7  - APL3 zone 7 under-temperature on day 1 afternoon, slower process speed
"""

from __future__ import annotations

import argparse
import heapq
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.aperam_gnk.assets import (
    GRADES,
    LINES_BY_ARMP,
    ROUTES,
    SENSOR_TEMPLATES,
    SITE_FK_GNK,
    SITE_TZ,
    Line,
    machine_to_line,
)

METAL_MOVEMENTS_COLUMNS: list[str] = [
    "ProductId", "ProductId_Legacy", "Metal", "Metal_Legacy", "MetalPart", "MetalHeat", "Machine",
    "Machine_Indication", "Asset_Tree", "ARMP_Rank", "StartDateTimeMachineSpecific", "EndDateTimeMachineSpecific",
    "unix_StartDateTimeMachineSpecific", "unix_EndDateTimeMachineSpecific", "FinishCode", "OperationStatus",
    "MU_rank", "UnderWindEntry", "UnderWindExit", "LengthPositionSegmInBegin", "LengthPositionSegmInEnd",
    "WidthPositionSegmInBegin", "WidthPositionSegmInEnd", "HasBeginEndChanged", "NumberOfPasses",
    "NumberOfPassesProcessed", "Site_FK", "Operation_FK", "OperationNameLocal", "ProductionLine_FK",
    "WorkshopNameLocal", "SpeedSetpoint", "SpeedRealised", "DurationROT", "SlabPosition", "Length", "Width",
    "Thickness", "Weight", "Rank", "shape", "Grade", "MetalUnitMaster_FK", "MetalUnitMaster_BK", "MetalUnit_FK",
    "MetalUnit_BK", "MetalMovemenet_FK", "MetalMovement_BK", "merge_attempts", "CurrentMetalUnitIdTim",
    "MotherMetalUnitIdTim",
]

# Source column types, as documented by the data owners (information_schema of the source tables).
METAL_MOVEMENTS_DDL = (
    "ProductId string, ProductId_Legacy string, Metal string, Metal_Legacy string, MetalPart string, "
    "MetalHeat string, Machine string, Machine_Indication string, Asset_Tree string, ARMP_Rank string, "
    "StartDateTimeMachineSpecific timestamp, EndDateTimeMachineSpecific timestamp, "
    "unix_StartDateTimeMachineSpecific bigint, unix_EndDateTimeMachineSpecific bigint, FinishCode string, "
    "OperationStatus string, MU_rank string, UnderWindEntry decimal(11,0), UnderWindExit decimal(11,0), "
    "LengthPositionSegmInBegin int, LengthPositionSegmInEnd int, WidthPositionSegmInBegin decimal(7,2), "
    "WidthPositionSegmInEnd decimal(7,2), HasBeginEndChanged decimal(11,0), NumberOfPasses decimal(11,0), "
    "NumberOfPassesProcessed decimal(11,0), Site_FK int, Operation_FK int, OperationNameLocal string, "
    "ProductionLine_FK int, WorkshopNameLocal string, SpeedSetpoint decimal(6,2), SpeedRealised decimal(6,2), "
    "DurationROT int, SlabPosition string, Length decimal(10,3), Width decimal(10,3), Thickness decimal(10,3), "
    "Weight int, Rank string, shape int, Grade string, MetalUnitMaster_FK decimal(20,0), MetalUnitMaster_BK string, "
    "MetalUnit_FK decimal(20,0), MetalUnit_BK string, MetalMovemenet_FK decimal(20,0), MetalMovement_BK string, "
    "merge_attempts int, CurrentMetalUnitIdTim string, MotherMetalUnitIdTim string"
)
TIMEBASED_SENSOR_DDL = (
    "TagId string, Timestamp timestamp, Date date, unix_timestamp bigint, Domain string, Value string, "
    "Flag_ValueInRange boolean, Flag_MasterDataExists boolean"
)

TIMEBASED_SENSOR_COLUMNS: list[str] = [
    "TagId", "Timestamp", "Date", "unix_timestamp", "Domain", "Value", "Flag_ValueInRange", "Flag_MasterDataExists",
]

# Rate on ROT (t/h) and nominal speed (m/min) per line kind. Invented, order of magnitude only.
_RATE_T_H: dict[str, float] = {
    "reception": 0.0, "batch_annealing": 0.0, "coil_prep": 120, "hapl": 110, "capl": 95, "bal": 22,
    "crm_cvc6": 55, "crm_cvc4": 45, "crm_z": 30, "crm": 40, "skin_pass": 110, "slitter": 60, "ctl": 30,
    "stretch_leveller": 25, "polishing": 20, "packing": 250,
}
_SPEED_M_MIN: dict[str, float] = {
    "coil_prep": 150, "hapl": 60, "capl": 90, "bal": 35, "crm_cvc6": 600, "crm_cvc4": 550, "crm_z": 500,
    "crm": 500, "skin_pass": 300, "slitter": 200, "ctl": 60, "stretch_leveller": 45, "polishing": 40,
}
_BATCH_ANNEALING_H = 30.0
_MACHINE_CHOICE: dict[str, tuple[str, ...]] = {"GN-RP-SLT2": ("BS2L", "BS2Z"), "GN-RP-CTL2": ("LS2L", "LS2Z")}


@dataclass
class Incident:
    code: str
    line_armp: str
    start: datetime
    end: datetime


def _incidents(t0: datetime) -> list[Incident]:
    return [
        Incident("APL3_FURNACE_Z7", "GN-CR-APL3", t0 + timedelta(hours=14), t0 + timedelta(hours=20)),
        Incident("CRM2_BREAKDOWN", "GN-CR-CRM2", t0 + timedelta(days=1, hours=9), t0 + timedelta(days=1, hours=15)),
        Incident("BAL1_DEWPOINT", "GN-CR-BAL1", t0 + timedelta(days=1, hours=2), t0 + timedelta(days=1, hours=10)),
    ]


def _unix(ts: datetime | None) -> int | None:
    return None if ts is None else int(ts.timestamp())


class _Generator:
    def __init__(self, start: datetime, days: int, coils_per_day: int, seed: int) -> None:
        self.rng = random.Random(seed)
        self.t0 = start
        self.t_end = start + timedelta(days=days)
        self.days = days
        self.coils_per_day = coils_per_day
        self.line_free: dict[str, datetime] = {}
        self.rows: list[dict] = []
        self.runs: list[tuple[str, datetime, datetime]] = []  # (line, start, end) for sensors
        self.incidents = _incidents(start)
        self._movement_seq = 0
        self.line_fk = {armp: i + 1 for i, armp in enumerate(sorted(LINES_BY_ARMP))}

    # ── helpers ──────────────────────────────────────────────────────────
    def _incident(self, line: str, at: datetime) -> Incident | None:
        for inc in self.incidents:
            if inc.line_armp == line and inc.start <= at < inc.end:
                return inc
        return None

    def _row(self, *, metal: str, part: str, heat: str, line: Line, machine: str, rank: int, start: datetime | None,
             end: datetime | None, status: str, finish: str, grade: str, length: float, width: float,
             thickness: float, weight_kg: int, shape: int, sp_set: float | None = None, sp_real: float | None = None,
             rot: int | None = None, passes: int | None = None) -> None:
        self._movement_seq += 1
        armp_rank = str(rank // 10)
        asset_tree = "-".join(line.armp.split("-")[:3])
        indication = machine[-1] if machine in ("BS2L", "BS2Z", "LS2L", "LS2Z") else None
        op_name = "PCK" if line.kind == "packing" else machine
        self.rows.append({
            "ProductId": f"{metal}_{part}_{armp_rank}_{asset_tree}",
            "ProductId_Legacy": f"{metal}{part}",
            "Metal": metal, "Metal_Legacy": metal, "MetalPart": part, "MetalHeat": heat,
            "Machine": machine, "Machine_Indication": indication, "Asset_Tree": asset_tree, "ARMP_Rank": armp_rank,
            "StartDateTimeMachineSpecific": start, "EndDateTimeMachineSpecific": end,
            "unix_StartDateTimeMachineSpecific": _unix(start), "unix_EndDateTimeMachineSpecific": _unix(end),
            "FinishCode": finish, "OperationStatus": status, "MU_rank": "1",
            "UnderWindEntry": 0, "UnderWindExit": 0,
            "LengthPositionSegmInBegin": 0, "LengthPositionSegmInEnd": int(length * 1000),
            "WidthPositionSegmInBegin": 0.0, "WidthPositionSegmInEnd": round(width, 2),
            "HasBeginEndChanged": 0, "NumberOfPasses": passes, "NumberOfPassesProcessed": passes if status == "EXEC" else None,
            "Site_FK": SITE_FK_GNK, "Operation_FK": 1000 + self.line_fk[line.armp],
            "OperationNameLocal": op_name, "ProductionLine_FK": self.line_fk[line.armp],
            "WorkshopNameLocal": line.workshop,
            "SpeedSetpoint": None if sp_set is None else round(sp_set, 2),
            "SpeedRealised": None if sp_real is None else round(sp_real, 2),
            "DurationROT": rot, "SlabPosition": None,
            "Length": round(length, 3), "Width": round(width, 3), "Thickness": round(thickness, 3),
            "Weight": int(weight_kg), "Rank": str(rank), "shape": shape, "Grade": grade,
            "MetalUnitMaster_FK": None, "MetalUnitMaster_BK": metal, "MetalUnit_FK": None, "MetalUnit_BK": f"{metal}{part}",
            "MetalMovemenet_FK": self._movement_seq, "MetalMovement_BK": f"MM{self._movement_seq:09d}",
            "merge_attempts": 0, "CurrentMetalUnitIdTim": None, "MotherMetalUnitIdTim": None,
        })

    # ── cold side ────────────────────────────────────────────────────────
    def _pick_route(self):
        x = self.rng.random()
        acc = 0.0
        for r in ROUTES:
            acc += r.share
            if x <= acc:
                return r
        return ROUTES[-1]

    def generate_cold_side(self) -> None:
        """Event-driven: coils are scheduled on each line in the order they become ready (FIFO per line)."""
        n = self.days * self.coils_per_day
        heap: list[tuple[datetime, int, dict]] = []
        for i in range(n):
            arrival = self.t0 + timedelta(hours=self.rng.uniform(-12, self.days * 24 - 6))
            route = self._pick_route()
            family = "ferritic" if route.product == "2B-F" else self.rng.choice(("austenitic", "austenitic", "ferritic"))
            coil = {
                "metal": f"G{arrival:%y%j}{i:04d}", "heat": f"H{self.rng.randint(100000, 999999)}",
                "route": route, "grade": self.rng.choice(GRADES[family]),
                "width": float(self.rng.choice((1000, 1250, 1500, 1524, 2000))),
                "thickness": self.rng.uniform(3.0, 6.0), "weight_t": self.rng.uniform(15, 26), "step": 0,
            }
            heapq.heappush(heap, (arrival, i, coil))
        while heap:
            ready, key, c = heapq.heappop(heap)
            nxt = self._process_step(c, ready)
            if nxt is not None:
                heapq.heappush(heap, (nxt, key, c))

    def _process_step(self, c: dict, ready: datetime) -> datetime | None:
        """Book one operation of coil ``c``; return when it is ready for the next one (None = route over)."""
        route = c["route"]
        step_idx = c["step"]
        if step_idx >= len(route.steps):
            return None
        armp = route.steps[step_idx]
        line = LINES_BY_ARMP[armp]
        rank = (step_idx + 1) * 10
        if line.kind.startswith("crm"):
            floor = 0.4 if line.kind == "crm_z" else 0.6
            c["thickness"] = max(floor, c["thickness"] * self.rng.uniform(0.2, 0.45))
        elif line.kind == "skin_pass":
            c["thickness"] *= 0.994
        passes = self.rng.randint(5, 11) if line.kind.startswith("crm") else None
        width, thickness, weight_t = c["width"], c["thickness"], c["weight_t"]
        length = weight_t / (width / 1000 * thickness / 1000 * 7.9)
        common = dict(metal=c["metal"], heat=c["heat"], line=line, rank=rank, finish=route.product, grade=c["grade"],
                      length=length, width=width, thickness=thickness, shape=3, passes=passes)

        if ready >= self.t_end:
            # not reached within the window: the remaining route stays planned
            for k in range(step_idx, len(route.steps)):
                ln = LINES_BY_ARMP[route.steps[k]]
                status = "DISP" if k == step_idx else "NDIS"
                self._row(**{**common, "line": ln, "rank": (k + 1) * 10}, part="00", machine=ln.machine_codes[0],
                          start=None, end=None, status=status, weight_kg=int(weight_t * 1000))
            return None

        machine = self.rng.choice(_MACHINE_CHOICE.get(armp, (line.machine_codes[0],)))
        if line.kind == "reception":
            end = ready + timedelta(minutes=5)
            self._row(**common, part="00", machine=machine, start=ready, end=end, status="EXEC",
                      weight_kg=int(weight_t * 1000))
            c["step"] += 1
            return end

        sp_set = _SPEED_M_MIN.get(line.kind)
        if line.kind == "batch_annealing":
            start = ready + timedelta(hours=self.rng.uniform(2, 10))
            end = start + timedelta(hours=_BATCH_ANNEALING_H * self.rng.uniform(0.9, 1.1))
            rot = int((end - start).total_seconds() / 60)
        else:
            queue_free = self.line_free.get(armp, self.t0 - timedelta(days=1))
            start = max(ready + timedelta(minutes=self.rng.uniform(20, 120)),
                        queue_free + timedelta(minutes=self.rng.uniform(3, 12)))
            inc = self._incident(armp, start)
            if inc and inc.code == "CRM2_BREAKDOWN":
                start = inc.end + timedelta(minutes=self.rng.uniform(10, 40))
            rate = _RATE_T_H[line.kind] * self.rng.uniform(0.85, 1.05)
            if line.kind.startswith("crm"):
                rate *= 6.0 / passes  # more passes, lower t/h
            slow = armp == "GN-CR-APL3" and self._incident(armp, start) is not None
            if slow:
                rate *= 0.75
            rot = max(5, int(weight_t / rate * 60))
            stop = self.rng.uniform(5, 45) if self.rng.random() < 0.15 else 0.0
            end = start + timedelta(minutes=rot + stop)
            self.line_free[armp] = end

        sp_real = None
        if sp_set:
            factor = self.rng.uniform(0.86, 1.0)
            if armp == "GN-CR-APL3" and self._incident(armp, start):
                factor *= 0.78
            sp_real = sp_set * factor

        if end > self.t_end:
            status = "COUR" if start <= self.t_end else "DISP"
            self._row(**common, part="00", machine=machine, start=start if status == "COUR" else None, end=None,
                      status=status, weight_kg=int(weight_t * 1000), sp_set=sp_set)
            for k in range(step_idx + 1, len(route.steps)):
                ln = LINES_BY_ARMP[route.steps[k]]
                self._row(**{**common, "line": ln, "rank": (k + 1) * 10}, part="00", machine=ln.machine_codes[0],
                          start=None, end=None, status="NDIS", weight_kg=int(weight_t * 1000))
            return None

        weight_out = weight_t * self.rng.uniform(0.975, 0.997)
        parts = self.rng.randint(2, 6) if line.kind in ("slitter", "ctl") else 1
        for p in range(parts):
            self._row(**{**common, "width": (width - 20) / parts, "shape": 6 if line.kind == "ctl" else 3},
                      part=f"{p + 1:02d}" if parts > 1 else "00", machine=machine, start=start, end=end,
                      status="EXEC", weight_kg=int(weight_out * 1000 / parts), sp_set=sp_set, sp_real=sp_real, rot=rot)
        self.runs.append((armp, start, end))
        c["weight_t"] = weight_out
        c["step"] += 1

        # BA coils annealed during the dew point excursion are put on quality hold before the next step
        if armp == "GN-CR-BAL1" and self._incident(armp, start) and c["step"] < len(route.steps):
            ln = LINES_BY_ARMP[route.steps[c["step"]]]
            self._row(**{**common, "line": ln, "rank": rank + 10}, part="00", machine=ln.machine_codes[0],
                      start=None, end=None, status="BLOQ", weight_kg=int(weight_out * 1000))
            return None
        return end

    # ── steel shop (heats -> slabs) ──────────────────────────────────────
    def generate_steel_shop(self) -> None:
        t = self.t0
        heat_no = 0
        while t < self.t_end - timedelta(hours=3):
            heat_no += 1
            eaf = "GN-ST-EAF2" if heat_no % 3 else "GN-ST-EAF1"
            eaf_line = LINES_BY_ARMP[eaf]
            heat = f"H{t:%y%j}{heat_no:03d}"
            grade = self.rng.choice(GRADES["austenitic"] + GRADES["ferritic"])
            tons = (eaf_line.spec.capacity_t or 100) * self.rng.uniform(0.95, 1.02)
            cycle = (eaf_line.spec.cycle_min or 60) * self.rng.uniform(0.95, 1.15)
            steps = [
                (eaf, cycle, "OVN2" if eaf.endswith("2") else "OVN1"),
                ("GN-ST-AOD1", self.rng.uniform(45, 60), "AOD1"),
                (self.rng.choice(("GN-ST-SLM1", "GN-ST-SLM3")), self.rng.uniform(25, 40), None),
                ("GN-ST-CCS1", self.rng.uniform(50, 65), "GIET"),
            ]
            start = t
            for rank, (armp, dur, machine) in enumerate(steps, start=1):
                line = LINES_BY_ARMP[armp]
                machine = machine or line.machine_codes[0]
                end = start + timedelta(minutes=dur)
                if end > self.t_end:
                    break
                self._row(metal=heat, part="00", heat=heat, line=line, machine=machine, rank=rank * 10, start=start,
                          end=end, status="EXEC", finish="SLAB", grade=grade, length=0.0, width=0.0, thickness=0.0,
                          weight_kg=int(tons * 1000), shape=7 if armp == "GN-ST-CCS1" else 5, rot=int(dur))
                self.runs.append((armp, start, end))
                start = end + timedelta(minutes=self.rng.uniform(5, 15))
            t += timedelta(minutes=cycle)

    # ── sensors ──────────────────────────────────────────────────────────
    def generate_sensors(self, step_s: int, lines: set[str] | None = None) -> pd.DataFrame:
        by_line: dict[str, list[tuple[datetime, datetime]]] = {}
        for armp, s, e in self.runs:
            by_line.setdefault(armp, []).append((s, e))
        out: list[dict] = []
        n_steps = int((self.t_end - self.t0).total_seconds() // step_s)
        for armp, line in LINES_BY_ARMP.items():
            templates = SENSOR_TEMPLATES.get(line.kind)
            if not templates or (lines and armp not in lines):
                continue
            intervals = sorted(by_line.get(armp, []))
            if not intervals:
                continue  # a line that never ran in the window has no historian data
            j = 0
            drift_state = {t.suffix: 0.0 for t in templates}
            for k in range(n_steps):
                ts = self.t0 + timedelta(seconds=k * step_s)
                while j < len(intervals) and intervals[j][1] < ts:
                    j += 1
                running = j < len(intervals) and intervals[j][0] <= ts <= intervals[j][1]
                inc = self._incident(armp, ts)
                for t in templates:
                    is_speed = "SPEED" in t.suffix or t.suffix in ("SHEAR.CYCLES", "STRETCH.FORCE", "KNIFE.LOAD")
                    if not running and (is_speed or "ROLLFORCE" in t.suffix or "TEN." in t.suffix or "ELONG" in t.suffix):
                        value = 0.0
                    else:
                        noise = self.rng.gauss(0, t.noise * abs(t.nominal) if t.nominal else 1.5)
                        value = t.nominal + noise
                        if inc and inc.code == "APL3_FURNACE_Z7" and t.suffix == "FUR.Z07.TEMP":
                            frac = (ts - inc.start) / (inc.end - inc.start)
                            value -= 90 * math.sin(math.pi * frac)
                        if inc and inc.code == "APL3_FURNACE_Z7" and t.suffix == "PRC.STRIP.SPEED":
                            value *= 0.78
                        if inc and inc.code == "BAL1_DEWPOINT" and t.suffix == "ATM.DEWPOINT":
                            drift_state[t.suffix] = min(25.0, drift_state[t.suffix] + 0.08 * step_s / 60)
                            value += drift_state[t.suffix]
                        if inc and inc.code == "CRM2_BREAKDOWN":
                            value = 0.0 if is_speed or "ROLLFORCE" in t.suffix else value
                    in_range = t.low <= value <= t.high
                    out.append({
                        "TagId": f"{armp}.{t.suffix}", "Timestamp": ts, "Date": ts.date(),
                        "unix_timestamp": _unix(ts), "Domain": "-".join(armp.split("-")[:3]),
                        "Value": f"{value:.3f}", "Flag_ValueInRange": bool(in_range), "Flag_MasterDataExists": True,
                    })
        return pd.DataFrame(out, columns=TIMEBASED_SENSOR_COLUMNS)


INDICATOR_COLUMNS: list[str] = (
    "periode LT T0 FTdivCT FTdivLT FTdivTS N_FUST FTFUSTdivLT FTOdivLT FTOdivTS FTPdivLT FTPdivTS FTSdivLT FTSdivTS "
    "FTTdivLT FTTdivTS FTTEdivTS FTTGdivTS FTTIdivTS FTTMdivTS FTWdivLT FTWdivTS N_FT MTBF MTTR N_COIL N_STRI ROTdivTS "
    "SDCdivCT SDdivCT SDEdivCT SDMdivCT SDO1divCT SDO2divCT SDO3plus4 SDO3plus4divLT SDO3divCT SDO3divLT SDO4divCT "
    "SDO4divLT SDOdivCT SDPdivCT SDPdivLT T1 TSdivCT UOTdivLT UOTdivTS resource _ingested_at _input_file_name "
    "_file_arrival_time"
).split()
INDICATORS_DDL = ", ".join(
    f"{c} {'date' if c == 'periode' else 'string' if c in ('resource', '_input_file_name') else 'timestamp' if c in ('_ingested_at', '_file_arrival_time') else 'double'}"
    for c in INDICATOR_COLUMNS
)
# Synthetic resources only (the real codes, e.g. RD79, are mapped in reference/resource_map_gnk.csv).
SYNTHETIC_RESOURCE_MAP: list[tuple[str, str, str]] = [
    ("SYN_APL3", "GN-CR-APL3", "synthetic"), ("SYN_CRM2", "GN-CR-CRM2", "synthetic"),
    ("SYN_APL2", "GN-CR-APL2", "synthetic"), ("SYN_BAL1", "GN-CR-BAL1", "synthetic"),
    ("SYN_SLT2", "GN-RP-SLT2", "synthetic"),
]


def generate_indicators(mm: pd.DataFrame, seed: int = 42) -> pd.DataFrame:
    """Daily rows of ``indicators_day_label`` for the synthetic resources, consistent with ``mm``.

    Time model (see sql/60_gold_gnk_time_model.sql): CT = SD_out + LT, LT = SDO3 + SDO4 + TS,
    TS = FT + UOT + ROT. ROT comes from the coils' DurationROT, FT from the injected incidents plus
    random failures, UOT from stops inside coils, SDO3 is the idle remainder (no material).
    """
    rng = random.Random(seed)
    exe = mm[(mm.OperationStatus == "EXEC") & mm.DurationROT.notna()].copy()
    exe["line"] = exe.Machine.map({k: v.armp for k, v in machine_to_line().items()})
    runs = exe.drop_duplicates(["Metal", "line", "StartDateTimeMachineSpecific"])
    if runs.empty:
        return pd.DataFrame(columns=INDICATOR_COLUMNS)
    t0 = runs.StartDateTimeMachineSpecific.min().normalize()
    t1 = runs.EndDateTimeMachineSpecific.max().normalize()
    incidents = _incidents(min(runs.StartDateTimeMachineSpecific).to_pydatetime())
    out = []
    for resource, armp, _ in SYNTHETIC_RESOURCE_MAP:
        lr = runs[runs.line == armp]
        for day in pd.date_range(t0, t1, freq="D"):
            d0, d1 = day.to_pydatetime(), (day + pd.Timedelta(days=1)).to_pydatetime()
            in_day = lr[(lr.EndDateTimeMachineSpecific > d0) & (lr.StartDateTimeMachineSpecific < d1)]
            ct = 86400.0
            sdm = 4 * 3600.0 if (day.dayofweek == 2 and rng.random() < 0.5) else 0.0  # planned maintenance
            lt = ct - sdm
            rot = float((in_day.DurationROT * 60).sum())
            busy = float(sum((min(e, d1) - max(s, d0)).total_seconds()
                             for s, e in zip(in_day.StartDateTimeMachineSpecific, in_day.EndDateTimeMachineSpecific)))
            rot = min(rot, busy)
            fail_inc = sum(max(0.0, (min(i.end, d1) - max(i.start, d0)).total_seconds())
                           for i in incidents if i.line_armp == armp and i.code.endswith("BREAKDOWN"))
            n_rand = rng.randint(0, 4)
            fails = [rng.uniform(300, 2400) for _ in range(n_rand)]
            fam = {k: 0.0 for k in ("O", "P", "S", "TE", "TG", "TI", "TM", "W")}
            fam["TM"] += fail_inc
            for f in fails:
                fam[rng.choice(list(fam))] += f
            ft = sum(fam.values())
            uot = max(0.0, busy - rot) + rng.uniform(0.02, 0.06) * lt
            if rot + ft + uot > lt:  # keep the identities exact
                scale = lt / (rot + ft + uot)
                rot, ft, uot = rot * scale, ft * scale, uot * scale
                fam = {k: v * scale for k, v in fam.items()}
            sdo3 = lt - (rot + ft + uot)
            ts = lt - sdo3
            n_ft = n_rand + (1 if fail_inc else 0)
            ftt = fam["TE"] + fam["TG"] + fam["TI"] + fam["TM"]

            def r_(x, base):
                return (x / base) if base > 0 and x > 0 else None

            row = {
                "periode": day.date(), "LT": lt, "T0": lt / ct, "FTdivCT": r_(ft, ct), "FTdivLT": r_(ft, lt),
                "FTdivTS": r_(ft, ts), "N_FUST": float(n_ft) or None, "FTFUSTdivLT": r_(ft, lt),
                "FTOdivLT": r_(fam["O"], lt), "FTOdivTS": r_(fam["O"], ts), "FTPdivLT": r_(fam["P"], lt),
                "FTPdivTS": r_(fam["P"], ts), "FTSdivLT": r_(fam["S"], lt), "FTSdivTS": r_(fam["S"], ts),
                "FTTdivLT": r_(ftt, lt), "FTTdivTS": r_(ftt, ts), "FTTEdivTS": r_(fam["TE"], ts),
                "FTTGdivTS": r_(fam["TG"], ts), "FTTIdivTS": r_(fam["TI"], ts), "FTTMdivTS": r_(fam["TM"], ts),
                "FTWdivLT": r_(fam["W"], lt), "FTWdivTS": r_(fam["W"], ts), "N_FT": float(n_ft) or None,
                "MTBF": (ts / n_ft) if n_ft else None, "MTTR": (ft / n_ft) if n_ft else None,
                "N_COIL": float(len(in_day)) or None, "N_STRI": float(rng.randint(0, 6)), "ROTdivTS": r_(rot, ts),
                "SDCdivCT": None, "SDdivCT": r_(sdm + sdo3, ct), "SDEdivCT": None, "SDMdivCT": r_(sdm, ct),
                "SDO1divCT": None, "SDO2divCT": None, "SDO3plus4": sdo3 or None, "SDO3plus4divLT": r_(sdo3, lt),
                "SDO3divCT": r_(sdo3, ct), "SDO3divLT": r_(sdo3, lt), "SDO4divCT": None, "SDO4divLT": None,
                "SDOdivCT": r_(sdo3, ct), "SDPdivCT": None, "SDPdivLT": None, "T1": rot / lt if lt else 0.0,
                "TSdivCT": r_(ts, ct), "UOTdivLT": r_(uot, lt), "UOTdivTS": r_(uot, ts), "resource": resource,
                "_ingested_at": datetime.now(), "_input_file_name": "synthetic/INDICATORS_DAY.parquet",
                "_file_arrival_time": datetime.now(),
            }
            out.append(row)
    return pd.DataFrame(out, columns=INDICATOR_COLUMNS)


def to_spark_rows(df: pd.DataFrame, ddl: str) -> list[tuple]:
    """Convert a generated DataFrame to Python rows matching ``ddl`` (None for nulls, Decimal for decimals)."""
    from decimal import Decimal

    types = [part.strip().split(" ", 1)[1].strip().lower() for part in ddl.split(", ") if " " in part.strip()]
    # decimal(p,s) contains ", " only without a space after the comma, so the split above is safe.
    conv = []
    for typ in types:
        if typ.startswith("decimal"):
            scale = int(typ.split(",")[1].rstrip(")"))
            conv.append(lambda v, s=scale: Decimal(f"{float(v):.{s}f}"))
        elif typ in ("int", "bigint"):
            conv.append(lambda v: int(v))
        elif typ == "timestamp":
            conv.append(lambda v: v.to_pydatetime() if hasattr(v, "to_pydatetime") else v)
        elif typ == "double":
            conv.append(lambda v: float(v))
        elif typ == "date":
            conv.append(lambda v: v.date() if hasattr(v, "date") and not isinstance(v, date) else v)
        elif typ == "boolean":
            conv.append(lambda v: bool(v))
        else:
            conv.append(lambda v: v)
    out = []
    for rec in df.itertuples(index=False, name=None):
        out.append(tuple(None if (v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT) else f(v)
                         for v, f in zip(rec, conv)))
    return out


def generate(days: int = 3, start: datetime | None = None, coils_per_day: int = 90, sensor_step_s: int = 60,
             seed: int = 42, sensor_lines: set[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(metal_movements, timebased_sensor)`` DataFrames in the source schemas."""
    if start is None:
        # end the window "now" so ages, look-backs and current_timestamp() filters behave like live data
        now = datetime.now(ZoneInfo(SITE_TZ)).replace(tzinfo=None, second=0, microsecond=0)
        start = now - timedelta(days=days)
    g = _Generator(start, days, coils_per_day, seed)
    g.generate_steel_shop()
    g.generate_cold_side()
    mm = pd.DataFrame(g.rows, columns=METAL_MOVEMENTS_COLUMNS)
    sensors = g.generate_sensors(sensor_step_s, sensor_lines)
    return mm, sensors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--coils-per-day", type=int, default=90)
    ap.add_argument("--sensor-step-s", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, help="write metal_movements.parquet / timebased_sensor.parquet here")
    ap.add_argument("--uc-target", help="catalog.schema to write Delta tables to (needs an active Spark session)")
    args = ap.parse_args(argv)
    mm, sensors = generate(args.days, coils_per_day=args.coils_per_day, sensor_step_s=args.sensor_step_s, seed=args.seed)
    print(f"metal_movements: {len(mm):,} rows, timebased_sensor: {len(sensors):,} rows")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        mm.to_parquet(args.out / "metal_movements.parquet", index=False)
        sensors.to_parquet(args.out / "timebased_sensor.parquet", index=False)
        generate_indicators(mm, seed=args.seed).to_parquet(args.out / "indicators_day_label.parquet", index=False)
        print(f"written to {args.out}")
    if args.uc_target:
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
        spark.createDataFrame(to_spark_rows(mm, METAL_MOVEMENTS_DDL), METAL_MOVEMENTS_DDL).write.mode("overwrite").option("overwriteSchema", "true") \
            .saveAsTable(f"{args.uc_target}.metal_movements")
        ind = generate_indicators(mm, seed=args.seed)
        spark.createDataFrame(to_spark_rows(ind, INDICATORS_DDL), INDICATORS_DDL).write.mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(f"{args.uc_target}.indicators_day_label")
        spark.createDataFrame(to_spark_rows(sensors, TIMEBASED_SENSOR_DDL), TIMEBASED_SENSOR_DDL).write.mode("overwrite").option("overwriteSchema", "true") \
            .saveAsTable(f"{args.uc_target}.timebased_sensor")
        print(f"Delta tables written to {args.uc_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
