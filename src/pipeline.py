"""pipeline.py — medallion bronze -> silver (dimensional) -> gold, en tres variantes.

Variante A: sin verificación (lectura defensiva: el defecto avanza en silencio).
Variante B: restricciones del motor (NOT NULL + CHECK de Delta Lake): una
            violación aborta la escritura de la tabla de hechos.
Variante C: contratos ODCS verificados en cada frontera (ver runner.gate).

Fronteras:
  frontera 1 = ingesta -> intermedia   (bronze -> silver)
  frontera 2 = intermedia -> consumo   (silver -> gold)

Comparabilidad del costo: la tabla de hechos se materializa con la misma ruta de
escritura en las tres variantes (crear vacía + append), de modo que la diferencia
de tiempo sea atribuible a la verificación y no al modo de escritura.
"""
from __future__ import annotations
import time
from pyspark.sql import DataFrame, SparkSession, functions as F

from .core import geo_lookup_df, physical_name

FACT_TABLE = "fact_ventas"
ALL_TABLES = ["bronze_ventas", "dim_cliente", "dim_producto", "dim_tiempo",
              "dim_geografia", FACT_TABLE, "gold_ventas_region"]

# Esquema físico de la tabla de hechos, el mismo en las tres variantes.
FACT_DDL = [("invoice", "STRING"), ("stock_code", "STRING"),
            ("customer_id", "BIGINT"), ("date_key", "DATE"),
            ("country", "STRING"), ("quantity", "INT"), ("price", "DOUBLE"),
            ("amount", "DOUBLE")]

# Restricciones de la variante B. NOT NULL se declara al crear la tabla porque
# delta-spark rechaza endurecer la nulabilidad de una columna existente
# ("Cannot change nullable column to non-nullable", delta-io/delta#831); en
# Databricks Runtime el ALTER sí funciona. Los CHECK se agregan por ALTER TABLE.
B_NOT_NULL = ["quantity", "price", "customer_id"]
B_CHECKS = {"price_min": "price >= 0",
            "quantity_range": "quantity BETWEEN -1000 AND 10000"}


def _write(df: DataFrame, table: str, write_mode: str = "overwrite"):
    df.write.format("delta").mode(write_mode).saveAsTable(table)


# ---------------------------------------------------------------------- bronze
def build_bronze(spark: SparkSession, csv_path: str):
    """La capa de ingesta conserva el dato crudo: todo como texto, sin inferir
    esquema. Un cast implícito acá enmascararía las fallas de tipo."""
    df = spark.read.option("header", True).csv(csv_path)
    # Normaliza identificadores (ver core.physical_name); no toca valores,
    # tipos ni orden de columnas.
    _write(df.toDF(*[physical_name(c) for c in df.columns]), "bronze_ventas")


# ---------------------------------------------------------------------- silver
def _col(df: DataFrame, name: str):
    """Lectura defensiva: si la columna no está (deriva de esquema), null."""
    return F.col(f"`{name}`") if name in df.columns else F.lit(None).cast("string")


def _project(b: DataFrame) -> DataFrame:
    return b.select(
        _col(b, "Invoice").alias("invoice"),
        _col(b, "StockCode").alias("stock_code"),
        _col(b, "Description").alias("description"),
        _col(b, "Quantity").cast("int").alias("quantity"),
        F.to_timestamp(_col(b, "InvoiceDate")).alias("ts"),
        _col(b, "Price").cast("double").alias("price"),
        _col(b, physical_name("Customer ID")).cast("double").cast("long")
            .alias("customer_id"),
        _col(b, "Country").alias("country"),
    ).withColumn("date_key", F.to_date("ts"))


def build_silver(spark: SparkSession, variant: str) -> float:
    """Escribe las dimensiones y la tabla de hechos. Devuelve el tiempo, en ms,
    de crear la tabla de hechos (con sus restricciones en la variante B)."""
    base = _project(spark.table("bronze_ventas"))
    dims = {
        "dim_cliente": (base.where(F.col("customer_id").isNotNull())
                        .groupBy("customer_id")
                        .agg(F.first("country").alias("country"))),
        "dim_producto": (base.groupBy("stock_code")
                         .agg(F.first("description").alias("description"))),
        "dim_tiempo": (base.select("date_key").where(F.col("date_key").isNotNull())
                       .distinct()
                       .withColumn("year", F.year("date_key"))
                       .withColumn("month", F.month("date_key"))),
        "dim_geografia": spark.createDataFrame(geo_lookup_df()),
    }
    for name, df in dims.items():
        _write(df, name)

    fact = (base.select("invoice", "stock_code", "customer_id", "date_key",
                        "country", "quantity", "price")
                .withColumn("amount", F.round(F.col("quantity") * F.col("price"), 2)))

    # Crear la tabla vacía y hacer append, igual en las tres variantes. En B,
    # el append aborta con DELTA_NOT_NULL_CONSTRAINT_VIOLATED o
    # DELTA_VIOLATE_CONSTRAINT si alguna fila viola una restricción.
    t0 = time.perf_counter()
    cols = ", ".join(
        f"{n} {t}" + (" NOT NULL" if variant == "B" and n in B_NOT_NULL else "")
        for n, t in FACT_DDL)
    spark.sql(f"CREATE TABLE {FACT_TABLE} ({cols}) USING delta")
    if variant == "B":
        for name, expr in B_CHECKS.items():
            spark.sql(f"ALTER TABLE {FACT_TABLE} ADD CONSTRAINT {name} CHECK ({expr})")
    ddl_ms = (time.perf_counter() - t0) * 1000
    _write(fact, FACT_TABLE, write_mode="append")
    return round(ddl_ms, 1)


# ------------------------------------------------------------------------ gold
def build_gold(spark: SparkSession):
    f = spark.table(FACT_TABLE)
    g = spark.table("dim_geografia")
    t = spark.table("dim_tiempo")
    gold = (f.join(g, on="country", how="left")
             .join(t, on="date_key", how="left")
             .groupBy("region", "year", "month")
             .agg(F.round(F.sum("amount"), 2).alias("total_amount"),
                  F.count("*").alias("n_lines")))
    _write(gold, "gold_ventas_region")
