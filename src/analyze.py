"""analyze.py — de raw_runs.csv a lo que se midió.

Escribe en el directorio del CSV de entrada (results/ o results/_smoke/):
  resumen.txt   detección, capa, volumen contaminado, costo y punto de equilibrio
  tablero.png   lo mismo, en un tablero de gráficos
y muestra el resumen en la consola. No escribe en ningún otro lado.

Uso: python -m src.analyze [--in results/raw_runs.csv]
"""
from __future__ import annotations
import argparse, json, os, sys, warnings
import pandas as pd
import matplotlib
matplotlib.use("Agg")
# El desvío de la clase drift no es calculable (el total publicado es nulo):
# su mediana es vacía, numpy avisa y el resumen informa "no calculable".
warnings.filterwarnings("ignore", message="Mean of empty slice")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe_fx
from matplotlib.patches import Rectangle

from .injector import FAULTS as ORDER

# ---------------------------------------------------------------- presentación
ETIQUETA_FALLA = {
    "tipo": "Cambio de tipo", "nulos": "Nulos en campo obligatorio",
    "unicidad": "Clave de negocio duplicada", "drift": "Deriva de esquema",
    "dominio": "Valor fuera de dominio", "ri": "Integridad referencial",
}
DIMENSION = {
    "tipo": "Exactitud", "nulos": "Completitud", "unicidad": "Consistencia",
    "drift": "Consistencia", "dominio": "Exactitud", "ri": "Consistencia",
}
LAYERS = ["ingesta", "intermedia", "ninguna"]
ETIQUETA_CAPA = {"ingesta": "Ingesta", "intermedia": "Intermedia",
                 "ninguna": "No detectada"}
VARIANTE = {"A": "A — sin verificación", "B": "B — restricciones del motor",
            "C": "C — contratos ODCS"}

# Rampa ordinal de un solo tono (claro -> oscuro = detección más tardía) y
# paleta categórica de tres ranuras. Ambas validadas: separación bajo visión
# de color atípica y monotonía de luminosidad para impresión en escala de grises.
RAMPA_CAPA = {"ingesta": "#86b6ef", "intermedia": "#2a78d6", "ninguna": "#104281"}
SERIE = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}
TRAZO = {"A": (0, (1, 1.6)), "B": (0, (5, 2)), "C": "solid"}
TINTA, TENUE, GRILLA, FONDO = "#1a1a19", "#6b6a66", "#e6e5e1", "#fcfcfb"


def es(x, dec=0):
    """Número con separadores del español: miles con punto y decimales con coma."""
    if x is None or pd.isna(x):
        return "—"
    x = round(float(x), dec) + 0.0  # sin "−0": un cero redondeado no tiene signo
    entero, _, frac = f"{x:,.{dec}f}".partition(".")
    entero = entero.replace(",", ".").replace("-", "−")
    return entero + ("," + frac if frac else "")


# ===================================================================== medición
def equilibrio(m, v, r):
    """Amortización del costo de verificar sobre una ventana de corridas.

    Sobre N corridas de las que una trae defecto, cada camino cuesta:

        sin verificar     N*c_A + r          N corridas más un recálculo
        con verificación  (N-1)*c_V + a_V    N-1 verificadas más la que aborta

    con c_V el costo de una corrida limpia de la variante, a_V el de una que
    aborta por detección y r el del recálculo. Igualando y despejando, verificar
    sale más barato a partir de una frecuencia de defecto

        p*_V = (c_V - c_A) / [ d_V * (r + c_V - a_V) ]

    equivalente a un defecto cada 1/p* corridas. El factor d_V reparte el caso
    de la variante que no detecta todo: la fracción que se le escapa paga
    también el recálculo. Devuelve None si verificar nunca conviene.
    """
    den = m[f"d_{v}"] * (r + m[f"c_{v}"] - m[f"a_{v}"])
    return (m[f"c_{v}"] - m["c_A"]) / den if den > 0 else None


def costo(m, v, p):
    """Recta que une el costo de una corrida limpia (p = 0) con el de una
    corrida con defecto (p = 1)."""
    c, d, a, r = m[f"c_{v}"], m[f"d_{v}"], m[f"a_{v}"], m["r"]
    return (1 - p) * c + p * (d * a + (1 - d) * (c + r))


