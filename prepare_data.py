"""
Préparation des données du portfolio.

Ce script lit les fichiers bruts (Kaggle / UCI), les nettoie, puis écrit des fichiers
compacts dans le dossier data/ que l'application Streamlit lit directement.
Chaque nettoyage est consigné dans un journal (data/journal_<projet>.csv) affiché dans l'app.

Utilisation :
    python prepare_data.py --raw ./raw --out ./data            # tous les projets
    python prepare_data.py --raw ./raw --out ./data --only olist

Dossiers attendus dans raw/ :
    olist/        les 9 fichiers du jeu "Brazilian E-Commerce Public Dataset by Olist"
    mql/          olist_marketing_qualified_leads_dataset.csv et olist_closed_deals_dataset.csv
    retail/       Online Retail.xlsx
    favorita/     train.csv, stores.csv, oil.csv, holidays_events.csv, transactions.csv
    personality/  marketing_campaign.csv
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def noter(journal, operation, lignes, justification):
    """Ajoute une ligne au journal de nettoyage."""
    journal.append({"Opération": operation, "Lignes concernées": int(lignes), "Justification": justification})


def ecrire_journal(journal, sortie, nom):
    pd.DataFrame(journal).to_csv(sortie / f"journal_{nom}.csv", index=False)


# ---------------------------------------------------------
# Projet 1 : Olist e-commerce (commandes, livraisons, avis)
# ---------------------------------------------------------
def preparer_olist(dossier, sortie):
    journal = []
    colonnes_dates = ["order_purchase_timestamp", "order_delivered_customer_date", "order_estimated_delivery_date"]
    commandes = pd.read_csv(dossier / "olist_orders_dataset.csv", parse_dates=colonnes_dates)
    clients = pd.read_csv(dossier / "olist_customers_dataset.csv")[["customer_id", "customer_state"]]
    articles = pd.read_csv(dossier / "olist_order_items_dataset.csv")
    produits = pd.read_csv(dossier / "olist_products_dataset.csv")[["product_id", "product_category_name"]]
    traduction = pd.read_csv(dossier / "product_category_name_translation.csv")
    avis = pd.read_csv(dossier / "olist_order_reviews_dataset.csv", parse_dates=["review_answer_timestamp"])

    noter(journal, "Commandes dans le fichier brut", len(commandes), "Point de départ.")

    # 1) On ne garde que les commandes livrées
    non_livrees = commandes["order_status"] != "delivered"
    noter(journal, "Commandes non livrées retirées", non_livrees.sum(),
          "Annulées, indisponibles ou en cours : pas de délai de livraison ni d'avis à analyser.")
    commandes = commandes[~non_livrees]

    # 2) Commandes livrées sans date de livraison
    sans_date = commandes["order_delivered_customer_date"].isna()
    noter(journal, "Commandes livrées sans date de livraison retirées", sans_date.sum(),
          "Impossible de calculer le retard.")
    commandes = commandes[~sans_date]

    # 3) Avis en double : on garde le plus récent de chaque commande
    nb_commandes_doublons = avis["order_id"].duplicated(keep=False).groupby(avis["order_id"]).first().sum()
    avis = avis.sort_values("review_answer_timestamp").drop_duplicates("order_id", keep="last")
    noter(journal, "Commandes avec plusieurs avis (dernier avis conservé)", nb_commandes_doublons,
          "Une commande doit avoir une seule note, sinon elle serait comptée plusieurs fois.")

    # 4) Catégorie principale = celle de l'article le plus cher de la commande
    articles = articles.merge(produits, on="product_id", how="left")
    manquantes = articles["product_category_name"].isna()
    noter(journal, "Articles sans catégorie (classés « non renseignée »)", manquantes.sum(),
          "On garde ces articles dans le chiffre d'affaires, avec une catégorie explicite.")
    articles["product_category_name"] = articles["product_category_name"].fillna("non renseignée")
    articles = articles.merge(traduction, on="product_category_name", how="left")
    articles["categorie"] = articles["product_category_name_english"].fillna(articles["product_category_name"])
    principale = articles.sort_values("price", ascending=False).drop_duplicates("order_id")[["order_id", "categorie"]]
    totaux = articles.groupby("order_id").agg(
        nb_articles=("order_item_id", "count"), montant=("price", "sum"), frais_port=("freight_value", "sum")
    ).reset_index()

    # 5) Assemblage et indicateurs de livraison
    df = (commandes.merge(clients, on="customer_id", how="left")
          .merge(totaux, on="order_id", how="left")
          .merge(principale, on="order_id", how="left")
          .merge(avis[["order_id", "review_score"]], on="order_id", how="left"))
    achat = df["order_purchase_timestamp"]
    livre = df["order_delivered_customer_date"]
    df["delai_jours"] = ((livre - achat).dt.total_seconds() / 86400).round(1)
    df["retard_jours"] = (livre.dt.normalize() - df["order_estimated_delivery_date"].dt.normalize()).dt.days
    df["mois"] = achat.dt.strftime("%Y-%m")

    sans_avis = df["review_score"].isna()
    noter(journal, "Commandes sans avis (gardées)", sans_avis.sum(),
          "Gardées pour le chiffre d'affaires, ignorées dans les calculs de note.")

    # 6) Période d'analyse : les mois de 2016 et fin 2018 sont presque vides
    hors_periode = (df["mois"] < "2017-01") | (df["mois"] > "2018-08")
    noter(journal, "Commandes hors période janvier 2017 - août 2018 retirées", hors_periode.sum(),
          "Mois quasi vides : ils fausseraient les évolutions mensuelles.")
    df = df[~hors_periode]

    df = df.rename(columns={"customer_state": "etat", "review_score": "note"})
    df = df[["mois", "etat", "categorie", "nb_articles", "montant", "frais_port", "delai_jours", "retard_jours", "note"]]
    noter(journal, "Commandes conservées pour l'analyse", len(df), "Table finale : une ligne par commande livrée.")
    df.round(2).to_csv(sortie / "olist_commandes.csv.gz", index=False)
    ecrire_journal(journal, sortie, "olist")
    return len(df)


# ---------------------------------------------------------
# Projet 2 : Online Retail (transactions, RFM)
# ---------------------------------------------------------
def preparer_retail(fichier, sortie):
    journal = []
    brut = pd.read_excel(fichier)
    noter(journal, "Lignes de transaction dans le fichier brut", len(brut), "Point de départ (1 ligne = 1 produit sur une facture).")

    df = brut.drop_duplicates()
    noter(journal, "Lignes en double supprimées", len(brut) - len(df), "Lignes strictement identiques : ventes comptées deux fois.")

    annulation = df["InvoiceNo"].astype(str).str.startswith("C")
    retours = df[annulation].copy()
    df = df[~annulation]
    noter(journal, "Lignes d'annulation (factures « C ») séparées", annulation.sum(),
          "Gardées à part pour mesurer les retours, retirées des ventes.")

    produit = df["StockCode"].astype(str).str.match(r"^\d{5}[A-Za-z]{0,2}$")
    noter(journal, "Lignes hors produits retirées (port, frais, ajustements)", (~produit).sum(),
          "Codes comme POST, D, M, BANK CHARGES : ce ne sont pas des produits vendus.")
    df = df[produit]
    retours = retours[retours["StockCode"].astype(str).str.match(r"^\d{5}[A-Za-z]{0,2}$")]

    invalide = (df["Quantity"] <= 0) | (df["UnitPrice"] <= 0)
    noter(journal, "Lignes avec quantité ou prix nul ou négatif retirées", invalide.sum(),
          "Corrections de stock ou cadeaux : pas de vraie vente.")
    df = df[~invalide].copy()

    df["montant"] = df["Quantity"] * df["UnitPrice"]
    sans_client = df["CustomerID"].isna()
    noter(journal, "Lignes sans identifiant client (gardées pour le chiffre d'affaires global)", sans_client.sum(),
          "Utilisées pour le CA mensuel, mais exclues de la segmentation RFM qui exige un client identifié.")

    # Factures des clients identifiés (base de la segmentation RFM)
    identifies = df[~sans_client]
    factures = identifies.groupby("InvoiceNo").agg(
        client=("CustomerID", "first"),
        date=("InvoiceDate", "min"),
        pays=("Country", "first"),
        montant=("montant", "sum"),
        quantite=("Quantity", "sum"),
        nb_lignes=("StockCode", "count"),
    ).reset_index()
    factures["client"] = factures["client"].astype(int)
    factures["date"] = factures["date"].dt.strftime("%Y-%m-%d %H:%M")
    factures.round(2).to_csv(sortie / "retail_factures.csv.gz", index=False)

    # Chiffre d'affaires mensuel (tous clients) et retours
    df["mois"] = df["InvoiceDate"].dt.strftime("%Y-%m")
    mensuel = df.groupby("mois").agg(ca=("montant", "sum"), nb_factures=("InvoiceNo", "nunique")).reset_index()
    mensuel["ca_clients_identifies"] = df[~sans_client].groupby("mois")["montant"].sum().reindex(mensuel["mois"]).values
    retours["mois"] = retours["InvoiceDate"].dt.strftime("%Y-%m")
    retours["montant"] = (retours["Quantity"] * retours["UnitPrice"]).abs()
    mensuel["ca_retours"] = retours.groupby("mois")["montant"].sum().reindex(mensuel["mois"]).fillna(0).values
    mensuel.round(2).to_csv(sortie / "retail_mensuel.csv", index=False)

    # Produits
    description = df.groupby("StockCode")["Description"].agg(lambda s: s.mode().iat[0] if not s.mode().empty else "")
    produits = df.groupby("StockCode").agg(quantite=("Quantity", "sum"), ca=("montant", "sum"),
                                           nb_factures=("InvoiceNo", "nunique")).reset_index()
    produits["description"] = produits["StockCode"].map(description)
    produits.round(2).to_csv(sortie / "retail_produits.csv", index=False)

    noter(journal, "Factures de clients identifiés conservées", len(factures), "Table finale utilisée pour la segmentation RFM.")
    ecrire_journal(journal, sortie, "retail")
    return len(factures)


# ---------------------------------------------------------
# Projet 3 : Customer Personality Analysis (réponse aux campagnes)
# ---------------------------------------------------------
def preparer_clients(fichier, sortie):
    journal = []
    df = pd.read_csv(fichier, sep=";")
    noter(journal, "Clients dans le fichier brut", len(df), "Point de départ (séparateur « ; »).")

    doublons = df.drop(columns="ID").duplicated()
    noter(journal, "Clients en double supprimés (même profil, ID différent)", doublons.sum(),
          "Les mêmes valeurs sur toutes les colonnes : très probablement des lignes dupliquées.")
    df = df[~doublons]

    sans_revenu = df["Income"].isna()
    noter(journal, "Clients sans revenu retirés", sans_revenu.sum(), "Le revenu est une variable clé : on ne l'invente pas.")
    df = df[~sans_revenu]

    aberrant_revenu = df["Income"] > 200000
    noter(journal, "Revenus aberrants retirés (plus de 200 000)", aberrant_revenu.sum(),
          "Valeurs isolées très au-dessus des autres (max 666 666) : elles tireraient les moyennes.")
    df = df[~aberrant_revenu]

    trop_ages = df["Year_Birth"] < 1920
    noter(journal, "Années de naissance impossibles retirées (avant 1920)", trop_ages.sum(), "Clients de plus de 100 ans : erreurs de saisie.")
    df = df[~trop_ages].copy()

    statut = {"Alone": "Single", "Absurd": "Non précisé", "YOLO": "Non précisé"}
    noter(journal, "Situations familiales regroupées (Alone, Absurd, YOLO)", df["Marital_Status"].isin(statut).sum(),
          "« Alone » devient « Single » ; « Absurd » et « YOLO » deviennent « Non précisé ».")
    df["Marital_Status"] = df["Marital_Status"].replace(statut)

    # Variables dérivées (la dernière inscription date de décembre 2014)
    depenses = [c for c in df.columns if c.startswith("Mnt")]
    achats = ["NumWebPurchases", "NumCatalogPurchases", "NumStorePurchases"]
    df["age"] = 2014 - df["Year_Birth"]
    df["depense_totale"] = df[depenses].sum(axis=1)
    df["nb_achats"] = df[achats].sum(axis=1)
    df["nb_enfants"] = df["Kidhome"] + df["Teenhome"]
    df["campagnes_acceptees_avant"] = df[[f"AcceptedCmp{i}" for i in range(1, 6)]].sum(axis=1)
    df["anciennete_jours"] = (pd.Timestamp("2014-12-31") - pd.to_datetime(df["Dt_Customer"])).dt.days
    noter(journal, "Colonnes créées", 6, "Âge, dépense totale, nombre d'achats, nombre d'enfants, campagnes acceptées, ancienneté.")

    df = df.drop(columns=["Z_CostContact", "Z_Revenue", "Dt_Customer"])
    noter(journal, "Colonnes constantes supprimées (Z_CostContact, Z_Revenue)", 2, "Même valeur pour tous les clients : aucune information.")
    noter(journal, "Clients conservés pour l'analyse", len(df), "Table finale.")
    df.to_csv(sortie / "clients_campagne.csv", index=False)
    ecrire_journal(journal, sortie, "clients")
    return len(df)


# ---------------------------------------------------------
# Projet 4 : Favorita (séries temporelles de ventes)
# ---------------------------------------------------------
def preparer_favorita(dossier, sortie):
    journal = []
    train = pd.read_csv(dossier / "train.csv", parse_dates=["date"])
    magasins = pd.read_csv(dossier / "stores.csv")
    petrole = pd.read_csv(dossier / "oil.csv", parse_dates=["date"])
    fetes = pd.read_csv(dossier / "holidays_events.csv", parse_dates=["date"])
    transactions = pd.read_csv(dossier / "transactions.csv", parse_dates=["date"])
    noter(journal, "Lignes dans train.csv (1 ligne = 1 magasin, 1 famille, 1 jour)", len(train), "Point de départ.")
    noter(journal, "Lignes à zéro vente (informatif, gardées)", (train["sales"] == 0).sum(),
          "Beaucoup de familles n'existent pas encore dans certains magasins : ce sont de vrais zéros.")

    # Série quotidienne : tous magasins et toutes familles confondus
    jour = train.groupby("date").agg(ventes=("sales", "sum"), promos=("onpromotion", "sum")).reset_index()
    toutes_les_dates = pd.date_range(jour["date"].min(), jour["date"].max())
    manquants = toutes_les_dates.difference(jour["date"])
    noter(journal, "Jours absents de la série (25 décembre de chaque année)", len(manquants),
          "Magasins fermés : ces jours sont exclus plutôt que remplis avec des valeurs inventées.")

    jour = jour.merge(transactions.groupby("date")["transactions"].sum().reset_index(), on="date", how="left")

    # Prix du pétrole : on reconduit le dernier prix connu pour les jours sans cotation
    petrole = petrole.set_index("date").reindex(toutes_les_dates)
    noter(journal, "Prix du pétrole manquants (week-ends, jours fériés US)", petrole["dcoilwtico"].isna().sum(),
          "On reconduit le dernier prix connu ; le premier jour est comblé par le suivant.")
    petrole["dcoilwtico"] = petrole["dcoilwtico"].ffill().bfill()
    jour = jour.merge(petrole["dcoilwtico"].rename("petrole").rename_axis("date").reset_index(), on="date", how="left")

    # Jours fériés nationaux réellement chômés (les jours « transférés » comptent à leur nouvelle date)
    nationaux = fetes[fetes["locale"] == "National"]
    vrais = nationaux[((nationaux["type"].isin(["Holiday", "Additional", "Bridge"])) & (~nationaux["transferred"]))
                      | (nationaux["type"] == "Transfer")]
    jour["ferie_national"] = jour["date"].isin(vrais["date"])
    noter(journal, "Jours fériés nationaux repérés", jour["ferie_national"].sum(),
          "Jours fériés nationaux effectivement célébrés (fêtes transférées prises à leur nouvelle date).")
    jour["date"] = jour["date"].dt.strftime("%Y-%m-%d")
    jour.round(2).to_csv(sortie / "favorita_jour.csv", index=False)

    # Ventes mensuelles par famille de produits
    train["mois"] = train["date"].dt.strftime("%Y-%m")
    familles = train.groupby(["mois", "family"])["sales"].sum().reset_index().rename(columns={"family": "famille", "sales": "ventes"})
    familles.round(0).to_csv(sortie / "favorita_familles.csv", index=False)

    # Ventes par magasin
    par_magasin = train.groupby("store_nbr")["sales"].sum().rename("ventes").reset_index()
    tr_mag = transactions.groupby("store_nbr")["transactions"].sum().reset_index()
    magasins = (magasins.merge(par_magasin, on="store_nbr").merge(tr_mag, on="store_nbr", how="left")
                .rename(columns={"city": "ville", "state": "province", "type": "type_magasin"}))
    magasins.round(0).to_csv(sortie / "favorita_magasins.csv", index=False)

    noter(journal, "Jours conservés dans la série quotidienne", len(jour), "Du 1er janvier 2013 au 15 août 2017.")
    ecrire_journal(journal, sortie, "favorita")
    return len(jour)


# ---------------------------------------------------------
# Projet 5 : Olist Marketing Funnel (leads qualifiés et ventes conclues)
# ---------------------------------------------------------
def preparer_leads(dossier, sortie):
    journal = []
    leads = pd.read_csv(dossier / "olist_marketing_qualified_leads_dataset.csv", parse_dates=["first_contact_date"])
    deals = pd.read_csv(dossier / "olist_closed_deals_dataset.csv", parse_dates=["won_date"])
    noter(journal, "Leads qualifiés (MQL) dans le fichier brut", len(leads), "Une ligne par lead.")
    noter(journal, "Affaires conclues dans le fichier brut", len(deals), "Chaque affaire est rattachée à un lead par mql_id.")

    df = leads.merge(deals[["mql_id", "won_date", "business_segment", "lead_type", "business_type"]], on="mql_id", how="left")
    df["gagne"] = df["won_date"].notna()
    df["cycle_jours"] = (df["won_date"].dt.normalize() - df["first_contact_date"]).dt.days

    negatif = df["cycle_jours"] < 0
    noter(journal, "Cycle de vente négatif mis à vide", negatif.sum(), "Affaire gagnée avant le premier contact : incohérent.")
    df.loc[negatif, "cycle_jours"] = np.nan

    sans_origine = df["origin"].isna()
    noter(journal, "Origine manquante renommée « non renseigné »", sans_origine.sum(), "On garde les leads avec une origine explicite.")
    df["origin"] = df["origin"].fillna("non renseigné")

    df["mois_contact"] = df["first_contact_date"].dt.strftime("%Y-%m")
    df["page"] = df["landing_page_id"].str[:8]
    df = df.rename(columns={"origin": "origine", "business_segment": "segment", "lead_type": "type_lead", "business_type": "type_vendeur"})
    df["date_contact"] = df["first_contact_date"].dt.strftime("%Y-%m-%d")
    df = df[["mois_contact", "date_contact", "origine", "page", "gagne", "cycle_jours", "segment", "type_lead", "type_vendeur"]]
    noter(journal, "Leads conservés", len(df), "Table finale : une ligne par lead, avec l'issue (gagné ou non).")
    df.to_csv(sortie / "leads.csv", index=False)
    ecrire_journal(journal, sortie, "leads")
    return len(df)


# ---------------------------------------------------------
# Lancement
# ---------------------------------------------------------
def main():
    parseur = argparse.ArgumentParser(description="Prépare les données du portfolio.")
    parseur.add_argument("--raw", default="raw", help="dossier des fichiers bruts")
    parseur.add_argument("--out", default="data", help="dossier de sortie")
    parseur.add_argument("--only", choices=["olist", "retail", "clients", "favorita", "leads"], help="un seul projet")
    args = parseur.parse_args()

    brut = Path(args.raw)
    sortie = Path(args.out)
    sortie.mkdir(parents=True, exist_ok=True)

    taches = {
        "olist": lambda: preparer_olist(brut / "olist", sortie),
        "retail": lambda: preparer_retail(brut / "retail" / "Online Retail.xlsx", sortie),
        "clients": lambda: preparer_clients(brut / "personality" / "marketing_campaign.csv", sortie),
        "favorita": lambda: preparer_favorita(brut / "favorita", sortie),
        "leads": lambda: preparer_leads(brut / "mql", sortie),
    }
    for nom, tache in taches.items():
        if args.only and args.only != nom:
            continue
        print(f"Préparation : {nom} ...")
        print(f"  OK, {tache()} lignes dans la table finale")


if __name__ == "__main__":
    main()
