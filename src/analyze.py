"""analyze.py — de raw_runs.csv a los artefactos del paper.

Genera en --outdir (por defecto latex/generado/, de donde los toma el paper):
  tabla1_deteccion.tex     tabla de tasa de detección por clase y variante
  tabla2_contaminacion.tex tabla de volumen defectuoso y desvío publicado
  fig1_capas.pdf/.png      figura de capa de detección por variante
  fig2_equilibrio.pdf/.png figura del punto de equilibrio
  metricas.tex             macros LaTeX con todos los números citados en el texto

Uso: python -m src.analyze [--in results/raw_runs.csv] [--outdir latex/generado]
"""
from __future__ import annotations
import argparse, json, os, sys, warnings
import pandas as pd
import matplotlib
matplotlib.use("Agg")
# El desvío de la clase drift no es calculable (el total publicado es nulo):
# su mediana es vacía, numpy avisa y la tabla informa "no calculable".
warnings.filterwarnings("ignore", message="Mean of empty slice")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe_fx

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
TINTA, TINTA_TENUE = "#1a1a19", "#6b6a66"


def estilo():
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Nimbus Roman", "DejaVu Serif"],
        "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8,
        "xtick.labelsize": 7.5, "ytick.labelsize": 7.5, "legend.fontsize": 7,
        "axes.edgecolor": TINTA_TENUE, "axes.linewidth": 0.6,
        "xtick.color": TINTA_TENUE, "ytick.color": TINTA_TENUE,
        "text.color": TINTA, "axes.labelcolor": TINTA,
        "figure.dpi": 300, "savefig.dpi": 300, "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
    })


# ------------------------------------------------------------------- utilidades
def es(x, dec=0):
    """Número con separadores del español: miles con punto, decimales con coma
    y signo menos tipográfico ($-$, no guion)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "---"
    entero, _, frac = f"{float(x):,.{dec}f}".partition(".")
    entero = entero.replace(",", ".").replace("-", "$-$")
    return entero + ("," + frac if frac else "")


def escribir(outdir, nombre, texto):
    with open(os.path.join(outdir, nombre), "w") as fh:
        fh.write(texto)


def guardar_fig(fig, outdir, nombre):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(outdir, f"{nombre}.{ext}"))
    plt.close(fig)


# ================================================================ tablas I y II
def tabla_tex(caption, label, columnas, encabezado, filas):
    cuerpo = "\n".join(filas)
    return f"""\\begin{{table}}[t]