def medir(df):
    """Todo lo que se reporta, a partir de medianas del CSV. Tiempos en segundos."""
    runs, limpias = df[df.fault != "ninguna"], df[df.fault == "ninguna"]
    m = {"corridas": len(df), "clases": runs.fault.nunique(), "reps": df.rep.nunique(),
         "control": len(limpias),
         # tamaño de la muestra, sin las filas que agrega la falla
         "filas": limpias.rows_source.median()}
    for v in "ABC":
        m[f"c_{v}"] = limpias[limpias.variant == v].wall_ms_total.median() / 1000
        m[f"d_{v}"] = runs[runs.variant == v].detected.mean()
        det = runs[(runs.variant == v) & (runs.detected == 1)]
        m[f"a_{v}"] = det.wall_ms_total.median() / 1000 if len(det) else m[f"c_{v}"]
    m["r"] = runs[runs.variant == "A"].wall_ms_total.median() / 1000
    for v in ("B", "C"):
        m[f"pe_{v}"] = equilibrio(m, v, m["r"])
    # Sensibilidad de C a k, el costo del recálculo medido en corridas limpias
    # completas (r = k·c_A).
    for k in (5, 10):
        m[f"pe_C_k{k}"] = equilibrio(m, "C", k * m["c_A"])

    por_celda = runs.groupby(["fault", "variant"])
    m["deteccion"] = por_celda.detected.mean().unstack().reindex(ORDER)
    m["capa"] = por_celda.detect_layer.agg(lambda s: s.mode()[0]).unstack().reindex(ORDER)
    capas = (runs.groupby(["variant", "detect_layer"]).size().unstack(fill_value=0)
                 .reindex(columns=LAYERS, fill_value=0))
    m["capas"] = capas.div(capas.sum(1), axis=0) * 100

    a = runs[runs.variant == "A"]
    m["contam_A"] = a.contaminated_fact.median()
    m["consumo_A"] = a.groupby("fault")[["injected", "contaminated_fact", "gold_dev_pct",
                                         "gold_null_region_lines"]].median().reindex(ORDER)
    m["llega_A"] = a.reached_consumo.mean() * 100
    m["llega_C"] = runs[runs.variant == "C"].reached_consumo.mean() * 100

    m["gate_C"] = limpias[limpias.variant == "C"].gate_ms.median() / 1000
    m["ddl_B"] = limpias[limpias.variant == "B"].constraint_ddl_ms.median() / 1000
    # Volumen materializado, auditado con el registro de transacciones de Delta.
    m["delta_filas"] = limpias.delta_output_rows.median()
    m["delta_mb"] = limpias.delta_output_bytes.median() / 1e6
    return m


def tiempos_validos(m):
    """Con una sola repetición no hay mediana: los tiempos no se interpretan."""
    return m["reps"] > 1


