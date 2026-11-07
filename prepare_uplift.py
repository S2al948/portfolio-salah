"""Préparation du projet « Faut-il vraiment envoyer cet e-mail ? » (uplift, données Hillstrom / MineThatData).

À lancer une seule fois, en local (pip install pandas numpy scikit-learn).
Entrée : sources/hillstorm_no_indices.csv.gz (64 000 clients, test e-mail randomisé en 3 groupes)
Sorties (data/) :
  - uplift_ab.csv        : résultats du test A/B (taux, écart au groupe sans e-mail, intervalle de confiance à 95 %)
  - uplift_segments.csv  : effet de chaque e-mail par segment de clients
  - uplift_courbe.csv    : effet cumulé quand on cible les k % de clients au plus fort uplift prédit (sur l'échantillon test)
  - uplift_modele.csv    : qualité du modèle et répartition des e-mails recommandés
"""
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import train_test_split

SRC, OUT = Path("sources"), Path("data")
OUT.mkdir(exist_ok=True)
GRAINE = 42
GROUPES = {"Mens E-Mail": "homme", "Womens E-Mail": "femme", "No E-Mail": "aucun"}

d = pd.read_csv(SRC / "hillstorm_no_indices.csv.gz")
d["groupe"] = d["segment"].map(GROUPES)


# ---------------------------------------------------------------------------
# 1. Test A/B : effet moyen de chaque e-mail, avec intervalle de confiance à 95 %
# ---------------------------------------------------------------------------
def ecart(t, c):
    """Différence de moyennes et intervalle de confiance à 95 % (approximation normale)."""
    diff = t.mean() - c.mean()
    se = np.sqrt(t.var(ddof=1) / len(t) + c.var(ddof=1) / len(c))
    return diff, diff - 1.96 * se, diff + 1.96 * se


lignes = []
temoin = d[d.groupe == "aucun"]
for g in ["homme", "femme", "aucun"]:
    x = d[d.groupe == g]
    for indic in ["visit", "conversion", "spend"]:
        diff, bas, haut = ecart(x[indic], temoin[indic]) if g != "aucun" else (0.0, 0.0, 0.0)
        lignes.append({"groupe": g, "indicateur": indic, "clients": len(x), "moyenne": x[indic].mean(),
                       "ecart": diff, "ic_bas": bas, "ic_haut": haut})
pd.DataFrame(lignes).to_csv(OUT / "uplift_ab.csv", index=False)

# ---------------------------------------------------------------------------
# 2. Effet par segment (visites et dépense)
# ---------------------------------------------------------------------------
d["anciennete"] = pd.cut(d["recency"], [0, 3, 6, 9, 12], labels=["1-3 mois", "4-6 mois", "7-9 mois", "10-12 mois"])
d["achats_passes"] = np.select([(d.mens == 1) & (d.womens == 1), d.mens == 1], ["Homme et femme", "Homme seulement"], "Femme seulement")
d["client"] = np.where(d.newbie == 1, "Nouveau client (< 12 mois)", "Client fidèle")
d["montant_passe"] = d["history_segment"].str[3:]
DIMENSIONS = {"achats_passes": "Rayons achetés l'an passé", "anciennete": "Dernier achat", "client": "Ancienneté du client",
              "channel": "Canal d'achat", "zip_code": "Zone", "montant_passe": "Montant dépensé l'an passé"}
seg = []
for dim, libelle in DIMENSIONS.items():
    for valeur, x in d.groupby(dim, observed=True):
        c = x[x.groupe == "aucun"]
        for g in ["homme", "femme"]:
            t = x[x.groupe == g]
            dv, bv, hv = ecart(t.visit, c.visit)
            ds, bs, hs = ecart(t.spend, c.spend)
            seg.append({"dimension": libelle, "valeur": str(valeur).replace("Surburban", "Suburban"), "email": g, "clients": len(x),
                        "visite_temoin": c.visit.mean(), "uplift_visite": dv, "uplift_visite_bas": bv, "uplift_visite_haut": hv,
                        "uplift_depense": ds, "uplift_depense_bas": bs, "uplift_depense_haut": hs})
pd.DataFrame(seg).to_csv(OUT / "uplift_segments.csv", index=False)

# ---------------------------------------------------------------------------
# 3. Modèle d'uplift (T-learner) : une probabilité de visite par groupe, l'uplift = écart au groupe sans e-mail
# ---------------------------------------------------------------------------
X = pd.get_dummies(d[["recency", "history", "mens", "womens", "zip_code", "newbie", "channel"]], columns=["zip_code", "channel"], dtype=int)
train, test = train_test_split(d.index, test_size=0.5, random_state=GRAINE, stratify=d["groupe"])
proba = {}
for g in ["homme", "femme", "aucun"]:
    idx = train[d.loc[train, "groupe"] == g]
    modele = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, random_state=GRAINE)
    modele.fit(X.loc[idx], d.loc[idx, "visit"])
    proba[g] = modele.predict_proba(X.loc[test])[:, 1]
