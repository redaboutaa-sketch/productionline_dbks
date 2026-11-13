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
| Générateur de données synthétiques | Générateur au **schéma exact** de vos tables, avec 3 incidents injectés (panne MKW2, zone 7 four BUL3, point de rosée BAL1) | `src/aperam_gnk/synthetic.py` |

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
2. **Créer la couche GNK** dans le schéma de l'app (`uc_catalog.uc_schema`) :

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
9. **Aciérie** : `Metal` désigne-t-il la coulée ou la brame sur EAF/AOD/CCS ? Comment se fait le lien
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