# ====================================================================== resumen
def resumen(m, pob):
    pct = lambda x: f"{es(x)} %"
    L = [
        "POBLACIÓN",
        f"  {es(pob['filas_poblacion'])} de {es(pob['filas_crudas'])} filas cumplen el "
        f"contrato de la fuente (se descarta el {es(pob['pct_descartadas'], 2)} %)",
        f"  en el crudo: {es(pob['crudo_customer_id_nulo'])} filas sin cliente "
        f"({es(100 * pob['crudo_customer_id_nulo'] / pob['filas_crudas'], 1)} %), "
        f"{es(pob['crudo_clave_duplicada'])} claves duplicadas, "
        f"{es(pob['crudo_precio_fuera_dominio'])} precios y "
        f"{es(pob['crudo_cantidad_fuera_dominio'])} cantidades fuera de dominio",
        f"  {es(pob['facturas'])} facturas, {es(pob['crudo_paises_distintos'])} países",
        "",
        "MATRIZ",
        f"  {es(m['corridas'])} corridas: {m['clases']} fallas × 3 variantes × "
        f"{m['reps']} repeticiones + {m['control']} de control",
        f"  muestra de {es(m['filas'])} filas por corrida",
        "",
        "DETECCIÓN: corridas con falla que la detectan, y en qué capa",
        f"  {'Clase de falla':<28}{'Dimensión':<14}{'A':<17}{'B':<17}C",
    ]
    for f in ORDER:
        celdas = []
        for v in "ABC":
            d = m["deteccion"].loc[f, v]
            celdas.append(pct(d * 100) + (f" {m['capa'].loc[f, v]}" if d else ""))
        L.append(f"  {ETIQUETA_FALLA[f]:<28}{DIMENSION[f]:<14}"
                 f"{celdas[0]:<17}{celdas[1]:<17}{celdas[2]}")
    L.append(f"  {'Total':<42}" + "".join(f"{pct(m[f'd_{v}'] * 100):<17}" for v in "AB")
             + pct(m["d_C"] * 100))
    for v in "ABC":
        L.append(f"  capa de detección {v}: " + ", ".join(
            f"{c} {pct(m['capas'].loc[v, c])}" for c in LAYERS))

    L += ["", "QUÉ LLEGA AL CONSUMO SIN VERIFICAR (variante A, medianas)",
          f"  {'Clase de falla':<28}{'Inyectados':>12}{'En hechos':>12}{'Desvío del total':>20}"]
    for f in ORDER:
        s = m["consumo_A"].loc[f]
        dev = "no calculable" if pd.isna(s.gold_dev_pct) else f"{es(s.gold_dev_pct, 2)} %"
        L.append(f"  {ETIQUETA_FALLA[f]:<28}{es(s.injected):>12}"
                 f"{es(s.contaminated_fact):>12}{dev:>20}")
    L += [f"  registros defectuosos en hechos (A): {es(m['contam_A'])}",
          f"  líneas sin región en gold (ri): {es(m['consumo_A'].loc['ri'].gold_null_region_lines)}",
          f"  llegan al consumo: A {pct(m['llega_A'])}, C {pct(m['llega_C'])}",
          f"  volumen escrito por corrida de control (DESCRIBE HISTORY): "
          f"{es(m['delta_filas'])} filas, {es(m['delta_mb'], 2)} MB",
          "", "COSTO (segundos, medianas)"]
    if not tiempos_validos(m):
        L.append("  una sola repetición: los tiempos no se interpretan (make matriz)")
        return "\n".join(L) + "\n"

    def ventana(pe):
        return "no alcanzado" if pe is None else (
            f"{es(pe * 100, 1)} % (un defecto cada {es(1 / pe, 1)} corridas)")

    sobre = {v: (m[f"c_{v}"] - m["c_A"]) / m["c_A"] * 100 for v in "BC"}
    q_B = costo(m, "B", 1)
    L += [f"  corrida sin defecto: A {es(m['c_A'], 2)}, B {es(m['c_B'], 2)} "
          f"(+{es(sobre['B'], 1)} %), C {es(m['c_C'], 2)} (+{es(sobre['C'], 1)} %, "
          f"+{es(m['c_C'] - m['c_A'], 2)} s)",
          f"  verificación de C: {es(m['gate_C'], 2)}",
          f"  restricciones de B al crear la tabla: {es(m['ddl_B'], 2)}",
          f"  corrida que aborta: B {es(m['a_B'], 2)}, C {es(m['a_C'], 2)}",
          f"  recálculo (corrida con defecto que llega al consumo): {es(m['r'], 2)}",
          "  costo por corrida, con p la proporción de corridas con defecto:",
          f"    A = {es(m['c_A'], 2)} + {es(m['r'], 2)} × p",
          f"    B = {es(m['c_B'], 2)} × (1 − p) + {es(q_B, 2)} × p",
          f"    C = {es(m['c_C'], 2)} × (1 − p) + {es(m['a_C'], 2)} × p",
          f"  equilibrio frente a A: C desde {ventana(m['pe_C'])}",
          f"                         B desde {ventana(m['pe_B'])}",
          "  si el recálculo cuesta k corridas limpias, C conviene desde un defecto cada: "
          + ", ".join(f"{es(1 / m[f'pe_C_k{k}'], 1) if m[f'pe_C_k{k}'] else '—'} (k = {k})"
                      for k in (5, 10))]
    return "\n".join(L) + "\n"


# ====================================================================== tablero
def _ejes(ax, grilla):
    ax.grid(axis=grilla, color=GRILLA, linewidth=0.6, zorder=0)
    ax.tick_params(length=0, colors=TENUE, labelsize=10)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("bottom", "left"):
        ax.spines[lado].set_color(GRILLA)


