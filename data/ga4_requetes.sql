-- =====================================================================
-- Projet : parcours d'achat e-commerce en SQL (Google Analytics 4)
-- Données : export GA4 de la boutique Google Merchandise Store, anonymisé
--           et publié par Google dans BigQuery (1er nov. 2020 au 31 janv. 2021).
-- Table : bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*
--         une table par jour ; une ligne = un événement (page vue, ajout au panier, achat...)
-- Auteur : Salah Fetaissa
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. Activité quotidienne : visiteurs, sessions, achats, chiffre d'affaires
--    Question : comment évoluent le trafic et les ventes, et quel est l'effet du Black Friday et de Noël ?
-- ---------------------------------------------------------------------
SELECT
  PARSE_DATE('%Y%m%d', event_date) AS jour,
  COUNT(DISTINCT user_pseudo_id) AS visiteurs,
  -- une session = un visiteur + l'identifiant de session stocké dans les paramètres de l'événement
  COUNT(DISTINCT CONCAT(user_pseudo_id, '-',
        CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS STRING))) AS sessions,
  COUNTIF(event_name = 'purchase') AS achats,
  ROUND(SUM(IF(event_name = 'purchase', IFNULL(ecommerce.purchase_revenue_in_usd, 0), 0)), 2) AS chiffre_affaires_usd
FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`
WHERE _TABLE_SUFFIX BETWEEN '20201101' AND '20210131'
GROUP BY jour
ORDER BY jour;


-- ---------------------------------------------------------------------
-- 2. Entonnoir d'achat par appareil
--    Question : à quelle étape les visiteurs abandonnent-ils, et le mobile fait-il moins bien que l'ordinateur ?
-- ---------------------------------------------------------------------
WITH etapes AS (
  SELECT user_pseudo_id, device.category AS appareil, event_name
  FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`
  WHERE _TABLE_SUFFIX BETWEEN '20201101' AND '20210131'
    AND event_name IN ('session_start', 'view_item', 'add_to_cart', 'begin_checkout', 'add_payment_info', 'purchase')
)
SELECT
  appareil,
  COUNT(DISTINCT user_pseudo_id) AS visiteurs,
  COUNT(DISTINCT IF(event_name = 'view_item', user_pseudo_id, NULL)) AS vue_produit,
  COUNT(DISTINCT IF(event_name = 'add_to_cart', user_pseudo_id, NULL)) AS ajout_panier,
  COUNT(DISTINCT IF(event_name = 'begin_checkout', user_pseudo_id, NULL)) AS debut_paiement,
  COUNT(DISTINCT IF(event_name = 'add_payment_info', user_pseudo_id, NULL)) AS infos_paiement,
  COUNT(DISTINCT IF(event_name = 'purchase', user_pseudo_id, NULL)) AS achat
FROM etapes
GROUP BY appareil
ORDER BY visiteurs DESC;


-- ---------------------------------------------------------------------
-- 3. Canaux d'acquisition
--    Question : quels canaux amènent des acheteurs, et pas seulement du trafic ?
--    traffic_source = le canal par lequel le visiteur est arrivé la première fois
-- ---------------------------------------------------------------------
WITH par_visiteur AS (
  SELECT
    user_pseudo_id,
    ANY_VALUE(traffic_source.medium) AS support,
    ANY_VALUE(traffic_source.source) AS source,
    MAX(IF(event_name = 'purchase', 1, 0)) AS a_achete,
    SUM(IF(event_name = 'purchase', IFNULL(ecommerce.purchase_revenue_in_usd, 0), 0)) AS chiffre_affaires
  FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`
  WHERE _TABLE_SUFFIX BETWEEN '20201101' AND '20210131'
  GROUP BY user_pseudo_id
)
SELECT
  support,
  source,
  COUNT(*) AS visiteurs,
  SUM(a_achete) AS acheteurs,
  ROUND(100 * SUM(a_achete) / COUNT(*), 2) AS taux_conversion_pct,
  ROUND(SUM(chiffre_affaires), 2) AS chiffre_affaires_usd
FROM par_visiteur
GROUP BY support, source
HAVING COUNT(*) >= 100
ORDER BY visiteurs DESC;


-- ---------------------------------------------------------------------
-- 4. Fidélité par cohorte hebdomadaire
--    Question : quelle part des nouveaux visiteurs d'une semaine revient les semaines suivantes ?
-- ---------------------------------------------------------------------
WITH visites AS (
  SELECT DISTINCT
    user_pseudo_id,
    DATE_TRUNC(PARSE_DATE('%Y%m%d', event_date), WEEK(MONDAY)) AS semaine
  FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`
  WHERE _TABLE_SUFFIX BETWEEN '20201101' AND '20210131'
),
premiere_visite AS (
  SELECT user_pseudo_id, MIN(semaine) AS cohorte
  FROM visites
  GROUP BY user_pseudo_id
)
SELECT
  p.cohorte,
  DIV(DATE_DIFF(v.semaine, p.cohorte, DAY), 7) AS semaines_apres,
  COUNT(DISTINCT v.user_pseudo_id) AS visiteurs
FROM visites AS v
JOIN premiere_visite AS p USING (user_pseudo_id)
GROUP BY p.cohorte, semaines_apres
ORDER BY p.cohorte, semaines_apres;


-- ---------------------------------------------------------------------
-- 5. Produits : vus, ajoutés au panier, achetés
--    Question : quels produits attirent le regard mais ne se vendent pas, et l'inverse ?
--    items = la liste des produits concernés par l'événement (on la « déplie » avec UNNEST)
-- ---------------------------------------------------------------------
SELECT
  item.item_category AS categorie,
  item.item_name AS produit,
  COUNTIF(event_name = 'view_item') AS vues,
  COUNTIF(event_name = 'add_to_cart') AS ajouts_panier,
  SUM(IF(event_name = 'purchase', IFNULL(item.quantity, 0), 0)) AS quantites_vendues,
  ROUND(SUM(IF(event_name = 'purchase', IFNULL(item.item_revenue_in_usd, 0), 0)), 2) AS chiffre_affaires_usd
FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`,
  UNNEST(items) AS item
WHERE _TABLE_SUFFIX BETWEEN '20201101' AND '20210131'
  AND event_name IN ('view_item', 'add_to_cart', 'purchase')
GROUP BY categorie, produit
HAVING COUNTIF(event_name = 'view_item') >= 50
ORDER BY vues DESC;
