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
    make matriz     # 105 corridas y, al terminar, lo medido -> results/

La matriz completa tarda entre 10 y 20 minutos, según la máquina. Al terminar
deja en `results/`:

| Archivo | Qué tiene |
|---|---|
| `raw_runs.csv` | Una fila por corrida: qué se detectó, en qué capa, cuánto tardó, cuánto se contaminó |
| `poblacion.json` | Cuántas filas de la fuente cumplen el contrato y por qué se descarta el resto |
| `resumen.txt` | Lo medido: detección por falla y variante, volumen contaminado, costo y punto de equilibrio. También se muestra en la consola |
| `tablero.png` | Lo mismo, en un tablero de gráficos |

El repo trae `raw_runs.csv` y `poblacion.json` de la corrida que informa el
paper. Con esos dos archivos, `make analizar` genera `resumen.txt` y
`tablero.png` con los números del paper, sin correr la matriz:

    make analizar

`make matriz` los reemplaza por los de tu corrida.

Prueba rápida, sin valor de medición:

    make smoke      # una repetición con 4000 filas -> results/_smoke/

Con una sola repetición los tiempos no se interpretan: deja solo `resumen.txt`,
sin tablero.

## Estructura

| Ruta | Qué hay |
|---|---|
| `src/core.py` | Sesión Spark con Delta Lake, población experimental y dimensión geográfica |
| `src/injector.py` | Las seis clases de falla, con semilla fija |
| `src/pipeline.py` | El proceso de tres capas en las tres variantes |
| `src/runner.py` | El verificador de contratos y la ejecución de la matriz |
| `src/analyze.py` | De los resultados crudos al resumen y el tablero |
| `contracts/` | Contratos ODCS de la fuente y de la tabla de hechos |
| `results/` | Resultados crudos de la corrida que informa el paper |
| `Dockerfile`, `requirements.txt` | Entorno de medición con versiones fijadas |
