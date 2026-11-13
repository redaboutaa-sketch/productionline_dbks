# ProdLine CoPilot — adaptation Aperam Genk (GNK)

Ce dépôt part de l'accélérateur Databricks *ProdLine CoPilot* (ligne d'embouteillage simulée).
Cette branche l'adapte au site **Aperam Genk** : les agents raisonnent désormais sur les **vraies
données Aperam** (`metal_movements`, `timebased_sensor`, arbre ARMP) au lieu du simulateur.

> Mode activé par la variable `COPILOT_DOMAIN=aperam_gnk` (déjà mise dans `app.yaml`, `app.dev.yaml`
> et `config/project-settings.yaml`). Sans cette variable, l'application se comporte exactement comme
> l'accélérateur d'origine. Ses 2 535 tests passent toujours. Les 5 échecs déjà présents viennent de
> l'environnement de test, pas de cette adaptation.

## 1. Ce qui a été adapté

| Brique d'origine (embouteillage) | Équivalent GNK | Fichier |
| --- | --- | --- |
| 19 machines `FILLER`, `CAPPER`… (`src/data_generation/config.py`) | ~40 lignes GNK identifiées par **code ARMP niveau 3** (`GN-CR-APL3`…), avec codes machine locaux, étape du flux et enveloppe technique publiée (posters du site) | `src/aperam_gnk/assets.py` |
| — | Table de correspondance `Machine` → ARMP fournie par Aperam | `src/aperam_gnk/reference/armp_machine_mapping_gnk.csv` |
| Télémétrie simulée → bronze → `gold_history_*` | Couche silver/gold SQL **sur vos tables sources** (Site_FK = 3) | `src/aperam_gnk/sql/*.sql` |
| Buffers entre machines (moteur de cascade) | **Files d'attente (WIP) devant chaque ligne** : bobines, tonnes, âge, blocages BLOQ | `gold_gnk_wip_buffers`, `gold_gnk_coil_position` |
| OEE A × P × Q | **OEE proxy** = utilisation × efficacité de marche (ROT / temps sur ligne) × ratio vitesse (réalisée / consigne) × rendement métal | `gold_gnk_line_kpi_shift_oee` |
| Événements d'arrêt simulés | **Trous entre bobines** ≥ 15 min sur une ligne | `gold_gnk_line_gaps` |
| Capteurs simulés | Santé capteurs horaire + **profil capteur par bobine** (jointure `Domain` = `Asset_Tree` et horodatage dans [début, fin] de la bobine) | `gold_gnk_sensor_health_hourly`, `gold_gnk_sensor_by_coil` |
| Contexte live Lakebase | Contexte construit depuis la couche gold : état des lignes (marche / dégradée / arrêtée avec file / affamée), KPI du poste, situations ouvertes | `agent_server/aperam_gnk/context.py` |
| Prompts « production line » | Orchestrateur (EN/FR/NL) + 8 spécialistes + synthèse réécrits pour l'inox Genk | `agent_server/aperam_gnk/prompts.py` |
| Outils lisant les tables d'embouteillage | 4 outils SQL déterministes : `gnk_line_status`, `gnk_coil_trace`, `gnk_sensor_scan`, `gnk_flow_overview` (+ `sql_query`, `calculator`) | `agent_server/aperam_gnk/tools.py` |
| OEE A × P × Q | **OEE journalier selon la définition Aperam** : T0 × T1 × T2 × T3, avec T0 et T1 officiels (`indicators_day_label`), T2 et T3 approchés depuis `metal_movements`, Pareto par code TUC, MTBF / MTTR | `src/aperam_gnk/sql/60_gold_gnk_time_model.sql` |
| Générateur de données synthétiques | Générateur au **schéma exact** de vos tables, avec 3 incidents injectés (panne MKW2, zone 7 four BUL3, point de rosée BAL1) | `src/aperam_gnk/synthetic.py` |

### OEE selon la définition Aperam (`indicators_day_label`)

Source des définitions : documentation Aperam « OEE DWH ». La table contient une ligne par ressource et
par **jour calendaire (0 h-24 h)**. CT vaut 86 400 s par jour et par ressource.

```
OEE = T1 × T2 × T3                         (disponibilité × performance × qualité)
OEE capacité = T0 × T1 × T2 × T3
T0 ouverture      = LT / CT                (temps requis / temps calendaire)
T1 disponibilité  = ROT / LT               (temps de fonctionnement / temps requis)
T2 performance    = vitesse réelle / vitesse nominale (design)
T3 qualité        = produits bons / production totale
```

| Bande | Code TUC | Libellé Aperam |
| --- | --- | --- |
| T0 (hors LT) | SDC, SDM, SDE | Arrêt usine, manque de matière, manque d'électricité |
| T0 (hors LT) | SDO1, SDO2 | Maintenance annuelle, investissements |
| T1 (dans LT) | SDO3, SDO4, SDP | Maintenance périodique, nettoyage, manque de personnel |
| T1 (dans LT) | FTT, FTP, FTO, FTS, FTW | Pannes et arrêts : technique, produit/qualité, opérateur, personnel, attente |
| T1 (dans LT) | UOT | Temps improductif standard (changements, chargement…) |
| T2 | ROT | Temps de fonctionnement |

