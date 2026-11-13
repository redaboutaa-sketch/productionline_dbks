"""Aperam Genk (GNK) asset model: ARMP tree, production lines, routes, sensors.

This module is the single source of truth for the GNK scope of the copilot.
It replaces, for Aperam, what ``src/data_generation/config.py`` is for the
upstream bottling-line demo.

Sources
-------
* ``reference/armp_machine_mapping_gnk.csv``: local machine code (as found in
  ``metal_movements.Machine``) to ARMP code, as provided by Aperam data teams.
* "Overview processes Aperam Genk site" poster set: technical data per line
  (capacities, widths, thicknesses, speeds). Values are copied as published;
  where the poster gives nothing, the field is ``None`` and the copilot must
  use the *demonstrated* rate computed from ``metal_movements`` instead
  (see ``sql/30_gold_line_kpi.sql``).

ARMP code structure
-------------------
``<SITE>-<WORKSHOP>-<LINE>[-<SECTION>-<SUBSECTION>]`` e.g. ``GN-CR-APL3``.
``metal_movements.Asset_Tree`` and ``timebased_sensor.Domain`` both carry the
first three levels, which is the join key between movements and sensors.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SITE_FK_GNK = 3
SITE_CODE = "GN"
SITE_NAME = "Aperam Genk"
# metal_movements / timebased_sensor carry LOCAL site time; SQL compares them to this clock.
SITE_TZ = "Europe/Brussels"

_REFERENCE_DIR = Path(__file__).parent / "reference"
MAPPING_CSV = _REFERENCE_DIR / "armp_machine_mapping_gnk.csv"

# Steel density used for tonnage <-> length conversions (austenitic ~7.9,
# ferritic ~7.7). Only used by the synthetic generator and sanity checks.
STAINLESS_DENSITY_T_PER_M3 = 7.9


# =============================================================================
# Workshops (ARMP level 2)
# =============================================================================

WORKSHOPS: dict[str, str] = {
    "ST": "Steel shop (melt shop + continuous casting + slab yard)",
    "CR": "Cold rolling (annealing & pickling, cold rolling mills, BA, skin pass)",
    "RP": "Finishing & packing (slitting, cut-to-length, levelling, polishing, packing)",
    "SS": "Aperam SteelCenter Limburg (service centre, external to the Genk route)",
}


# =============================================================================
# OperationStatus semantics (metal_movements.OperationStatus)
# =============================================================================

OPERATION_STATUS: dict[str, str] = {
    "EXEC": "executed - the operation has been carried out (the only status that counts as production)",
    "COUR": "in progress (en cours) - the coil is on the line right now",
    "DISP": "available (disponible) - the coil is released and waiting for this operation",
    "NDIS": "not available (non disponible) - planned but not yet releasable",
    "BLOQ": "blocked (bloque) - quality / technical hold, the coil cannot move on",
    "SUPP": "deleted (supprime) - planned operation cancelled, ignore for KPIs",
}

# Shape_FK master data (metal_movements.shape)
SHAPES: dict[int, str] = {1: "Unknown", 2: "Blank", 3: "Coil", 4: "Disc", 5: "Heat", 6: "Sheet", 7: "Slab"}


# =============================================================================
# Production lines (ARMP level 3)
# =============================================================================

@dataclass(frozen=True)
class LineSpec:
    """Published technical envelope of a line. ``None`` = not published."""

    width_mm: tuple[float, float] | None = None
    thickness_in_mm: tuple[float | None, float | None] | None = None  # entry thickness range
    thickness_out_mm: tuple[float | None, float | None] | None = None  # exit thickness range
    max_speed_m_min: float | None = None
    max_coil_t: float | None = None
    capacity_t_h: float | None = None
    capacity_t: float | None = None  # batch capacity (EAF / AOD / VOD / ladle)
    cycle_min: float | None = None  # tap-to-tap / batch cycle
    notes: str = ""


@dataclass(frozen=True)
class Line:
    """One production line of the GNK route, identified by its ARMP level-3 code."""

    armp: str  # ARMP level 3, e.g. GN-CR-APL3 (== metal_movements.Asset_Tree)
    name: str
    workshop: str  # ST / CR / RP / SS
    stage: int  # position on the route (10, 20, ...) used for WIP buffers & flow order
    stage_name: str
    kind: str  # process family, drives prompts and sensor templates
    machine_codes: tuple[str, ...]  # metal_movements.Machine values that map to this line
    rate_unit: str = "t/h"
    spec: LineSpec = field(default_factory=LineSpec)
    is_logistic: bool = False  # transport / reception / invoicing: no transformation
    remarks: str = ""


LINES: list[Line] = [
    # ── Steel shop ────────────────────────────────────────────────────────
    Line("GN-ST-EAF1", "Electric Arc Furnace 1", "ST", 10, "Melting", "eaf", ("OVN1", "EAF1", "AFS1"),
         rate_unit="t/heat",
         spec=LineSpec(capacity_t=100, cycle_min=65,
                       notes="96 MVA UHP transformer, eccentric bottom tapping, 1600-1700 C tapping, no gas burners")),
    Line("GN-ST-EAF2", "Electric Arc Furnace 2", "ST", 10, "Melting", "eaf", ("OVN2", "EAF2", "AFS2"),
         rate_unit="t/heat",
         spec=LineSpec(capacity_t=120, cycle_min=60,
                       notes="96 MVA, spout tapping, natural gas door burners, slag foaming, bottom stirring")),
    Line("GN-ST-AOD1", "AOD Converter", "ST", 20, "Refining", "aod", ("AOD1",), rate_unit="t/heat",
         spec=LineSpec(capacity_t=120, notes="decarburisation C 1.5-2% -> 0.03%, O2 up to 150 Nm3/min, argon stirring")),
    Line("GN-ST-VOD1", "VOD 1", "ST", 20, "Refining", "vod", ("VOD1",), rate_unit="t/heat",
         spec=LineSpec(capacity_t=120, notes="vacuum 200 mbar, alternative/complement to AOD")),
    Line("GN-ST-VOD2", "VOD 2", "ST", 20, "Refining", "vod", ("VOD2",), rate_unit="t/heat",
         spec=LineSpec(capacity_t=120, notes="vacuum 200 mbar")),
    Line("GN-ST-SLM1", "Melt finishing stand 1 (ladle)", "ST", 25, "Ladle metallurgy", "ladle", ("AFW1",), rate_unit="t/heat"),
    Line("GN-ST-SLM2", "Melt finishing stand 2 (ladle)", "ST", 25, "Ladle metallurgy", "ladle", ("AFW2",), rate_unit="t/heat"),
    Line("GN-ST-SLM3", "Melt finishing stand 3 (ladle)", "ST", 25, "Ladle metallurgy", "ladle", ("AFW3",), rate_unit="t/heat"),
    Line("GN-ST-CCS1", "Continuous slab caster", "ST", 30, "Casting", "caster", ("GIET", "KG"),
         spec=LineSpec(width_mm=(800, 2100), thickness_out_mm=(160, 200), max_speed_m_min=1.6,
                       notes="1 strand up to 2100 mm or twin mould 2x1050 mm, slabs up to 12 m / 15-33 t")),
    Line("GN-ST-RCU1", "Slab head & tail recutting", "ST", 35, "Slab conditioning", "slab_cut", ("HERS",)),
    Line("GN-ST-CCS1-40", "Slab grinders 1-4", "ST", 35, "Slab conditioning", "slab_grinder",
         ("SLP1", "SLP2", "SLP3", "SLP4", "SLPM"),
         spec=LineSpec(width_mm=(800, 2100), thickness_in_mm=(100, 300), max_coil_t=40,
                       notes="4 reversible grinders, removes ~2% thickness top & bottom, slab <= 400 C"),
         remarks="Grinders sit under GN-ST-CCS1-40-0x in ARMP; Asset_Tree (3 levels) reports them as GN-ST-CCS1."),
    Line("GN-ST-ETR1", "Slab transport to HSM Chatelet", "ST", 40, "Hot rolling (Chatelet)", "transport", ("VZCA",),
         is_logistic=True, rate_unit="t/shipment",
         spec=LineSpec(notes="train 6 h ~1200 t, 18/week; barge 15 h ~1200 t, 0-3/week. Genk has no hot strip mill")),
    # ── Cold rolling ──────────────────────────────────────────────────────
    Line("GN-CR-ETR1", "Transport HSM -> cold rolling", "CR", 45, "Black coil reception", "transport", ("TP",), is_logistic=True),
    Line("GN-CR-RCP1", "Black coil reception", "CR", 45, "Black coil reception", "reception", ("RCG",), is_logistic=True),
    Line("GN-CR-BAF1", "Batch (bell) annealing furnaces", "CR", 50, "Hot band annealing", "batch_annealing", ("KLOK",),
         spec=LineSpec(notes="box annealing for ferritic grades (e.g. 430), ~24 h heating + cooling cycle")),
    Line("GN-CR-CPL2", "Coil preparation line 2 (BVL2)", "CR", 50, "Hot band annealing", "coil_prep", ("BVL2",)),
    Line("GN-CR-APL3", "Annealing & pickling line 3 (BUL3 + SL3)", "CR", 55, "Hot band annealing & pickling", "hapl", ("BUL3",),
         spec=LineSpec(width_mm=(800, 2100), thickness_in_mm=(1.5, 13.5), max_speed_m_min=250, max_coil_t=40,
                       notes="furnace 1200 C, 156 m, 13 zones; process section 50/100 m/min; shot blasting, "
                             "electrolytic (Na2SO4) + mixed-acid pickling; in-line slitter SL3 / edge trimmer 1.0-6.1 mm")),
    Line("GN-CR-CRM2", "Cold rolling mill MKW2 (CVC 6-high)", "CR", 60, "Cold rolling", "crm_cvc6", ("MKW2",),
         spec=LineSpec(width_mm=(1000, 2100), thickness_in_mm=(None, 10), thickness_out_mm=(0.8, None),
                       max_speed_m_min=840, max_coil_t=45,
                       notes="reversible sexto, 500-2500 t rolling force, CVC + bending + HS flatness control")),
    Line("GN-CR-CRM3", "Cold rolling mill CRM3 (20-roll Sendzimir)", "CR", 60, "Cold rolling", "crm_z", ("CRM3",),
         spec=LineSpec(width_mm=(800, 1625), thickness_in_mm=(None, 6), thickness_out_mm=(0.4, None),
                       max_speed_m_min=800, max_coil_t=38,
                       notes="Sundwig 20-roll cluster, work rolls 68-100 mm, mineral oil, feeds BA route")),
    Line("GN-CR-CRM4", "Cold rolling mill CRM4 (CVC 4-high)", "CR", 60, "Cold rolling", "crm_cvc4", ("CRM4",),
         spec=LineSpec(width_mm=(800, 1600), thickness_in_mm=(None, 6), thickness_out_mm=(0.6, None),
                       max_speed_m_min=800, max_coil_t=36, notes="reversible quarto, 420 kN tension")),
    Line("GN-CR-CRM5", "Cold rolling mill CRM5", "CR", 60, "Cold rolling", "crm", ("CRM5",),
         remarks="Not described in the site poster; envelope to be completed by the process owner."),
    Line("GN-CR-APL2", "Annealing & pickling line 2 (BUL2 + SKP3)", "CR", 70, "Final annealing & pickling", "capl", ("BUL2",),
         spec=LineSpec(width_mm=(800, 2100), thickness_in_mm=(0.6, 8), max_speed_m_min=200, max_coil_t=40,
                       capacity_t_h=152,
                       notes="furnace 1200 C, 122 m, 20 zones, 234 burners; process 120 m/min; in-line skin pass SKP3 "
                             "(12000 kN, elongation 0.2-0.8%). Can run as first (HRAP) or final (2D/2B) A&P"),
         remarks="Dual role: first A&P on hot band or final A&P after cold rolling."),
    Line("GN-CR-APL1", "Annealing & pickling line 1 (BUL1)", "CR", 70, "Final annealing & pickling", "capl", ("BUL1",),
         spec=LineSpec(notes="'1500' line (1500 mm width class)")),
    Line("GN-CR-APL4", "Annealing & pickling line 4 (BUL4)", "CR", 70, "Final annealing & pickling", "capl", ("BUL4",),
         remarks="Role on the route to be confirmed with the process owner (not on the site poster)."),
    Line("GN-CR-BAL1", "Bright annealing line BAL1", "CR", 70, "Bright annealing", "bal", ("BAL1",),
         spec=LineSpec(width_mm=(550, 1520), thickness_in_mm=(0.4, 2.0), max_speed_m_min=100, max_coil_t=35,
                       notes="vertical muffle furnace (alloy 6025HT), 1160 C bottom / 1000 C top, H2/N2 atmosphere, "
                             "process 5-70 m/min; produces BA finish")),
    Line("GN-CR-SKP1", "Skin pass 1 (1500)", "CR", 80, "Skin pass", "skin_pass", ("SKP1",)),
    Line("GN-CR-SKP2", "Skin pass 2 (2000)", "CR", 80, "Skin pass", "skin_pass", ("SKP2",)),
    Line("GN-CR-TOC1", "Technical operation correction", "CR", 85, "Administrative", "admin", ("KW1",), is_logistic=True),
    Line("GN-CR-ETR2", "Transport Genk -> SteelCenter Limburg", "CR", 95, "Dispatch", "transport", ("VZSL",), is_logistic=True),
    # ── Finishing & packing ───────────────────────────────────────────────
    Line("GN-RP-SLT2", "Slitter BS2 (L light / Z heavy)", "RP", 90, "Finishing", "slitter", ("BS2L", "BS2Z"),
         remarks="Machine_Indication L (licht/light) or Z (zwaar/heavy) distinguishes the two heads (GN-RP-SLT2-02-01/02)."),
    Line("GN-RP-SLT5", "Slitting line 52", "RP", 90, "Finishing", "slitter", ("SL52",)),
    Line("GN-RP-SLT6", "Slitting line 60", "RP", 90, "Finishing", "slitter", ("SL60",)),
    Line("GN-RP-CTL2", "Cut-to-length line LS2 (L / Z)", "RP", 90, "Finishing", "ctl", ("LS2L", "LS2Z"),
         remarks="Machine_Indication L / Z as for BS2."),
    Line("GN-RP-SBL1", "Stretcher leveller STL1", "RP", 90, "Finishing", "stretch_leveller", ("STL1",),
         spec=LineSpec(width_mm=(300, 1575), thickness_in_mm=(0.3, 4), max_coil_t=35,
                       notes="50 m stretch length, 180 t stretching force, 60-65 s cycle per 50 m")),
    Line("GN-RP-CPO2", "Coil polishing line GR2", "RP", 90, "Finishing", "polishing", ("GR2",)),
    Line("GN-RP-PCK1", "Packing", "RP", 100, "Packing & dispatch", "packing", ("PACK", "PACKS", "PCK"),
         remarks="Only OperationNameLocal IN ('PCKI','PCKG','PCK') are packing operations."),
    Line("GN-RP-PRD1", "Package production", "RP", 100, "Packing & dispatch", "admin", ("PRDC",), is_logistic=True),
    Line("GN-RP-INV1", "Package invoicing", "RP", 110, "Packing & dispatch", "admin", ("INV",), is_logistic=True),
    Line("GN-RP-ETR1", "Package expedition", "RP", 110, "Packing & dispatch", "transport", ("EXPK",), is_logistic=True),
]

LINES_BY_ARMP: dict[str, Line] = {ln.armp: ln for ln in LINES}

# Core production lines of the Genk cold side, in flow order. This is what the
# control tower and the WIP buffers are drawn on.
COLD_ROUTE_ARMP: tuple[str, ...] = (
    "GN-CR-RCP1",
    "GN-CR-BAF1",
    "GN-CR-CPL2",
    "GN-CR-APL3",
    "GN-CR-CRM2",
    "GN-CR-CRM3",
    "GN-CR-CRM4",
    "GN-CR-CRM5",
    "GN-CR-APL2",
    "GN-CR-APL1",
    "GN-CR-APL4",
    "GN-CR-BAL1",
    "GN-CR-SKP1",
    "GN-CR-SKP2",
    "GN-RP-SLT2",
    "GN-RP-SLT5",
    "GN-RP-SLT6",
    "GN-RP-CTL2",
    "GN-RP-SBL1",
    "GN-RP-CPO2",
    "GN-RP-PCK1",
)


def armp_level3(armp: str | None) -> str | None:
    """Truncate an ARMP code to its first three levels (the Asset_Tree / Domain grain)."""
    if not armp:
        return None
    return "-".join(armp.split("-")[:3])


def line_for_armp(armp: str | None) -> Line | None:
    """Resolve any ARMP code (3 to 5 levels) to its Line."""
    if not armp:
        return None
    if armp in LINES_BY_ARMP:
        return LINES_BY_ARMP[armp]
    parts = armp.split("-")
    # Grinders are modelled at level 4 (GN-ST-CCS1-40); try 4 then 3 levels.
    for n in (4, 3):
        key = "-".join(parts[:n])
        if key in LINES_BY_ARMP:
            return LINES_BY_ARMP[key]
    return None


# =============================================================================
# Machine code mapping (metal_movements.Machine -> ARMP)
# =============================================================================

@dataclass(frozen=True)
class MachineMapping:
    machine_code: str
    description: str
    armp: str | None
    is_movement_only: bool  # Movement_Condition = 'Y': logistic / administrative, not a transformation
    comments: str
    movement_count: int | None


@lru_cache(maxsize=1)
def load_machine_mapping(path: Path = MAPPING_CSV) -> tuple[MachineMapping, ...]:
    rows: list[MachineMapping] = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["Site_FK"].strip() not in {"3", "2"}:
                continue
            count = r.get("COUNT_of_MetalMovementID") or ""
            rows.append(MachineMapping(
                machine_code=r["MachineCode_Local"].strip(),
                description=r["MachineCode_Local_Description"].strip(),
                armp=(r["ARMP"].strip() or None),
                is_movement_only=r["Movement_Condition"].strip().upper() == "Y",
                comments=r["Comments"].strip(),
                movement_count=int(count) if count.strip().isdigit() else None,
            ))
    return tuple(rows)


def machine_to_line() -> dict[str, Line]:
    """metal_movements.Machine -> Line, built from the declared LINES."""
    out: dict[str, Line] = {}
    for ln in LINES:
        for code in ln.machine_codes:
            out[code] = ln
    return out


# =============================================================================
# Product families and routes
# =============================================================================

@dataclass(frozen=True)
class Route:
    product: str  # finish family
    description: str
    steps: tuple[str, ...]  # ARMP level-3 codes (one representative per stage)
    share: float  # share of cold-side tonnage, synthetic generator only


ROUTES: list[Route] = [
    Route("HRAP", "Hot rolled, annealed & pickled (No.1)",
          ("GN-CR-RCP1", "GN-CR-APL3", "GN-RP-SLT6", "GN-RP-PCK1"), 0.20),
    Route("2D", "Cold rolled, annealed & pickled (dull)",
          ("GN-CR-RCP1", "GN-CR-APL3", "GN-CR-CRM2", "GN-CR-APL1", "GN-RP-SLT2", "GN-RP-PCK1"), 0.15),
    Route("2B", "Cold rolled, annealed, pickled & skin passed (bright)",
          ("GN-CR-RCP1", "GN-CR-APL3", "GN-CR-CRM2", "GN-CR-APL2", "GN-CR-SKP2", "GN-RP-SLT2", "GN-RP-PCK1"), 0.40),
    Route("2B-F", "Ferritic 2B via batch annealing",
          ("GN-CR-RCP1", "GN-CR-BAF1", "GN-CR-CPL2", "GN-CR-APL3", "GN-CR-CRM4", "GN-CR-APL2",
           "GN-CR-SKP1", "GN-RP-CTL2", "GN-RP-PCK1"), 0.10),
    Route("BA", "Bright annealed",
          ("GN-CR-RCP1", "GN-CR-APL3", "GN-CR-CRM3", "GN-CR-BAL1", "GN-CR-SKP1", "GN-RP-SLT2", "GN-RP-PCK1"), 0.15),
]

# Typical grades by family (EN / AISI). Synthetic only.
GRADES: dict[str, tuple[str, ...]] = {
    "austenitic": ("1.4301", "1.4307", "1.4404", "1.4541"),
    "ferritic": ("1.4016", "1.4509", "1.4512"),
}


# =============================================================================
# Shifts (Genk runs 3 x 8 h continuous shifts)
# =============================================================================

SHIFTS: tuple[tuple[str, int, int], ...] = (
    ("morning", 6, 14),
    ("afternoon", 14, 22),
    ("night", 22, 6),
)


# =============================================================================
# Sensor templates per line kind (synthetic generator + tag master data)
# =============================================================================

@dataclass(frozen=True)
class SensorTemplate:
    suffix: str  # appended to the ARMP L3 to form the TagId
    description: str
    unit: str
    nominal: float
    low: float  # Range_Lower_Limit
    high: float  # Range_Upper_Limit
    noise: float  # 1-sigma noise as fraction of nominal


SENSOR_TEMPLATES: dict[str, tuple[SensorTemplate, ...]] = {
    "hapl": (
        SensorTemplate("FUR.Z07.TEMP", "Furnace zone 7 temperature", "degC", 1120, 1050, 1200, 0.008),
        SensorTemplate("FUR.Z13.TEMP", "Furnace zone 13 temperature", "degC", 1100, 1030, 1200, 0.008),
        SensorTemplate("PRC.STRIP.SPEED", "Process section strip speed", "m/min", 60, 20, 100, 0.05),
        SensorTemplate("PCK.EL.CURRENT", "Electrolytic pickling current", "kA", 3.5, 1.0, 5.0, 0.06),
        SensorTemplate("PCK.ACID.HF", "Mixed acid bath HF concentration", "g/l", 25, 15, 35, 0.04),
        SensorTemplate("SHB.TURBINE.AMP", "Shot blaster turbine current", "A", 180, 120, 240, 0.05),
    ),
    "capl": (
        SensorTemplate("FUR.Z10.TEMP", "Furnace zone 10 temperature", "degC", 1100, 1020, 1200, 0.008),
        SensorTemplate("PRC.STRIP.SPEED", "Process section strip speed", "m/min", 90, 30, 120, 0.05),
        SensorTemplate("PCK.EL.CURRENT", "Electrolytic pickling current", "kA", 4.0, 1.0, 5.0, 0.06),
        SensorTemplate("SKP.ELONG", "In-line skin pass elongation", "%", 0.5, 0.2, 0.8, 0.10),
    ),
    "bal": (
        SensorTemplate("MUF.BOT.TEMP", "Muffle bottom temperature", "degC", 1140, 1080, 1160, 0.004),
        SensorTemplate("MUF.TOP.TEMP", "Muffle top temperature", "degC", 1010, 950, 1060, 0.006),
        SensorTemplate("ATM.DEWPOINT", "Furnace atmosphere dew point", "degC", -50, -70, -40, 0.04),
        SensorTemplate("ATM.H2", "H2 content in furnace atmosphere", "%", 75, 60, 100, 0.03),
        SensorTemplate("PRC.STRIP.SPEED", "Process section strip speed", "m/min", 35, 5, 70, 0.06),
    ),
    "crm_cvc6": (
        SensorTemplate("STD.ROLLFORCE", "Rolling force", "kN", 14000, 5000, 25000, 0.08),
        SensorTemplate("STD.SPEED", "Mill speed", "m/min", 600, 0, 840, 0.06),
        SensorTemplate("TEN.EXIT", "Exit strip tension", "kN", 250, 100, 320, 0.06),
        SensorTemplate("FLT.IUNIT", "Flatness deviation", "I-unit", 8, 0, 20, 0.25),
        SensorTemplate("EMUL.TEMP", "Emulsion temperature", "degC", 55, 45, 65, 0.03),
    ),
    "crm_cvc4": (
        SensorTemplate("STD.ROLLFORCE", "Rolling force", "kN", 12000, 5000, 22000, 0.08),
        SensorTemplate("STD.SPEED", "Mill speed", "m/min", 550, 0, 800, 0.06),
        SensorTemplate("TEN.EXIT", "Exit strip tension", "kN", 300, 100, 420, 0.06),
        SensorTemplate("FLT.IUNIT", "Flatness deviation", "I-unit", 8, 0, 20, 0.25),
    ),
    "crm_z": (
        SensorTemplate("STD.ROLLFORCE", "Rolling force", "kN", 8000, 3000, 12500, 0.08),
        SensorTemplate("STD.SPEED", "Mill speed", "m/min", 500, 0, 800, 0.06),
        SensorTemplate("OIL.TEMP", "Rolling oil temperature", "degC", 45, 35, 55, 0.03),
        SensorTemplate("TEN.EXIT", "Exit strip tension", "kN", 150, 30, 270, 0.06),
        SensorTemplate("THK.DEV", "Exit thickness deviation", "um", 0, -8, 8, 0.0),
    ),
    "crm": (
        SensorTemplate("STD.ROLLFORCE", "Rolling force", "kN", 10000, 3000, 20000, 0.08),
        SensorTemplate("STD.SPEED", "Mill speed", "m/min", 500, 0, 800, 0.06),
    ),
    "skin_pass": (
        SensorTemplate("ELONG", "Skin pass elongation", "%", 0.6, 0.2, 1.2, 0.10),
        SensorTemplate("ROLLFORCE", "Roll force", "kN", 6000, 2000, 12000, 0.08),
        SensorTemplate("STRIP.SPEED", "Strip speed", "m/min", 300, 0, 420, 0.06),
    ),
    "slitter": (
        SensorTemplate("STRIP.SPEED", "Strip speed", "m/min", 200, 0, 300, 0.07),
        SensorTemplate("KNIFE.LOAD", "Knife shaft motor load", "%", 55, 10, 90, 0.08),
    ),
    "ctl": (
        SensorTemplate("STRIP.SPEED", "Strip speed", "m/min", 60, 0, 100, 0.07),
        SensorTemplate("SHEAR.CYCLES", "Flying shear cycles per minute", "1/min", 20, 0, 40, 0.10),
    ),
    "stretch_leveller": (
        SensorTemplate("STRETCH.FORCE", "Stretching force", "t", 120, 20, 180, 0.08),
        SensorTemplate("CYCLE.TIME", "Stretch cycle time", "s", 62, 55, 75, 0.03),
    ),
    "batch_annealing": (
        SensorTemplate("BELL.TEMP", "Heating bell temperature", "degC", 820, 700, 880, 0.01),
    ),
}
