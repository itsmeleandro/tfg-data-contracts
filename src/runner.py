"""runner.py — verificación de contratos ODCS y ejecución de la matriz.

Ejecuta los checks declarados en contracts/*.odcs.yaml contra tablas Spark:
un subconjunto de ODCS v3 con logicalType, required, unique, range y
referential. Se puede reemplazar por datacontract-cli como librería, con la
misma especificación YAML; está implementado acá para evitar el extra de spark
del CLI, que rompe la sesión en entornos serverless.

Uso (los valores por defecto son la configuración de la corrida del paper):
    python -m src.runner [--reps 5] [--rows 50000] [--no-warmup]
        [--source data/online_retail_II.csv] [--out results/raw_runs.csv]
"""
from __future__ import annotations
import argparse, csv, json, os, shutil, tempfile, time
import yaml
from pyspark.sql import SparkSession, functions as F

from .core import get_spark, prepare_population, sample_population
from .injector import FAULTS, DOMAIN_SENTINEL, inject
from .pipeline import ALL_TABLES, FACT_TABLE, build_bronze, build_gold, build_silver

CONTRACTS_DIR = os.path.join(os.path.dirname(__file__), "..", "contracts")

# Errores de Delta que son violación de restricción, y solo esos. Un criterio
# más amplio (buscar la subcadena "DELTA_", por ejemplo) cuenta como detección
# cualquier fallo del motor.
DELTA_CONSTRAINT_ERRORS = (
    "DELTA_NOT_NULL_CONSTRAINT_VIOLATED",
    "DELTA_VIOLATE_CONSTRAINT",
    "InvariantViolationException",
    "CHECK constraint",
)


# ============================================================ contracts engine
class BoundaryViolation(Exception):
    def __init__(self, layer: str, details: list[str]):
        super().__init__(f"[{layer}] " + "; ".join(details[:5]))
        self.layer, self.details = layer, details


def load_contract(name: str) -> dict:
    with open(os.path.join(CONTRACTS_DIR, f"{name}.odcs.yaml")) as f:
        return yaml.safe_load(f)


def check_contract(spark: SparkSession, table: str, contract: dict) -> list[str]:
    df = spark.table(table)
    spec = contract["schema"][0]
    props = spec.get("properties", [])

    # Nombre lógico -> físico (ODCS `physicalName`): el contrato usa el nombre
    # del diccionario de datos; la tabla, el identificador que admite el formato.
    phys = {p["name"]: p.get("physicalName", p["name"]) for p in props}
    present = lambda name: phys.get(name, name) in df.columns
    col = lambda name: F.col(f"`{phys.get(name, name)}`")

    violations = [f"schema: falta la columna declarada '{n}'"
                  for n in phys if not present(n)]
    violations += [f"schema: columna no declarada '{c}'"
                   for c in df.columns if c not in phys.values()]

    types = dict(df.dtypes)
    for p in props:
        name, ltype = p["name"], p.get("logicalType")
        if not present(name):
            continue
        c = col(name)
        if p.get("required") and df.where(c.isNull()).limit(1).count():
            violations.append(f"required: nulos en '{name}'")
        if ltype in ("integer", "number") and types[phys[name]] == "string":
            cast = "int" if ltype == "integer" else "double"
            if df.where(c.isNotNull() & c.cast(cast).isNull()).limit(1).count():
                violations.append(f"type: '{name}' no castea a {ltype}")

    for q in spec.get("quality", []):
        rule, names = q["rule"], q.get("columns") or [q["column"]]
        if not all(map(present, names)):
            continue  # ya reportado como violación de esquema
        if rule == "unique":
            ok = df.select(*map(col, names)).distinct().count() == df.count()
        elif rule == "range":
            c = col(q["column"]).cast("double")
            fuera = c.isNotNull() & ((c < q["min"]) | (c > q["max"]))
            ok = df.where(fuera).limit(1).count() == 0
        elif rule == "referential":
            ref = (spark.table(q["refTable"])
                   .select(F.col(q["refColumn"]).alias("__ref")).distinct())
            ok = (df.select(col(q["column"]).alias("__ref")).distinct()
                    .join(ref, "__ref", "left_anti").limit(1).count()) == 0
        else:
            raise ValueError(f"regla de calidad no soportada: {rule}")
        if not ok:
            violations.append(f"quality[{rule}]: {q.get('description', q)}")
    return violations


