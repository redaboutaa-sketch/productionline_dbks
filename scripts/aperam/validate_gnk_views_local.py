"""Run the whole GNK SQL layer on local Spark against synthetic source data.

Needs ``pyspark>=4.0`` and a Java 17/21 runtime. No Databricks workspace needed.

    PYSPARK_PYTHON=$(which python) python scripts/aperam/validate_gnk_views_local.py

Local Spark has no Delta, so ``CREATE OR REPLACE TABLE`` is executed as DROP + CREATE.
Prints a short report per gold object and fails if any of them is empty.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pyspark.sql import SparkSession  # noqa: E402

from src.aperam_gnk.synthetic import (  # noqa: E402
    METAL_MOVEMENTS_DDL,
    TIMEBASED_SENSOR_DDL,
    generate,
    to_spark_rows,
)
from src.aperam_gnk.views import render_statements  # noqa: E402

GOLD = [
    "gnk_line_registry", "gnk_machine_map", "gnk_sensor_tags", "gnk_movements", "gold_gnk_coil_ops",
    "gold_gnk_line_kpi_shift_oee", "gold_gnk_line_kpi_daily", "gold_gnk_line_reference_rate",
    "gold_gnk_coil_position", "gold_gnk_wip_buffers", "gold_gnk_line_gaps", "gold_gnk_sensor_health_hourly",
    "gold_gnk_sensor_by_coil",
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gnk_spark_"))
    spark = (
        SparkSession.builder.master("local[2]")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", str(tmp / "wh"))
        .config("spark.driver.extraJavaOptions", f"-Dderby.system.home={tmp / 'derby'}")
        .config("spark.sql.shuffle.partitions", "4")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    mm, sensors = generate(days=3, sensor_step_s=120)
    for db in ("src", "tgt"):
        spark.sql(f"CREATE DATABASE IF NOT EXISTS {db}")
    spark.createDataFrame(to_spark_rows(mm, METAL_MOVEMENTS_DDL), METAL_MOVEMENTS_DDL) \
        .write.mode("overwrite").saveAsTable("src.metal_movements")
    spark.createDataFrame(to_spark_rows(sensors, TIMEBASED_SENSOR_DDL), TIMEBASED_SENSOR_DDL) \
        .write.mode("overwrite").saveAsTable("src.timebased_sensor")

    for stmt in render_statements("src", "tgt", sensor_lookback_days=30):
        if stmt.startswith("CREATE OR REPLACE TABLE"):
            spark.sql(f"DROP TABLE IF EXISTS {stmt.split()[4]}")
            stmt = stmt.replace("CREATE OR REPLACE TABLE", "CREATE TABLE", 1)
        spark.sql(stmt)

    empty = []
    for obj in GOLD:
        n = spark.table(f"tgt.{obj}").count()
        print(f"{obj:34s} {n:>8,d}")
        if n == 0:
            empty.append(obj)
    spark.sql("SELECT * FROM tgt.gold_gnk_wip_buffers ORDER BY tonnes_waiting DESC").show(10, False)
    failures = _run_agent_sql(spark)
    spark.sql("SELECT line_armp, gap_start, gap_min FROM tgt.gold_gnk_line_gaps ORDER BY gap_min DESC").show(5, False)
    spark.stop()
    if empty:
        print("EMPTY:", ", ".join(empty))
    return 1 if (empty or failures) else 0


def _run_agent_sql(spark) -> int:
    """Execute the GNK context builder and the four GNK tools against the local views.

    Needs the agent-server dependencies (langchain...); skipped with a notice otherwise.
    """
    try:
        import agent_server.context_builder as cb
        from agent_server.aperam_gnk import context as gctx
        from agent_server.aperam_gnk import tools as gtools
    except ImportError as exc:
        print(f"[agent SQL] skipped, agent-server deps missing: {exc}")
        return 0

    spark.sql("USE tgt")
    errors: list[str] = []

    def run(sql_text: str, params: dict, label: str = "") -> list[dict]:
        lit = {k: "'" + str(v).replace("'", "''") + "'" for k, v in (params or {}).items()}
        try:
            return [r.asDict() for r in spark.sql(sql_text % lit if lit else sql_text).collect()]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {str(exc).splitlines()[0][:200]}")
            return []

    cb._parameterized_query = run
    ctx = gctx.build_gnk_context(None)
    print(f"[agent SQL] context: shift={ctx['scenario']['shift_id']} lines={len(ctx['line_state'])} "
          f"events={len(ctx['active_events'])} wip={len(ctx['wip_buffers'])} kpi={ctx['current_kpi']}")
    for m in ctx["line_state"]:
        if m["status"] != "running":
            print(f"   {m['machine_id']:14s} {m['status']:9s} idle={m['idle_min']} waiting={m['coils_waiting']}")
    for e in ctx["active_events"][:6]:
        print(f"   EVENT {e['severity']:6s} {e['machine_id']} {e['event_type']}: {e['description'][:110]}")
    status = gtools.gnk_line_status.invoke({"line_armp": "GN-CR-CRM2", "hours": 72})
    print(f"[agent SQL] gnk_line_status CRM2: shifts={len(status['kpi_by_shift'])} stops={status['stops'][:1]}")
    some_metal = (ctx["blocked_coils"] or [{"metal": None}])[0]["metal"]
    if some_metal:
        trace = gtools.gnk_coil_trace.invoke({"metal": some_metal})
        print(f"[agent SQL] gnk_coil_trace {some_metal}: ops={len(trace['operations'])} "
              f"sensor rows={len(trace['sensor_profile'])}")
    scan = gtools.gnk_sensor_scan.invoke({"line_armp": "GN-CR-BAL1", "hours": 48})
    print("[agent SQL] gnk_sensor_scan BAL1 top:", [(t["tag_id"], t["z_recent_vs_baseline"]) for t in scan["tags"][:3]])
    flow = gtools.gnk_flow_overview.invoke({})
    print("[agent SQL] gnk_flow_overview:", [(w["line_armp"], w["queue_cover_h"]) for w in flow["queues_in_flow_order"][:5]])
    for e in errors:
        print("[agent SQL] ERROR", e)
    return len(errors)


if __name__ == "__main__":
    raise SystemExit(main())