def _matriz(ax, m):
    n = len(ORDER)
    for i, f in enumerate(ORDER):
        for j, v in enumerate("ABC"):
            d = m["deteccion"].loc[f, v]
            c = m["capa"].loc[f, v] if d else "ninguna"
            ax.add_patch(Rectangle((j + 0.03, i + 0.06), 0.94, 0.88, facecolor=RAMPA_CAPA[c],
                                   edgecolor="none"))
            # si detecta solo en algunas repeticiones, va el porcentaje
            rotulo = ETIQUETA_CAPA[c] + ("" if d in (0, 1) else f" ({es(d * 100)} %)")
            ax.text(j + 0.5, i + 0.5, rotulo, ha="center", va="center", fontsize=10.5,
                    color=TINTA if c == "ingesta" else "white")
    ax.plot([0, 3], [n + 0.15, n + 0.15], color=TENUE, linewidth=0.8, clip_on=False)
    ax.text(-0.06, n + 0.6, "Total detectado", ha="right", va="center", fontsize=10.5,
            color=TINTA)
    for j, v in enumerate("ABC"):
        ax.text(j + 0.5, n + 0.6, f"{es(m[f'd_{v}'] * 100)} %", ha="center", va="center",
                fontsize=12, fontweight="bold", color=TINTA)
    ax.set_xlim(0, 3); ax.set_ylim(n + 1, 0)
    ax.set_xticks([0.5, 1.5, 2.5])
    ax.set_xticklabels(["A\nsin verificación", "B\nrestricciones del motor",
                        "C\ncontratos ODCS"], fontsize=10.5)
    ax.xaxis.tick_top()
    ax.set_yticks([i + 0.5 for i in range(n)])
    ax.set_yticklabels([ETIQUETA_FALLA[f] for f in ORDER], fontsize=10.5)
    ax.tick_params(length=0, colors=TINTA)
    for lado in ax.spines.values():
        lado.set_visible(False)
    ax.set_title("En qué capa se detecta cada falla", loc="left", fontsize=12.5,
                 color=TINTA, pad=46)


def _barras(ax, m):
    vals = [m[f"c_{v}"] for v in "ABC"]
    ax.barh(range(3), vals, height=0.5, color=[SERIE[v] for v in "ABC"], zorder=3)
    for i, v in enumerate("ABC"):
        extra = "" if v == "A" else f"  (+{es((m[f'c_{v}'] / m['c_A'] - 1) * 100, 1)} %)"
        ax.text(vals[i] + max(vals) * 0.015, i, f"{es(vals[i], 2)} s{extra}", va="center",
                fontsize=10.5, color=TINTA)
    ax.set_yticks(range(3)); ax.set_yticklabels([VARIANTE[v] for v in "ABC"], fontsize=10.5)
    ax.invert_yaxis()
    ax.set_xlim(0, max(vals) * 1.45)
    ax.set_xticks(range(0, int(max(vals) * 1.45) + 1, 5))
    ax.set_xlabel("segundos, mediana", fontsize=10, color=TENUE)
    _ejes(ax, "x")
    ax.spines["left"].set_visible(False)
    ax.set_title("Costo de una corrida sin defecto", loc="left", fontsize=12.5, color=TINTA)


def _rectas(ax, m):
    cruces = [m[f"pe_{v}"] for v in ("B", "C") if m[f"pe_{v}"]]
    tope = min(1.0, max(cruces) * 1.35) if cruces else 0.25
    ps = [i * tope / 200 for i in range(201)]
    for v in "ABC":
        ax.plot([p * 100 for p in ps], [costo(m, v, p) for p in ps], color=SERIE[v],
                linewidth=2, linestyle=TRAZO[v], label=VARIANTE[v], zorder=3)
    for v in ("B", "C"):
        pe = m[f"pe_{v}"]
        if pe is None or not 0 < pe <= tope:
            continue
        yv = costo(m, v, pe)
        ax.plot([pe * 100], [yv], "o", ms=8, color=SERIE[v], markeredgecolor=FONDO,
                markeredgewidth=2, zorder=5)
        ax.annotate(f"{es(pe * 100, 1)} %", (pe * 100, yv), textcoords="offset points",
                    xytext=(8, -12), va="top", fontsize=10, color=TINTA, zorder=6,
                    # halo: el rótulo nunca queda cruzado por una recta
                    path_effects=[pe_fx.withStroke(linewidth=3, foreground=FONDO)])
    extremos = [costo(m, v, p) for v in "ABC" for p in (0, tope)]
    ax.set_xlim(0, tope * 100)
    # lugar arriba para la leyenda
    ax.set_ylim(min(extremos) - 0.6, max(extremos) + 2.4)
    ax.set_xlabel("corridas con defecto [%]", fontsize=10, color=TENUE)
    ax.set_ylabel("costo por corrida [s]", fontsize=10, color=TENUE)
    _ejes(ax, "y")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: es(y)))
    ax.legend(loc="upper right", frameon=False, fontsize=9.5, handlelength=2.6)
    ax.set_title("Costo por corrida según cuántas traen un defecto", loc="left",
                 fontsize=12.5, color=TINTA)

    def desde(v):
        pe = m[f"pe_{v}"]
        return "no conviene nunca" if pe is None else (
            f"desde el {es(pe * 100, 1)} % (un defecto cada {es(1 / pe, 1)} corridas)")
    ax.text(-0.14, -0.24, f"Con contratos, verificar conviene {desde('C')}.\n"
            f"Con las restricciones del motor, {desde('B')}.", transform=ax.transAxes,
            fontsize=9.5, color=TINTA, va="top", linespacing=1.6)