def gate(spark: SparkSession, layer: str, table: str):
    """Verifica `table` contra su contrato (mismo nombre) y aborta si no cumple."""
    violations = check_contract(spark, table, load_contract(table))
    if violations:
        raise BoundaryViolation(layer, [f"{table}: {v}" for v in violations])


# ==================================================================== métricas
def contaminated_in_fact(spark: SparkSession, fault: str) -> int:
    """Registros defectuosos que alcanzaron la tabla de hechos."""
    f = spark.table(FACT_TABLE)
    if fault == "tipo":
        return f.where("price IS NULL").count()
    if fault == "nulos":
        return f.where("customer_id IS NULL").count()
    if fault == "drift":
        return f.where("quantity IS NULL").count()
    if fault == "dominio":
        return f.where(f"quantity = {DOMAIN_SENTINEL}").count()
    if fault == "unicidad":
        return f.count() - f.select("invoice", "stock_code").distinct().count()
    if fault == "ri":
        g = spark.table("dim_geografia").select("country")
        return f.select("country").join(g, "country", "left_anti").count()
    return 0


def gold_metrics(spark: SparkSession) -> dict:
    """Lo que ve el consumidor: total publicado y volumen no atribuible."""
    g = spark.table("gold_ventas_region")
    agg = g.agg(F.sum("total_amount").alias("t"), F.sum("n_lines").alias("n"),
                F.count("*").alias("g")).first()
    null_lines = g.where("region IS NULL").agg(F.sum("n_lines")).first()[0]
    return {"gold_rows": int(agg["g"]),
            "gold_total_obs": None if agg["t"] is None else float(agg["t"]),
            "gold_lines": int(agg["n"] or 0),
            "gold_null_region_lines": int(null_lines or 0)}


def true_total(clean_df) -> float:
    """Total de ventas de referencia, calculado sobre el dato limpio con la
    misma fórmula que gold. Permite cuantificar cuánto se desvía el número
    publicado cuando el defecto no se detecta."""
    return float((clean_df["Quantity"] * clean_df["Price"]).round(2).sum())


def delta_write_metrics(spark: SparkSession) -> dict:
    """operationMetrics del último write de cada tabla (DESCRIBE HISTORY).

    Instrumento externo al código desarrollado. Para las operaciones de
    escritura el registro informa VOLUMEN (numOutputRows, numOutputBytes,
    numFiles) y no tiempo: `executionTimeMs` solo aparece en operaciones de
    modificación (MERGE, UPDATE, DELETE). Sirve entonces para auditar que las
    tres variantes materializan la misma cantidad de datos, de modo que una
    diferencia de costo no provenga de escribir volúmenes distintos. El tiempo
    se mide por reloj, aparte.
    """
    out = {"delta_output_rows": 0, "delta_output_bytes": 0, "delta_files": 0}
    for t in ALL_TABLES:
        if not spark.catalog.tableExists(t):
            continue  # la corrida abortó antes de crearla
        m = spark.sql(f"DESCRIBE HISTORY {t} LIMIT 1").first().operationMetrics or {}
        out["delta_output_rows"] += int(m.get("numOutputRows", 0))
        out["delta_output_bytes"] += int(m.get("numOutputBytes", 0))
        out["delta_files"] += int(m.get("numFiles", 0))
    return out


# ====================================================================== runner
FIELDNAMES = ["fault", "variant", "rep", "rows_source", "injected", "detected",
              "detect_layer", "violations", "contaminated_fact", "reached_consumo",
              "gold_rows", "gold_lines", "gold_null_region_lines",
              "gold_total_obs", "gold_total_true", "gold_dev_pct",
              "gate_ms", "constraint_ddl_ms", "wall_ms_total",
              "delta_output_rows", "delta_output_bytes", "delta_files"]