t = d.loc[test].copy()
t["uplift_homme"] = proba["homme"] - proba["aucun"]
t["uplift_femme"] = proba["femme"] - proba["aucun"]
t["email_reco"] = np.where(t.uplift_homme >= t.uplift_femme, "homme", "femme")
t["score"] = t[["uplift_homme", "uplift_femme"]].max(axis=1)
t = t.sort_values("score", ascending=False).reset_index(drop=True)
t["rang_pct"] = (np.arange(len(t)) + 1) / len(t) * 100


def effet_cible(part, aleatoire=False, seed=0):
    """Effet réel (mesuré) quand on envoie l'e-mail recommandé aux `part` % de clients choisis.
    On compare, dans le groupe ciblé, ceux qui ont reçu l'e-mail recommandé à ceux qui n'ont rien reçu (tirage au sort)."""
    base = t.sample(frac=1, random_state=seed).reset_index(drop=True) if aleatoire else t
    cible = base.iloc[: int(round(len(base) * part / 100))]
    recu = cible[cible.groupe == cible.email_reco]
    rien = cible[cible.groupe == "aucun"]
    if len(recu) < 30 or len(rien) < 30:
        return np.nan, np.nan, len(cible)
    return recu.visit.mean() - rien.visit.mean(), recu.spend.mean() - rien.spend.mean(), len(cible)


courbe = []
for k in range(5, 101, 5):
    uv, us, n = effet_cible(k)
    alea = [effet_cible(k, aleatoire=True, seed=s) for s in range(20)]
    courbe.append({"part_cible": k, "clients_cibles_sur_64000": round(64000 * k / 100),
                   "uplift_visite_par_client": uv, "uplift_depense_par_client": us,
                   "visites_en_plus_pour_64000": uv * 64000 * k / 100, "depense_en_plus_pour_64000": us * 64000 * k / 100,
                   "visites_en_plus_hasard": np.nanmean([a[0] for a in alea]) * 64000 * k / 100,
                   "depense_en_plus_hasard": np.nanmean([a[1] for a in alea]) * 64000 * k / 100})
c = pd.DataFrame(courbe)
c.to_csv(OUT / "uplift_courbe.csv", index=False)

# ---------------------------------------------------------------------------
# 4. Stratégies d'envoi comparées (toutes mesurées de la même façon, sur l'échantillon test)
# ---------------------------------------------------------------------------
def mesurer(choix):
    """Effet réel d'une stratégie : pour chaque e-mail, écart entre clients ayant reçu l'e-mail choisi pour eux et clients sans e-mail."""
    effet_v = effet_s = 0.0
    for e in ["homme", "femme"]:
        concernes = t[choix == e]
        if len(concernes) == 0:
            continue
        recu, rien = concernes[concernes.groupe == e], concernes[concernes.groupe == "aucun"]
        part = len(concernes) / len(t)
        effet_v += part * (recu.visit.mean() - rien.visit.mean())
        effet_s += part * (recu.spend.mean() - rien.spend.mean())
    return effet_v * 64000, effet_s * 64000


regle = np.where((t.womens == 1) & (t.mens == 0), "femme", "homme")
strategies = [
    ("Tout le monde reçoit l'e-mail femme", np.full(len(t), "femme")),
    ("Moitié homme, moitié femme (comme le test)", np.where(np.arange(len(t)) % 2 == 0, "homme", "femme")),
    ("Choix du modèle d'uplift, client par client", t.email_reco.to_numpy()),
    ("Tout le monde reçoit l'e-mail homme", np.full(len(t), "homme")),
    ("Règle simple : e-mail femme aux clientes du seul rayon femme, e-mail homme aux autres", regle),
]
pd.DataFrame([{"strategie": nom, "visites_en_plus": mesurer(ch)[0], "depense_en_plus": mesurer(ch)[1]} for nom, ch in strategies]) \
    .to_csv(OUT / "uplift_strategies.csv", index=False)
print(pd.read_csv(OUT / "uplift_strategies.csv").round(0).to_string())

total = c.loc[c.part_cible == 100, "visites_en_plus_pour_64000"].iloc[0]
pd.DataFrame([
    {"cle": "clients_test", "valeur": len(t)},
    {"cle": "part_email_homme_recommande", "valeur": (t.email_reco == "homme").mean() * 100},
    {"cle": "part_homme_dans_top30", "valeur": (t.head(int(len(t) * 0.3)).email_reco == "homme").mean() * 100},
    {"cle": "visites_en_plus_tout_le_monde", "valeur": total},
]).to_csv(OUT / "uplift_modele.csv", index=False)

print(pd.read_csv(OUT / "uplift_ab.csv").round(4).to_string())
print(c.round(3).to_string())
print(pd.read_csv(OUT / "uplift_modele.csv").round(2).to_string())