def tablero(m, pob, salida):
    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": TINTA,
                         "axes.labelcolor": TENUE, "figure.facecolor": FONDO,
                         "axes.facecolor": FONDO, "savefig.facecolor": FONDO})
    fig = plt.figure(figsize=(15, 9), dpi=130)
    fig.text(0.03, 0.94, "Detección y costo por variante",
             fontsize=18, fontweight="bold", color=TINTA)
    fig.text(0.03, 0.875,
             f"{es(m['corridas'])} corridas sobre Online Retail II, {m['reps']} repeticiones, "
             f"muestra de {es(m['filas'])} filas por corrida.\n"
             f"De la fuente, {es(pob['filas_poblacion'])} de {es(pob['filas_crudas'])} filas "
             f"cumplen el contrato (se descarta el {es(pob['pct_descartadas'], 2)} %).",
             fontsize=11, color=TENUE, linespacing=1.5)

    g = fig.add_gridspec(2, 2, width_ratios=[1.05, 1.0], height_ratios=[0.8, 1.2],
                         left=0.15, right=0.97, top=0.74, bottom=0.19,
                         hspace=0.5, wspace=0.42)
    _matriz(fig.add_subplot(g[:, 0]), m)
    _barras(fig.add_subplot(g[0, 1]), m)
    _rectas(fig.add_subplot(g[1, 1]), m)
    fig.savefig(salida)
    plt.close(fig)


# ========================================================================= main
def validar(df):
    """Si el CSV no cumple estas condiciones, lo medido no significa lo que dice
    el paper: no se genera nada."""
    # Un fallo de infraestructura no puede contarse como detección.
    errores = df[df.detected == -1]
    if len(errores):
        muestra = [str(v)[:180] for v in errores.violations.dropna().unique()[:5]]
        sys.exit(f"ABORTA: {len(errores)} corridas terminaron en error.\n   "
                 + "\n   ".join(muestra))
    # Si en las corridas de control las variantes escriben volúmenes distintos,
    # la diferencia de costo no es atribuible a la verificación.
    vol = (df[df.fault == "ninguna"].groupby("rep")
           [["delta_output_rows", "delta_output_bytes"]].nunique())
    if (vol > 1).any().any():
        sys.exit("ABORTA: las variantes materializan volúmenes distintos "
                 "en las corridas de control.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="results/raw_runs.csv",
                    help="poblacion.json se lee del mismo directorio y la salida va ahí")
    a = ap.parse_args()

    df = pd.read_csv(a.inp)
    validar(df)
    outdir = os.path.dirname(a.inp) or "."
    with open(os.path.join(outdir, "poblacion.json")) as fh:
        pob = json.load(fh)

    m = medir(df)
    texto = resumen(m, pob)
    with open(os.path.join(outdir, "resumen.txt"), "w") as fh:
        fh.write(texto)
    print(texto)
    # Con una sola repetición los tiempos no se comparan: sin tablero.
    if tiempos_validos(m):
        tablero(m, pob, os.path.join(outdir, "tablero.png"))
        print(f"OK -> {outdir}/resumen.txt y {outdir}/tablero.png")
    else:
        print(f"OK -> {outdir}/resumen.txt")


if __name__ == "__main__":
    main()