\\caption{{{caption}}}
\\label{{{label}}}
\\centering
\\footnotesize
\\begin{{tabular}}{{@{{}}{columnas}@{{}}}}
\\toprule
{encabezado} \\\\
\\midrule
{cuerpo}
\\bottomrule
\\end{{tabular}}
\\end{{table}}
"""


def tabla1(runs, outdir):
    det = (runs.groupby(["fault", "variant"]).detected.mean() * 100).unstack()
    det = det.reindex(ORDER)
    filas = []
    for f in ORDER:
        celdas = " & ".join(f"{det.loc[f, v]:.0f}\\,\\%" for v in "ABC")
        filas.append(f"{ETIQUETA_FALLA[f]} & {DIMENSION[f]} & {celdas} \\\\")
    escribir(outdir, "tabla1_deteccion.tex", tabla_tex(
        "Tasa de detección por clase de falla y variante", "tab:deteccion",
        "llccc", "Clase de falla & Dimensión & A & B & C", filas))
    return det


def tabla2(runs, outdir):
    """Qué le llega al consumo cuando nadie verifica (variante A)."""
    a = runs[runs.variant == "A"]
    filas = []
    for f in ORDER:
        s = a[a.fault == f]
        dev = s.gold_dev_pct.median()
        dev_txt = "no calculable" if pd.isna(dev) else f"{es(dev, 2)}\\,\\%"
        filas.append(f"{ETIQUETA_FALLA[f]} & {es(s.injected.median())} & "
                     f"{es(s.contaminated_fact.median())} & {dev_txt} \\\\")
    escribir(outdir, "tabla2_contaminacion.tex", tabla_tex(
        "Volumen defectuoso y desvío del total publicado en la variante sin\n"
        "verificación (medianas)", "tab:contaminacion",
        "lrrr", "Clase de falla & Inyectados & En hechos & Desvío del total", filas))


# ====================================================================== figuras
def figura1(runs, outdir):
    piv = (runs.groupby(["variant", "detect_layer"]).size().unstack(fill_value=0)
              .reindex(columns=LAYERS, fill_value=0))
    piv = piv.div(piv.sum(1), axis=0) * 100

    fig, ax = plt.subplots(figsize=(3.45, 1.95))
    izq = pd.Series(0.0, index=piv.index)
    y = list(range(len(piv.index)))
    for capa in LAYERS:
        val = piv[capa]
        ax.barh(y, val, left=izq, height=0.62, color=RAMPA_CAPA[capa],
                edgecolor="white", linewidth=1.2, zorder=3)
        for i, (v, l) in enumerate(zip(val, izq)):
            if v >= 9:  # rótulo directo: la identidad no queda solo en el color
                ax.text(l + v / 2, i, f"{v:.0f}\u2009%", ha="center", va="center",
                        color=TINTA if capa == "ingesta" else "white",
                        fontsize=7, zorder=4)
        izq = izq + val

    ax.set_yticks(y); ax.set_yticklabels(list(piv.index))
    ax.invert_yaxis()
    ax.set_xlim(0, 100); ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("corridas con falla inyectada [%]")
    ax.set_ylabel("variante")
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.legend([plt.Rectangle((0, 0), 1, 1, color=RAMPA_CAPA[c]) for c in LAYERS],
              [ETIQUETA_CAPA[c] for c in LAYERS], loc="upper center",
              bbox_to_anchor=(0.5, 1.32), ncol=3, frameon=False,
              handlelength=1.1, handleheight=0.9, columnspacing=1.2)
    guardar_fig(fig, outdir, "fig1_capas")
    return piv


def _curva(m, v, p):
    c, d, a, r = m[f"c_{v}"], m[f"d_{v}"], m[f"a_{v}"], m["r"]
    return [(1 - x) * c + x * (d * a + (1 - d) * (c + r)) for x in p]


def figura2(m, outdir):
    # El eje llega hasta donde haga falta para mostrar los cruces.
    cruces = [m[f"pe_{v}"] for v in ("B", "C") if m.get(f"pe_{v}")]
    tope = min(100, max(25, round(max(cruces) * 100 * 1.35))) if cruces else 25
    n = 400
    p = [i * tope / (100 * n) for i in range(n + 1)]
    fig, ax = plt.subplots(figsize=(3.45, 2.45))
    for v in ("A", "B", "C"):
        ax.plot([x * 100 for x in p], [y / 1000 for y in _curva(m, v, p)],
                color=SERIE[v], linewidth=1.6, linestyle=TRAZO[v],
                solid_capstyle="round", label=VARIANTE[v], zorder=3)

    for v in ("B", "C"):
        pe = m.get(f"pe_{v}")
        if pe is not None and 0 < pe * 100 <= tope:
            yv = _curva(m, v, [pe])[0] / 1000
            ax.plot([pe * 100], [yv], "o", ms=4.5, color=SERIE[v],
                    markeredgecolor="white", markeredgewidth=1.1, zorder=5)
            ax.annotate(f"{es(pe * 100, 1)}\u2009%", (pe * 100, yv),
                        textcoords="offset points", xytext=(6, -15),
                        fontsize=7, color=SERIE[v], zorder=6,
                        # halo: el rótulo nunca queda cruzado por una curva
                        path_effects=[pe_fx.withStroke(linewidth=2.4,
                                                       foreground="white")])

    ax.set_xlabel("corridas con defecto [%]")
    ax.set_ylabel("costo por corrida [s]")
    ax.set_xlim(0, tope)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="#e6e5e1", linewidth=0.5, zorder=0)
    ax.set_axisbelow(True)
    # Leyenda fuera del área de trazado: dentro pisa las curvas.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.21), ncol=1,
              frameon=False, handlelength=2.4, borderpad=0.1,
              labelspacing=0.25, alignment="left")
    guardar_fig(fig, outdir, "fig2_equilibrio")


# ===================================================================== métricas
def metricas(df, runs, limpias, det, piv, pob, outdir):
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
    también el recálculo. Todas las cantidades son medianas medidas; la única
    que no se mide es la frecuencia de defecto, y por eso el resultado es una
    curva y no un valor.
    """
    m = {}
    for v in "ABC":
        m[f"c_{v}"] = float(limpias[limpias.variant == v].wall_ms_total.median())
        m[f"d_{v}"] = float(runs[runs.variant == v].detected.mean())
        det_v = runs[(runs.variant == v) & (runs.detected == 1)]
        m[f"a_{v}"] = float(det_v.wall_ms_total.median()) if len(det_v) else m[f"c_{v}"]
    m["r"] = float(runs[runs.variant == "A"].wall_ms_total.median())

    def equilibrio(v, r):
        """p*_V para un costo de recálculo r; None si verificar nunca conviene."""
        den = m[f"d_{v}"] * (r + m[f"c_{v}"] - m[f"a_{v}"])
        return (m[f"c_{v}"] - m["c_A"]) / den if den > 0 else None

    for v in ("B", "C"):
        m[f"pe_{v}"] = equilibrio(v, m["r"])

    L = []
    def mac(nombre, valor):
        L.append(f"\\newcommand{{\\{nombre}}}{{{valor}}}")

    # --- matriz
    mac("Ncorridas", es(len(df)))
    mac("Nclases", es(runs.fault.nunique()))
    mac("Nreps", es(df.rep.nunique()))
    mac("Ncontrol", es(len(limpias)))
    mac("Nfilas", es(limpias.rows_source.median()))  # tamaño de la muestra, sin las filas que agrega la falla

    # --- población: caracterización de la fuente real
    mac("PobCrudas", es(pob["filas_crudas"]))
    mac("PobFilas", es(pob["filas_poblacion"]))
    mac("PobPctDescartadas", es(pob["pct_descartadas"], 2))
    mac("PobNulos", es(pob["crudo_customer_id_nulo"]))
    mac("PobPctNulos", es(100 * pob["crudo_customer_id_nulo"] / pob["filas_crudas"], 1))
    mac("PobDuplicados", es(pob["crudo_clave_duplicada"]))
    mac("PobPrecioFuera", es(pob["crudo_precio_fuera_dominio"]))
    mac("PobCantidadFuera", es(pob["crudo_cantidad_fuera_dominio"]))
    mac("PobPaises", es(pob["crudo_paises_distintos"]))
    mac("PobFacturas", es(pob["facturas"]))

    # --- detección y costo
    for v in "ABC":
        mac(f"Det{v}", es(m[f"d_{v}"] * 100))
        mac(f"Costo{v}", es(m[f"c_{v}"] / 1000, 2))
    mac("SobrecostoB", es((m["c_B"] - m["c_A"]) / m["c_A"] * 100, 1))
    mac("SobrecostoC", es((m["c_C"] - m["c_A"]) / m["c_A"] * 100, 1))
    mac("SobrecostoCseg", es((m["c_C"] - m["c_A"]) / 1000, 2))
    mac("CostoReproceso", es(m["r"] / 1000, 2))
    for v in ("B", "C"):
        mac(f"Abortado{v}", es(m[f"a_{v}"] / 1000, 2))
        pe = m[f"pe_{v}"]
        mac(f"Equilibrio{v}", "no alcanzado" if pe is None else es(pe * 100, 1))
        # el mismo punto expresado como "un defecto cada N corridas"
        mac(f"Ventana{v}", "---" if not pe else es(1 / pe, 1))

    # Sensibilidad de C a k, el costo de reprocesar medido en corridas limpias
    # completas (r = k·c_A). Los nombres de macro de LaTeX no admiten dígitos.
    for k, sufijo in ((5, "Cinco"), (10, "Diez")):
        pk = equilibrio("C", k * m["c_A"])
        mac(f"VentanaCK{sufijo}", "---" if not pk else es(1 / pk, 1))

    # --- clases que las restricciones del motor no cubren
    ciegas = [ETIQUETA_FALLA[f].lower() for f in ORDER if det.loc[f, "B"] == 0]
    mac("CiegasB", " y ".join(ciegas) if ciegas else "ninguna")

    # --- capa de detección
    for v in "ABC":
        for capa in LAYERS:
            mac(f"Capa{v}{capa.capitalize()}", es(piv.loc[v, capa]))

    # --- contaminación y desvío del total publicado (variante A)
    a = runs[runs.variant == "A"]
    mac("ContamA", es(a.contaminated_fact.median()))
    for f in ORDER:
        s = a[a.fault == f]
        mac(f"Contam{f.capitalize()}", es(s.contaminated_fact.median()))
        d = s.gold_dev_pct.median()
        mac(f"Desvio{f.capitalize()}", "no calculable" if pd.isna(d) else es(d, 2))
    sri = a[a.fault == "ri"]
    mac("SinRegionRi", es(sri.gold_null_region_lines.median()))
    mac("ConsumoA", es(100 * a.reached_consumo.mean()))
    mac("ConsumoC", es(100 * runs[runs.variant == "C"].reached_consumo.mean()))

    # --- verificación y escritura
    mac("GateC", es(limpias[limpias.variant == "C"].gate_ms.median() / 1000, 2))
    mac("DdlB", es(limpias[limpias.variant == "B"].constraint_ddl_ms.median() / 1000, 2))
    # Volumen materializado, auditado con el registro de transacciones de Delta.
    mac("DeltaFilas", es(limpias.delta_output_rows.median()))
    mac("DeltaMB", es(limpias.delta_output_bytes.median() / 1e6, 2))

    escribir(outdir, "metricas.tex",
             "% Generado por src/analyze.py sobre results/raw_runs.csv. NO editar a mano.\n"
             + "\n".join(L) + "\n")
    return m


