"""Render and deploy the GNK silver/gold layer on Unity Catalog.

Usage (from the repo root)::

    # print the SQL without touching the workspace
    python -m src.aperam_gnk.views --src aperam_prod.manufacturing --tgt aperam_dev.gnk_copilot --dry-run

    # deploy on a SQL warehouse (ambient Databricks auth: CLI profile, env vars or job identity)
    python -m src.aperam_gnk.views --src aperam_prod.manufacturing --tgt aperam_dev.gnk_copilot \
        --warehouse-id <id>

``--src`` is the schema holding ``metal_movements`` and ``timebased_sensor``.
``--tgt`` is the schema the copilot reads (``UC_CATALOG``.``UC_SCHEMA`` of the app).

The reference tables (line registry, machine mapping, sensor tag catalogue) are
generated from :mod:`src.aperam_gnk.assets` so the ARMP model lives in one place.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from src.aperam_gnk.assets import (
    LINES,
    SENSOR_TEMPLATES,
    SITE_TZ,
    load_machine_mapping,
    machine_to_line,
)

SQL_DIR = Path(__file__).parent / "sql"
_IDENT = re.compile(r"^[A-Za-z0-9_`.\-]+$")


def _q(value: object) -> str:
    """SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _values_table(target: str, columns: list[tuple[str, str]], rows: Iterable[tuple]) -> str:
    cols_sql = ", ".join(f"CAST({name} AS {typ}) AS {name}" for name, typ in columns)
    names = ", ".join(name for name, _ in columns)
    values = ",\n  ".join("(" + ", ".join(_q(v) for v in row) + ")" for row in rows)
    return (
        f"CREATE OR REPLACE TABLE {target} AS\n"
        f"SELECT {cols_sql}\nFROM VALUES\n  {values}\nAS t({names})"
    )


RESOURCE_MAP_CSV = Path(__file__).parent / "reference" / "resource_map_gnk.csv"
DEFAULT_INDICATORS_TABLE = "dev_weu_next_best_action.raw.indicators_day_label"
TIME_MODEL_SQL = "60_gold_gnk_time_model.sql"


def load_resource_map(path: Path = RESOURCE_MAP_CSV) -> list[tuple[str, str | None, str | None]]:
    """indicators_day_label.resource -> ARMP line (blank line_armp = not mapped yet)."""
    import csv

    with path.open(encoding="utf-8") as fh:
        return [(r["resource"].strip(), (r.get("line_armp") or "").strip() or None, (r.get("comment") or "").strip() or None)
                for r in csv.DictReader(fh) if r.get("resource", "").strip()]


def reference_statements(tgt: str, extra_resource_map: list[tuple[str, str | None, str | None]] | None = None) -> list[str]:
    """CREATE TABLE statements for the reference data owned by assets.py."""
    registry = _values_table(
        f"{tgt}.gnk_line_registry",
        [
            ("line_armp", "STRING"), ("line_name", "STRING"), ("workshop", "STRING"), ("stage", "INT"),
            ("stage_name", "STRING"), ("kind", "STRING"), ("machine_codes", "STRING"), ("is_logistic", "BOOLEAN"),
            ("rate_unit", "STRING"), ("width_min_mm", "DOUBLE"), ("width_max_mm", "DOUBLE"),
            ("thickness_in_min_mm", "DOUBLE"), ("thickness_in_max_mm", "DOUBLE"),
            ("thickness_out_min_mm", "DOUBLE"), ("max_speed_m_min", "DOUBLE"), ("max_coil_t", "DOUBLE"),
            ("capacity_t_h", "DOUBLE"), ("capacity_t", "DOUBLE"), ("cycle_min", "DOUBLE"),
            ("spec_notes", "STRING"), ("remarks", "STRING"),
        ],
        (
            (
                ln.armp, ln.name, ln.workshop, ln.stage, ln.stage_name, ln.kind, ",".join(ln.machine_codes),
                ln.is_logistic, ln.rate_unit,
                ln.spec.width_mm[0] if ln.spec.width_mm else None,
                ln.spec.width_mm[1] if ln.spec.width_mm else None,
                ln.spec.thickness_in_mm[0] if ln.spec.thickness_in_mm else None,
                ln.spec.thickness_in_mm[1] if ln.spec.thickness_in_mm else None,
                ln.spec.thickness_out_mm[0] if ln.spec.thickness_out_mm else None,
                ln.spec.max_speed_m_min, ln.spec.max_coil_t, ln.spec.capacity_t_h, ln.spec.capacity_t,
                ln.spec.cycle_min, ln.spec.notes or None, ln.remarks or None,
            )
            for ln in LINES
        ),
    )

    m2l = machine_to_line()
    mapping = _values_table(
        f"{tgt}.gnk_machine_map",
        [
            ("machine_code", "STRING"), ("description", "STRING"), ("armp", "STRING"), ("line_armp", "STRING"),
            ("is_movement_only", "BOOLEAN"), ("comments", "STRING"), ("movement_count", "BIGINT"),
        ],
        (
            (
                m.machine_code, m.description or None, m.armp,
                m2l[m.machine_code].armp if m.machine_code in m2l else None,
                m.is_movement_only, m.comments or None, m.movement_count,
            )
            for m in load_machine_mapping()
        ),
    )

    tag_rows = []
    for ln in LINES:
        for t in SENSOR_TEMPLATES.get(ln.kind, ()):
            tag_rows.append((f"{ln.armp}.{t.suffix}", ln.armp, t.description, t.unit, t.nominal, t.low, t.high))
    tags = _values_table(
        f"{tgt}.gnk_sensor_tags",
        [
            ("tag_id", "STRING"), ("armp_l3", "STRING"), ("description", "STRING"), ("unit", "STRING"),
            ("nominal_value", "DOUBLE"), ("range_lower_limit", "DOUBLE"), ("range_upper_limit", "DOUBLE"),
        ],
        tag_rows,
    )
    resource_map = _values_table(
        f"{tgt}.gnk_resource_map",
        [("resource", "STRING"), ("line_armp", "STRING"), ("comment", "STRING")],
        [*load_resource_map(), *(extra_resource_map or [])],
    )
    return [registry, mapping, tags, resource_map]


