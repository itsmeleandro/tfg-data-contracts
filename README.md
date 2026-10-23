# Contratos de datos como mecanismo de detección temprana de errores

Trabajo Final de Grado — Licenciatura en Informática, Universidad de Palermo.

Prueba de concepto que compara tres formas de verificar datos en un proceso de
tres capas (ingesta, intermedia y consumo) sobre el conjunto de datos público
Online Retail II (UCI, CC BY 4.0):

- **A — sin verificación**: el defecto avanza en silencio hasta el consumo.
- **B — restricciones del motor**: `NOT NULL` y `CHECK` de Delta Lake sobre la
  tabla de hechos.
- **C — contratos de datos**: contratos ODCS verificados en las dos fronteras
  entre capas.

Se inyectan seis clases de falla (cambio de tipo, nulos, clave duplicada,
deriva de esquema, valor fuera de dominio e integridad referencial) y se mide
qué detecta cada variante, en qué capa y a qué costo.

El paper está en [`latex/paper_rojas_borrador.pdf`](latex/paper_rojas_borrador.pdf).

## Requisitos

- Docker
- `make`, `curl` y `unzip`
- Python 3 con `pandas` y `openpyxl`, solo para descargar el dataset

## Cómo se corre

    make dataset    # baja Online Retail II de UCI y arma data/online_retail_II.csv
    make build      # construye la imagen con versiones fijadas
    make matriz     # 105 corridas -> results/raw_runs.csv y results/poblacion.json
    make analizar   # tablas, figuras y métricas -> latex/generado/

La matriz completa tarda entre 10 y 20 minutos, según la máquina.

Prueba rápida, sin valor de medición:

    make smoke      # una repetición con 4000 filas -> results/_smoke/

## Estructura

| Ruta | Qué hay |
|---|---|
| `src/core.py` | Sesión Spark con Delta Lake, población experimental y dimensión geográfica |
| `src/injector.py` | Las seis clases de falla, con semilla fija |
| `src/pipeline.py` | El proceso de tres capas en las tres variantes |
| `src/runner.py` | El verificador de contratos y la ejecución de la matriz |
| `src/analyze.py` | De los resultados crudos a tablas, figuras y métricas |
| `contracts/` | Contratos ODCS de la fuente y de la tabla de hechos |
| `Dockerfile`, `requirements.txt` | Entorno de medición con versiones fijadas |
