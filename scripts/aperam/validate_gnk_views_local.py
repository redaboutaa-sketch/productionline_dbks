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
    spark.sql("SELECT line_armp, gap_start, gap_min FROM tgt.gold_gnk_line_gaps ORDER BY gap_min DESC").show(5, False)
    spark.stop()
    if empty:
        print("EMPTY:", ", ".join(empty))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
