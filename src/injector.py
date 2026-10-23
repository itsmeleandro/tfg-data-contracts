"""injector.py — generador controlado de fallas.

Seis clases asignadas a características de ISO/IEC 25012 (tipo, nulos y
duplicados figuran además entre las causas raíz de Foidl et al. 2024):
  tipo       : Price numérico llega como texto con coma decimal        (exactitud)
  nulos      : Customer ID ausente en campos obligatorios              (completitud)
  unicidad   : líneas duplicadas de la clave de negocio                (consistencia)
  drift      : columna Quantity renombrada en el origen                (consistencia)
  dominio    : Quantity fuera del rango admitido                       (exactitud)
  ri         : Country inexistente en la dimensión geográfica          (consistencia)

Determinismo: misma semilla, mismas filas corrompidas. Una clase por corrida.
Los contratos de contracts/ se especificaron a partir del diccionario de datos
del dataset y antes que este módulo. No se ajustan mirando qué detecta cada
variante.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

FAULTS = ["tipo", "nulos", "unicidad", "drift", "dominio", "ri"]
RATE = 0.03                  # fracción de filas afectadas
DOMAIN_SENTINEL = 999_999    # Quantity fuera de dominio
UNKNOWN_COUNTRIES = ["Atlantis", "Wakanda", "Zubrowka"]


def inject(df: pd.DataFrame, fault: str, seed: int) -> tuple[pd.DataFrame, int]:
    """Devuelve (df corrompido, cantidad de filas afectadas). La corrida de
    control usa fault='ninguna'."""
    if fault == "ninguna":
        return df.copy(), 0
    rng = np.random.default_rng(seed * 7919 + FAULTS.index(fault))
    out = df.copy()
    n = len(out)
    k = max(1, int(n * RATE))
    idx = rng.choice(n, size=k, replace=False)

    if fault == "tipo":
        # coma decimal -> el cast estricto a double produce null
        out["Price"] = out["Price"].astype("object")
        out.loc[out.index[idx], "Price"] = [
            f"{p:.2f}".replace(".", ",") for p in df["Price"].iloc[idx]]
        return out, k

    if fault == "nulos":
        out.loc[out.index[idx], "Customer ID"] = np.nan
        return out, k

    if fault == "unicidad":
        dupes = out.iloc[idx].copy()
        return pd.concat([out, dupes], ignore_index=True), k

    if fault == "drift":
        # deriva de esquema: el origen renombra la columna
        return out.rename(columns={"Quantity": "Qty"}), n

    if fault == "dominio":
        out.loc[out.index[idx], "Quantity"] = DOMAIN_SENTINEL
        return out, k

    # ri (FAULTS.index ya rechazó cualquier otra clase)
    out.loc[out.index[idx], "Country"] = rng.choice(UNKNOWN_COUNTRIES, size=k)
    return out, k