Égalités vérifiées sur les données : TS = FT + UOT + ROT, TS = LT − SDO3 − SDO4, T1 = ROT / LT,
MTTR = FT / N_FT, MTBF = TS / N_FT. SDP est supposé dans LT, conformément à la définition ; aucune
ligne de données disponible ne contenait de SDP pour le vérifier.

**Codes sans définition validée.** Les agents les citent tels quels (ex. « FTTM/TS = 4,1 % »). Quand
il faut préciser leur nature, ils écrivent seulement « ratio d'indicateur machine (Genk), définition en
attente de validation ».

- Sous-codes `FTTE`, `FTTG`, `FTTI`, `FTTM` : ce sont des sous-codes de FTT, rien de plus n'est connu.
- `N_FUST` : libellé source « # Furnace Stops », définition provisoire.
- `N_STRI` : libellé source « # Stringers ».

Les définitions sont à demander au responsable des indicateurs OEE de Genk.

**Dans le copilote**, T0 et T1 sont officiels : ils viennent de la table d'indicateurs. T2 et T3 sont
des **approximations** calculées à partir de `metal_movements` :

- T2 ≈ vitesse réalisée / vitesse de consigne. La consigne n'est pas la vitesse nominale, donc l'effet
  mix n'est pas capté et T2 est surestimé.
- T3 ≈ rendement métal (poids sortie / poids entrée).

Les agents le précisent systématiquement.

**Périmètre site.** La table contient plusieurs sites. Seules les ressources déclarées `site = GNK`
dans `src/aperam_gnk/reference/resource_map_gnk.csv` entrent dans les vues GNK. `RD79` est une
ressource de **Gueugnon (GGN)**, elle est exclue. La vue `gnk_indicators_resources` liste toutes les
ressources présentes dans la source, avec leurs dates : elle sert à identifier les ressources de Genk à
déclarer.

Vues : `gnk_indicators_day` (heures par code TUC), `gold_gnk_time_losses_day` (Pareto par code TUC et
bande T0 / T1) et `gold_gnk_oee_day` (`oee_t1t2t3`, `oee_t0t1t2t3`, `t2_proxy`, `t3_proxy`).

### Les 8 spécialistes en mode GNK

| Spécialiste | Questions types à Genk |
| --- | --- |
| `downtime_analyst` | « Pourquoi MKW2 est arrêté ? », « combien de tonnes perdues sur la nuit ? » |
| `quality_specialist` | « Quelles bobines sont BLOQ et pourquoi ? », « trace la bobine G26…  » |
| `supply_chain_advisor` (flux matière) | « Où s'accumule le métal ? », « BUL3 va-t-elle manquer de bobines noires ? » |
| `oee_coach` | « Quelles pertes sur BUL2 ce poste ? », « vitesse vs consigne sur CRM3 » |
| `schedule_optimizer` | « Peut-on basculer des bobines de CRM2 vers CRM4 ? » |
| `maintenance_predictor` | « Dérive des températures four BUL3 ? », « moufle BAL1 ? » |
| `strategic_advisor` | « Goulot structurel du site sur 4 semaines ? » |
| `shift_briefing` | « Prépare la passation du poste de nuit » |

## 2. Déploiement

1. **Paramètres** : dans `config/project-settings.local.yaml`, renseigner les champs habituels de
   l'accélérateur, `copilot_domain: 'aperam_gnk'` et `gnk_source_schema: '<catalog>.<schema>'` (le
   schéma qui contient `metal_movements` et `timebased_sensor`).
