"""core.py — sesión Spark, población experimental y dimensión geográfica.

La población experimental se obtiene de Online Retail II (UCI, CC BY 4.0). El
conjunto crudo no satisface el contrato de la fuente; `prepare_population`
documenta cuánto se descarta y por qué cláusula.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from delta import configure_spark_with_delta_pip
from pyspark.sql import SparkSession

SOURCE_COLUMNS = ["Invoice", "StockCode", "Description", "Quantity",
                  "InvoiceDate", "Price", "Customer ID", "Country"]


def physical_name(name: str) -> str:
    """Nombre físico de una columna en la capa de ingesta.

    Delta Lake no admite espacios en nombres de columna salvo que se habilite
    column mapping. La capa de ingesta normaliza entonces el IDENTIFICADOR (no
    el valor, ni el tipo, ni el orden): 'Customer ID' -> 'Customer_ID'. El
    contrato declara ambos nombres, que es para lo que ODCS define
    `physicalName`; el esquema lógico de la fuente no cambia.
    """
    return name.strip().replace(" ", "_")


# Dominio operativo declarado en contracts/bronze_ventas.odcs.yaml. Se replica
# acá para construir una población conforme; no se deriva de las clases de falla.
QTY_MIN, QTY_MAX = -1000, 10000
PRICE_MIN, PRICE_MAX = 0.0, 50000.0


# ---------------------------------------------------------------- spark session
def get_spark(warehouse: str):
    """Sesión local con Delta Lake. Los JARs se bajan en el build de la imagen."""
    b = (
        SparkSession.builder.appName("tfg-data-contracts")
        .master("local[2]")
        .config("spark.sql.warehouse.dir", warehouse)
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.driver.memory", "3g")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog",
                "org.apache.spark.sql.delta.catalog.DeltaCatalog")
    )
    return configure_spark_with_delta_pip(b).getOrCreate()


# ------------------------------------------------------- dimensión geográfica
# Dimensión conformada. El dominio de países es el de la fuente (los 43 valores
# distintos de Online Retail II); la correspondencia región/continente es propia.
# Un país fuera de este dominio viola la integridad referencial y el motor no lo
# impide, porque no aplica claves foráneas. "European Community" y "Unspecified"
# no son países pero están en el dominio de la fuente: región no determinada.
COUNTRY_REGION = {
    "Australia":            ("Oceania",          "Oceania"),
    "Austria":              ("Western Europe",   "Europe"),
    "Bahrain":              ("Middle East",      "Asia"),
    "Belgium":              ("Western Europe",   "Europe"),
    "Bermuda":              ("Caribbean",        "America"),
    "Brazil":               ("South America",    "America"),
    "Canada":               ("North America",    "America"),
    "Channel Islands":      ("Northern Europe",  "Europe"),
    "Cyprus":               ("Southern Europe",  "Europe"),
    "Czech Republic":       ("Eastern Europe",   "Europe"),
    "Denmark":              ("Northern Europe",  "Europe"),
    "EIRE":                 ("Western Europe",   "Europe"),
    "European Community":   ("No determinada",   "No determinado"),
    "Finland":              ("Northern Europe",  "Europe"),
    "France":               ("Western Europe",   "Europe"),
    "Germany":              ("Western Europe",   "Europe"),
    "Greece":               ("Southern Europe",  "Europe"),
    "Hong Kong":            ("East Asia",        "Asia"),
    "Iceland":              ("Northern Europe",  "Europe"),
    "Israel":               ("Middle East",      "Asia"),
    "Italy":                ("Southern Europe",  "Europe"),
    "Japan":                ("East Asia",        "Asia"),
    "Korea":                ("East Asia",        "Asia"),
    "Lebanon":              ("Middle East",      "Asia"),
    "Lithuania":            ("Northern Europe",  "Europe"),
    "Malta":                ("Southern Europe",  "Europe"),
    "Netherlands":          ("Western Europe",   "Europe"),
    "Nigeria":              ("West Africa",      "Africa"),
    "Norway":               ("Northern Europe",  "Europe"),
    "Poland":               ("Eastern Europe",   "Europe"),
    "Portugal":             ("Southern Europe",  "Europe"),
    "RSA":                  ("Southern Africa",  "Africa"),
    "Saudi Arabia":         ("Middle East",      "Asia"),
    "Singapore":            ("Southeast Asia",   "Asia"),
    "Spain":                ("Southern Europe",  "Europe"),
    "Sweden":               ("Northern Europe",  "Europe"),
    "Switzerland":          ("Western Europe",   "Europe"),
    "Thailand":             ("Southeast Asia",   "Asia"),
    "USA":                  ("North America",    "America"),
    "United Arab Emirates": ("Middle East",      "Asia"),
    "United Kingdom":       ("Western Europe",   "Europe"),
    "Unspecified":          ("No determinada",   "No determinado"),
    "West Indies":          ("Caribbean",        "America"),
}


def geo_lookup_df() -> pd.DataFrame:
    rows = [(c, r, cont) for c, (r, cont) in COUNTRY_REGION.items()]
    return pd.DataFrame(rows, columns=["country", "region", "continent"])


# --------------------------------------------------- población experimental
def _fuera(s: pd.Series, lo: float, hi: float) -> pd.Series:
    return (s < lo) | (s > hi)


def prepare_population(csv_path: str) -> tuple[pd.DataFrame, dict]:
    """Lee Online Retail II y devuelve (población conforme al contrato, informe).

    La inyección de fallas exige una línea de base sin defectos: de otro modo no
    puede atribuirse una detección a la falla inyectada. Los criterios de
    descarte son exactamente las cláusulas del contrato de la fuente, y el
    informe cuantifica cada una (dato reportado en Resultados).
    """
    df = pd.read_csv(csv_path, dtype={"Invoice": str, "StockCode": str,
                                      "Description": str, "Country": str})
    rep = {"filas_crudas": len(df)}
    n0 = len(df)

    # Conteo por cláusula sobre el conjunto crudo, cada una por separado y no
    # de forma secuencial: cuántas filas ya incumplen el contrato antes de
    # inyectar ninguna falla.
    rep["crudo_customer_id_nulo"] = int(df["Customer ID"].isna().sum())
    rep["crudo_clave_duplicada"] = int(
        df.duplicated(subset=["Invoice", "StockCode"], keep="first").sum())
    rep["crudo_precio_fuera_dominio"] = int(_fuera(df["Price"], PRICE_MIN, PRICE_MAX).sum())
    rep["crudo_cantidad_fuera_dominio"] = int(_fuera(df["Quantity"], QTY_MIN, QTY_MAX).sum())
    rep["crudo_pais_fuera_dimension"] = int((~df["Country"].isin(COUNTRY_REGION)).sum())
    rep["crudo_descripcion_nula"] = int(df["Description"].isna().sum())
    rep["crudo_paises_distintos"] = int(df["Country"].nunique())

    # Descarte secuencial, en este orden: el duplicado se evalúa al final,
    # sobre las filas que ya cumplen el resto de las cláusulas.
    m_cust = df["Customer ID"].isna()
    rep["desc_customer_id_nulo"] = int(m_cust.sum())
    df = df[~m_cust]

    m_price = _fuera(df["Price"], PRICE_MIN, PRICE_MAX)
    rep["desc_precio_fuera_dominio"] = int(m_price.sum())
    df = df[~m_price]

    m_qty = _fuera(df["Quantity"], QTY_MIN, QTY_MAX)
    rep["desc_cantidad_fuera_dominio"] = int(m_qty.sum())
    df = df[~m_qty]

    m_geo = ~df["Country"].isin(COUNTRY_REGION)
    rep["desc_pais_fuera_dimension"] = int(m_geo.sum())
    df = df[~m_geo]

    m_dup = df.duplicated(subset=["Invoice", "StockCode"], keep="first")
    rep["desc_clave_duplicada"] = int(m_dup.sum())
    df = df[~m_dup]

    df = df[SOURCE_COLUMNS].reset_index(drop=True)
    rep["filas_poblacion"] = len(df)
    rep["descartadas"] = n0 - len(df)
    rep["pct_descartadas"] = round(100.0 * (n0 - len(df)) / n0, 2)
    rep["facturas"] = int(df["Invoice"].nunique())
    rep["paises"] = int(df["Country"].nunique())
    return df, rep


def sample_population(pop: pd.DataFrame, n_rows: int, seed: int) -> pd.DataFrame:
    """Muestra determinística a nivel de factura: preserva el grano de línea de
    factura y la estructura transaccional (una factura no se parte)."""
    rng = np.random.default_rng(seed)
    invoices = pop["Invoice"].drop_duplicates().to_numpy()
    rng.shuffle(invoices)
    sizes = pop.groupby("Invoice", sort=False).size()
    take, acc = [], 0
    for inv in invoices:
        take.append(inv)
        acc += int(sizes[inv])
        if acc >= n_rows:
            break
    return pop[pop["Invoice"].isin(take)].reset_index(drop=True)