def run_once(clean, fault: str, variant: str, rep: int, seed: int,
             workdir: str) -> dict:
    corrupted, n_injected = inject(clean, fault, seed)
    csv_path = os.path.join(workdir, "source.csv")
    corrupted.to_csv(csv_path, index=False)

    spark = get_spark(os.path.join(workdir, "wh"))
    for t in ALL_TABLES:
        spark.sql(f"DROP TABLE IF EXISTS {t}")

    row = dict.fromkeys(FIELDNAMES)
    row.update(fault=fault, variant=variant, rep=rep, rows_source=len(corrupted),
               injected=n_injected, detected=0, detect_layer="ninguna",
               violations="", reached_consumo=0, constraint_ddl_ms=0.0,
               gold_total_true=round(true_total(clean), 2))

    gate_ms = 0.0  # tiempo de verificación acumulado, también si el gate aborta

    def timed_gate(layer, table):
        nonlocal gate_ms
        g0 = time.perf_counter()
        try:
            gate(spark, layer, table)
        finally:
            gate_ms += (time.perf_counter() - g0) * 1000

    t0 = time.perf_counter()
    try:
        build_bronze(spark, csv_path)
        if variant == "C":
            timed_gate("ingesta", "bronze_ventas")
        row["constraint_ddl_ms"] = build_silver(spark, variant)
        # Se mide antes del gate de la frontera intermedia, cuando la tabla de
        # hechos ya está materializada en las tres variantes y el volumen
        # contaminado es comparable entre ellas.
        row["contaminated_fact"] = contaminated_in_fact(spark, fault)
        if variant == "C":
            timed_gate("intermedia", FACT_TABLE)
        build_gold(spark)
        row.update(gold_metrics(spark))
        row["reached_consumo"] = 1
        if row["gold_total_obs"] is not None and row["gold_total_true"]:
            row["gold_dev_pct"] = round(
                100.0 * (row["gold_total_obs"] - row["gold_total_true"])
                / row["gold_total_true"], 3)
    except BoundaryViolation as e:
        row.update(detected=1, detect_layer=e.layer,
                   violations=" | ".join(e.details[:4])[:400])
    except Exception as e:  # violación de constraint del motor (variante B)
        msg = str(e)
        if any(k in msg for k in DELTA_CONSTRAINT_ERRORS):
            row.update(detected=1, detect_layer="intermedia",
                       violations=_first_line(msg))
        else:
            # Cualquier otro error se marca -1 y no cuenta como detección;
            # analyze.py aborta si encuentra alguno.
            row.update(detected=-1, violations=f"ERROR: {_first_line(msg)}")
    row["gate_ms"] = round(gate_ms, 1)
    row["wall_ms_total"] = round((time.perf_counter() - t0) * 1000, 1)

    row.update(delta_write_metrics(spark))
    spark.stop()
    return row


def _first_line(msg: str) -> str:
    return next((ln.strip() for ln in msg.splitlines() if ln.strip()), msg)[:220]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--rows", type=int, default=50_000)
    ap.add_argument("--source", default="data/online_retail_II.csv")
    ap.add_argument("--out", default="results/raw_runs.csv",
                    help="poblacion.json se escribe en el mismo directorio")
    ap.add_argument("--no-warmup", action="store_true",
                    help="no ejecutar la corrida de calentamiento descartada")
    args = ap.parse_args()

    outdir = os.path.dirname(args.out) or "."
    os.makedirs(outdir, exist_ok=True)

    print(f"[poblacion] leyendo {args.source} ...")
    pop, informe = prepare_population(args.source)
    informe["muestra_filas_objetivo"] = args.rows
    with open(os.path.join(outdir, "poblacion.json"), "w") as fh:
        json.dump(informe, fh, indent=2, ensure_ascii=False)
    for k, v in informe.items():
        print(f"  {k:32s} {v}")

    def one(fault, variant, rep):
        # Semilla fija por repetición: misma muestra y mismas filas corrompidas
        # para las tres variantes.
        seed = 1000 + rep
        wd = tempfile.mkdtemp(prefix="tfg_run_")
        try:
            return run_once(sample_population(pop, args.rows, seed),
                            fault, variant, rep, seed, wd)
        finally:
            shutil.rmtree(wd, ignore_errors=True)

    if not args.no_warmup:
        print("[calentamiento] corrida descartada (JVM, cache de página, JIT)")
        one("ninguna", "A", 1)

    matrix = [(f, v, r) for f in ["ninguna"] + FAULTS for v in "ABC"
              for r in range(1, args.reps + 1)]
    t_start = time.time()
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        w.writeheader()
        for i, (fault, variant, rep) in enumerate(matrix, 1):
            r = one(fault, variant, rep)
            w.writerow(r)
            fh.flush()
            print(f"[{i:3d}/{len(matrix)}] {fault:9s} {variant} rep{rep} "
                  f"detected={r['detected']:2d} layer={r['detect_layer']:10s} "
                  f"wall={r['wall_ms_total']:8.0f}ms")
    print(f"\nOK -> {args.out}  ({len(matrix)} corridas en "
          f"{(time.time() - t_start) / 60:.1f} min)")


if __name__ == "__main__":
    main()