2. **Créer la couche GNK** dans le schéma de l'app (`uc_catalog.uc_schema`). Par défaut, la table
   d'indicateurs journaliers lue est `dev_weu_next_best_action.raw.indicators_day_label`. L'option
   `--indicators <catalog.schema.table>` en désigne une autre ; `--indicators ''` désactive cette partie.

   ```bash
   python -m src.aperam_gnk.views --src <catalog>.<schema_source> --tgt <uc_catalog>.<uc_schema> --dry-run
   ```

   ```bash
   python -m src.aperam_gnk.views --src <catalog>.<schema_source> --tgt <uc_catalog>.<uc_schema> --warehouse-id <id>
   ```

   La table `gold_gnk_sensor_by_coil` est matérialisée (jointure coûteuse). Il faut la rafraîchir
   périodiquement en relançant la commande (un job Databricks planifié est l'étape suivante).
3. **Droits** : le service principal de l'app a besoin de `SELECT` sur le schéma source et sur le schéma cible.
4. Déployer l'app comme d'habitude : `./scripts/deploy.sh dev`.

### Sans données de production (dev / démo)

```bash
python -m src.aperam_gnk.synthetic --days 7 --uc-target <catalog>.<schema_dev>
```

Cette commande s'exécute dans un notebook ou un job Databricks. Elle écrit `metal_movements` et
`timebased_sensor` synthétiques au schéma exact, sur lesquels on déploie ensuite la couche GNK.

### Vérifier localement (sans workspace)

```bash
PYSPARK_PYTHON=$(which python) python scripts/aperam/validate_gnk_views_local.py
```

Le script génère 3 jours synthétiques et exécute toutes les vues SQL sur Spark local. Il exécute
aussi les requêtes du contexte et des 4 outils GNK. Il échoue si une vue est vide ou si une requête
est en erreur. Les tests unitaires se lancent avec `pytest tests/aperam_gnk`.

## 3. Hypothèses à valider avec les propriétaires des données

Le code est écrit pour être corrigé en un seul endroit si une hypothèse est fausse.

1. **Identifiant de bobine** : `Metal` reste-t-il stable de la réception bobine noire jusqu'à
   l'emballage ? Les calculs de rendement, de réduction d'épaisseur et d'attente entre lignes en
   dépendent (`20_gold_gnk_coil_ops.sql`). Si l'identifiant change (refendage, reprise…), il faut une
   table de généalogie.
2. **`OperationStatus`** : interprétation retenue : EXEC exécutée, COUR en cours, DISP disponible,
   NDIS non disponible, BLOQ bloquée qualité/technique, SUPP supprimée. À confirmer.
3. **Horodatages début/fin** : leur sens n'est documenté que pour les lignes de recuit-décapage.
   Pour les laminoirs (passes multiples), les refendeuses et le recuit base, que représentent-ils ?
4. **Unités** : vitesses en m/min, `DurationROT` en minutes, `Weight` en kg (source documentée).
   Le cas du four batch (KLOK), où plusieurs bobines sont traitées simultanément, est à confirmer.
5. **Capteurs** : `Domain` est-il toujours l'ARMP niveau 3, identique à `Asset_Tree` ? Où se trouve la
   table maître des tags (description, unité, `Range_Lower_Limit` / `Range_Upper_Limit`) ?
   `gnk_sensor_tags` est aujourd'hui générée à partir de modèles synthétiques : il faut la remplacer
   par la vraie table maître.
6. **Rôle de BUL4, CRM5, SL52** sur le flux : absents des posters du site.
7. **Postes** : 3 × 8 h, 06-14 / 14-22 / 22-06, en heure locale (Europe/Brussels) ?
8. **Emballage** : seules les opérations `PCKI`, `PCKG`, `PCK` comptent (commentaire du mapping).
9. **Indicateurs journaliers** :
   - quelles ressources de `indicators_day_label` appartiennent à Genk, et à quelles lignes ARMP
     correspondent-elles ? (`RD79` est à Gueugnon.)
   - quelle est la définition des sous-codes FTTE / FTTG / FTTI / FTTM, de `N_FUST` et de `N_STRI` ?
   - où trouver les vitesses nominales (design speed), pour un vrai T2, et les produits bons / production
     totale, pour un vrai T3 ?
   - les données montrent que MTBF et MTTR sont calculés avec `N_FT`, et non avec `N_FUST`
     (ex. 2015-10-28 : N_FT = 9, N_FUST = 8, MTTR = FT / 9). À confirmer.
10. **Aciérie** : `Metal` désigne-t-il la coulée ou la brame sur EAF/AOD/CCS ? Comment se fait le lien
   brame → bobine noire après Châtelet ?

## 4. Ce qui reste à faire (phase 2)

- **Control Tower (UI React)** : le schéma animé et les tuiles représentent encore la ligne
  d'embouteillage et lisent le flux simulé (Zerobus → Lakebase). Il faut le redessiner sur le flux
  GNK (lignes ARMP + files d'attente) et l'alimenter depuis la couche gold, en rejouant
  `metal_movements` au lieu du simulateur. Les métadonnées sont dans `client/src/lib/machineMeta.ts`.
- **Espace Genie « line ops » GNK** : `scripts/genie_space_config_streaming.py` décrit encore les
  tables d'embouteillage. Il faut une version sur les tables `gnk_*` / `gold_gnk_*`.
- **Documents usine (RAG)** : `data/plant_docs` contient des SOP d'embouteillage. Il faut les
  remplacer par les fiches de ligne, consignes et retours d'expérience Genk.
- **Sources supplémentaires** à connecter pour aller au-delà de l'OEE proxy :
  - causes d'arrêt et journal maintenance (arrêts planifiés ou non) ;
  - plan de production et carnet de commandes (rattrapage, respect des délais) ;
  - résultats qualité (défauts, déclassements, réclamations).
- **Optimiseurs** (MILP de rattrapage, Monte-Carlo, RUL, causal qualité) : ils sont désactivés en
  mode GNK car ils lisent les tables d'embouteillage. Ils sont à recâbler une fois les sources
  ci-dessus disponibles.
- **Évaluations** : le jeu `evaluations/` porte encore sur des scénarios d'embouteillage. Il faut
  écrire des scénarios GNK, à partir des incidents du générateur synthétique.