# ========================================================================= main
def validar(df):
    """Si el CSV no cumple estas condiciones, los artefactos no significan lo
    que dice el paper: no se genera nada."""
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
                    help="poblacion.json se lee del mismo directorio")
    ap.add_argument("--outdir", default="latex/generado")
    a = ap.parse_args()

    df = pd.read_csv(a.inp)
    validar(df)
    runs = df[df.fault != "ninguna"]
    limpias = df[df.fault == "ninguna"]
    with open(os.path.join(os.path.dirname(a.inp) or ".", "poblacion.json")) as fh:
        pob = json.load(fh)

    os.makedirs(a.outdir, exist_ok=True)
    estilo()
    det = tabla1(runs, a.outdir)
    tabla2(runs, a.outdir)
    piv = figura1(runs, a.outdir)
    m = metricas(df, runs, limpias, det, piv, pob, a.outdir)
    figura2(m, a.outdir)

    print(f"OK -> {a.outdir}")
    print(f"   detección       A={m['d_A']*100:.0f}%  B={m['d_B']*100:.0f}%  C={m['d_C']*100:.0f}%")
    print(f"   costo limpio[s] A={m['c_A']/1000:.2f}  B={m['c_B']/1000:.2f}  C={m['c_C']/1000:.2f}")
    for v in ("B", "C"):
        pe = m[f"pe_{v}"]
        print(f"   equilibrio {v} vs A: " +
              ("no alcanzado" if pe is None else f"{pe*100:.2f}% de corridas con defecto"))


if __name__ == "__main__":
    main()