def _split_statements(sql: str) -> Iterator[str]:
    """Drop ``--`` comment lines, then split on ';' at end of line (no ';' inside literals)."""
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for chunk in re.split(r";\s*$", code, flags=re.MULTILINE):
        body = chunk.strip()
        if body:
            yield body


def now_local_sql(tz: str = SITE_TZ) -> str:
    """'Now' in site local time. Source timestamps are local, the warehouse clock is UTC."""
    return f"from_utc_timestamp(current_timestamp(), '{tz}')"


def render_statements(src: str, tgt: str, sensor_lookback_days: int = 30,
                      include_reference: bool = True, indicators: str | None = DEFAULT_INDICATORS_TABLE,
                      extra_resource_map: list[tuple[str, str | None, str | None]] | None = None) -> list[str]:
    """All statements in dependency order. ``indicators`` = fully qualified daily OEE indicators table
    (``None`` skips the time-model views)."""
    for ident in (src, tgt, indicators or "x.y"):
        if not _IDENT.match(ident):
            raise ValueError(f"invalid catalog.schema identifier: {ident!r}")
    stmts = reference_statements(tgt, extra_resource_map) if include_reference else []
    for path in sorted(SQL_DIR.glob("*.sql")):
        if path.name == TIME_MODEL_SQL and not indicators:
            continue
        text = path.read_text(encoding="utf-8").format(
            src=src, tgt=tgt, sensor_lookback_days=int(sensor_lookback_days), now_local=now_local_sql(),
            indicators=indicators,
        )
        stmts.extend(_split_statements(text))
    return stmts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="catalog.schema holding metal_movements and timebased_sensor")
    ap.add_argument("--tgt", required=True, help="catalog.schema read by the copilot")
    ap.add_argument("--warehouse-id", help="SQL warehouse id (required unless --dry-run)")
    ap.add_argument("--sensor-lookback-days", type=int, default=30)
    ap.add_argument("--indicators", default=DEFAULT_INDICATORS_TABLE,
                    help="catalog.schema.table of the daily OEE indicators ('' to skip the time model)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    stmts = render_statements(args.src, args.tgt, args.sensor_lookback_days, indicators=args.indicators or None)
    if args.dry_run:
        print(";\n\n".join(stmts) + ";")
        return 0
    if not args.warehouse_id:
        ap.error("--warehouse-id is required unless --dry-run")

    from scripts._uc_sql import StmtCursor  # lazy: needs databricks-sdk

    cur = StmtCursor(args.warehouse_id)
    for i, stmt in enumerate(stmts, 1):
        first_line = stmt.splitlines()[0][:100]
        print(f"[{i}/{len(stmts)}] {first_line}")
        cur.execute(stmt)
    cur.close()
    print("GNK views deployed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
