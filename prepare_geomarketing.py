"""Préparation des données du projet « Géomarketing : où ouvrir un commerce à Paris ? »

À lancer une seule fois, en local (pip install pandas shapely pyproj openpyxl).
Entrées (dossier sources/) :
  - Local_commercial_par_taille_(6_postes).csv : BDCOM 2023, Apur (commerces de Paris, coordonnées Lambert 93)
  - BDCOM_2023_OD.xlsx : nomenclature des activités BDCOM 2023, Apur
  - IRIS.geojson : contours des IRIS, Apur
  - base-ic-evol-struct-pop-2021.CSV : population par IRIS, recensement 2021, Insee
  - BASE_TD_FILO_IRIS_2021_DISP.csv : revenus disponibles par IRIS, Filosofi 2021, Insee
  - validations-reseau-ferre-nombre-validations-par-jour-4eme-trimestre.csv : validations par jour et par arrêt, T4 2025, IDFM
  - emplacement-des-gares-idf.geojson : emplacement des gares et stations, IDFM
Sorties (dossier data/) :
  - geo_iris.csv : une ligne par IRIS de Paris (habitants, revenu, flux, concurrents par type de commerce)
  - geo_iris.geojson : contours simplifiés des IRIS de Paris (pour la carte)
  - geo_gares.csv : gares de Paris et voyageurs par jour ouvré
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import mapping, shape
import shapely

SRC = Path("sources")
OUT = Path("data")
OUT.mkdir(exist_ok=True)
RAYON = 400  # mètres : la zone de chalandise à pied autour du centre de chaque IRIS

# Types de commerce proposés, avec leurs codes d'activité BDCOM 2023
TYPES = {
    "boulangerie": ("Boulangerie-pâtisserie", ["CA103", "CA104"]),
    "cafe": ("Café, bar ou salon de thé", ["CH401", "CH402", "CH403"]),
    "sport": ("Salle de sport", ["SA405", "SA406"]),
    "pharmacie": ("Pharmacie", ["CC101"]),
    "librairie": ("Librairie", ["CE101"]),
    "fleuriste": ("Fleuriste", ["CF201"]),
    "fromagerie": ("Fromagerie", ["CA112"]),
    "laverie": ("Laverie automatique", ["SA105"]),
}

vers_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True).transform
vers_gps = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform


def nombre(serie):
    """Convertit les valeurs Insee ('ns', 'nd', '12,5') en nombres."""
    return pd.to_numeric(serie.astype(str).str.replace(",", ".", regex=False), errors="coerce")


# 1. IRIS de Paris : contours, centre, surface
iris_geo = json.loads((SRC / "IRIS.geojson").read_text(encoding="utf-8"))
lignes, contours = [], []
for f in iris_geo["features"]:
    p = f["properties"]
    code = str(p["c_ir"])
    if not code.startswith("751"):
        continue
    forme = shape(f["geometry"])
    forme_l93 = shapely.transform(forme, lambda xy: np.column_stack(vers_l93(xy[:, 0], xy[:, 1])))
    centre = forme_l93.centroid
    lon, lat = vers_gps(centre.x, centre.y)
    lignes.append({
        "iris": code, "nom": p["l_ir"], "arrondissement": int(code[2:5]) - 100, "type_iris": p["c_typeir"],
        "x": centre.x, "y": centre.y, "lat": lat, "lon": lon, "surface_ha": forme_l93.area / 10_000,
    })
    contours.append({"type": "Feature", "id": code, "properties": {"iris": code},
                     "geometry": mapping(forme.simplify(0.00004, preserve_topology=True))})
iris = pd.DataFrame(lignes)

# 2. Habitants (recensement 2021) et revenu médian (Filosofi 2021)
pop = pd.read_csv(SRC / "base-ic-evol-struct-pop-2021.CSV", sep=";", dtype={"IRIS": str}, usecols=["IRIS", "P21_POP"])
filo = pd.read_csv(SRC / "BASE_TD_FILO_IRIS_2021_DISP.csv", sep=";", dtype=str, usecols=["IRIS", "DISP_MED21"])
iris = iris.merge(pop.rename(columns={"IRIS": "iris", "P21_POP": "habitants"}), on="iris", how="left")
iris = iris.merge(filo.rename(columns={"IRIS": "iris"}).assign(revenu_median=lambda d: nombre(d.DISP_MED21))[["iris", "revenu_median"]], on="iris", how="left")
iris["habitants"] = iris["habitants"].fillna(0).round()

# 3. Flux de voyageurs : validations moyennes par jour ouvré, par gare
val = pd.read_csv(SRC / "validations-reseau-ferre-nombre-validations-par-jour-4eme-trimestre.csv", sep=";")
val["JOUR"] = pd.to_datetime(val["JOUR"])
val = val[val["JOUR"].dt.dayofweek < 5].dropna(subset=["ID_ZDC"])
par_jour = val.groupby(["ID_ZDC", "JOUR"])["NB_VALD"].sum().groupby("ID_ZDC").mean()
gares = pd.DataFrame([g["properties"] for g in json.loads((SRC / "emplacement-des-gares-idf.geojson").read_text(encoding="utf-8"))["features"]])
gares = gares.groupby("id_ref_zdc").agg(nom=("nom_zdc", "first"), x=("x", "mean"), y=("y", "mean"),
                                         lignes=("res_com", lambda s: ", ".join(sorted(set(s)))))
gares["voyageurs_jour"] = par_jour.reindex(gares.index.astype(float)).values
gares = gares.dropna(subset=["voyageurs_jour"])
lon_g, lat_g = vers_gps(gares["x"].to_numpy(), gares["y"].to_numpy())
gares["lon"], gares["lat"] = lon_g, lat_g
gares = gares[(gares.lat.between(48.81, 48.91)) & (gares.lon.between(2.22, 2.47))]

# 4. Commerces actifs (BDCOM 2023) et concurrents dans un rayon de 400 m
com = pd.read_csv(SRC / "Local_commercial_par_taille_(6_postes).csv", encoding="utf-8-sig")
com = com[com["type"] == "C"]
centres = iris[["x", "y"]].to_numpy()


def autour(points, poids=None):
    """Somme (ou nombre) des points situés à moins de RAYON mètres du centre de chaque IRIS."""
    d = np.sqrt(((centres[:, None, :] - points[None, :, :]) ** 2).sum(axis=2))
    proches = d <= RAYON
    return proches.sum(axis=1) if poids is None else (proches * poids[None, :]).sum(axis=1)


iris["habitants_400m"] = autour(centres, iris["habitants"].to_numpy())
iris["flux_jour"] = autour(gares[["x", "y"]].to_numpy(), gares["voyageurs_jour"].to_numpy()).round()
iris["commerces_400m"] = autour(com[["X", "Y"]].to_numpy())
for cle, (_, codes) in TYPES.items():
    iris[f"n_{cle}"] = autour(com.loc[com["codact"].isin(codes), ["X", "Y"]].to_numpy())

# 5. Nombre de commerces de chaque type par arrondissement
arr = pd.DataFrame({cle: com[com["codact"].isin(codes)].groupby("arro").size() for cle, (_, codes) in TYPES.items()}).fillna(0).astype(int)
arr.index.name = "arrondissement"
arr.to_csv(OUT / "geo_arrondissements.csv")

# 6. Enregistrement
colonnes = ["iris", "nom", "arrondissement", "type_iris", "lat", "lon", "surface_ha", "habitants", "habitants_400m", "revenu_median",
            "flux_jour", "commerces_400m"] + [f"n_{c}" for c in TYPES]
iris[colonnes].round({"lat": 5, "lon": 5, "surface_ha": 1}).to_csv(OUT / "geo_iris.csv", index=False)
(OUT / "geo_iris.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": contours}, separators=(",", ":")), encoding="utf-8")
gares.reset_index()[["nom", "lignes", "lat", "lon", "voyageurs_jour"]].round({"lat": 5, "lon": 5, "voyageurs_jour": 0}).to_csv(OUT / "geo_gares.csv", index=False)

print(f"{len(iris)} IRIS, {len(com)} commerces actifs, {len(gares)} gares")
print(iris[["habitants", "habitants_400m", "revenu_median", "flux_jour", "commerces_400m"]].describe().round(0))
for cle, (nom, codes) in TYPES.items():
    print(f"{nom:28s} {com['codact'].isin(codes).sum():5d} commerces dans Paris")
