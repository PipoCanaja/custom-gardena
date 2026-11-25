# custom-gardena

Version de développement de l'intégration Home Assistant `gardena_bluetooth`, chargée
en `custom_components`, pour ajouter la **programmation horaire du Water Computer
classique**. Une fois validé, le travail pourra être proposé en PR sur l'intégration
officielle.

## Base

| Élément | Version |
| --- | --- |
| Home Assistant cible | 2026.11.2 (HA OS), testé aussi sur 2026.11.3 |
| Source copiée | `home-assistant/core`, tag `2026.11.2`, `homeassistant/components/gardena_bluetooth` |
| Bibliothèque | `gardena-bluetooth==2.10.1`, identique au cœur (ne pas changer) |

La branche `main` contient la version validée sur l'appareil (phases 1 à 3), publiée
sous le tag `v0.3.3`. La phase 0 était une **copie conforme** de l'intégration du cœur :
seule la clé `"version"` était ajoutée au `manifest.json`, obligatoire pour une
intégration custom.

Règles du projet :

- Seul le **Water Computer classique** est concerné. Tout ajout est conditionné à ce
  que l'appareil expose la vanne (`98bd0f11`) et la programmation 1 (`98bd0c11`), sans
  aucun service ni caractéristique hybride (`98bda0xx`, `98bda1xx`), et soit de type
  `WATER_COMPUTER` : ce type seul ne suffit pas, il couvre aussi le modèle hybride
  (`classic_protocol.is_classic`).
- Le code existant n'est modifié qu'en cas de nécessité ; les ajouts vivent dans des
  fichiers ou des blocs isolés.
- La version de `gardena-bluetooth` reste celle du cœur.
- Chaque étape arrive par une PR, installable et testable sur HA avant fusion.

## Installation dans Home Assistant OS

Prérequis : le module complémentaire **Advanced SSH & Web Terminal** (git, curl, jq et la
commande `ha` déjà présents) ou **Terminal & SSH** (installer git à chaque démarrage du
module : `apk add git`). Faire une sauvegarde HA avant la première installation.

Récupérer le script une seule fois :

```sh
mkdir -p /config/scripts
curl -fsSL https://raw.githubusercontent.com/PipoCanaja/custom-gardena/main/scripts/gardena-update.sh \
  -o /config/scripts/gardena-update.sh
chmod +x /config/scripts/gardena-update.sh
```

Puis :

```sh
/config/scripts/gardena-update.sh install            # branche main (version validée)
/config/scripts/gardena-update.sh install --pr N     # une pull request ouverte
/config/scripts/gardena-update.sh install --branch observe   # main + journal des connexions
/config/scripts/gardena-update.sh install --tag v0.3.3
/config/scripts/gardena-update.sh install --ref 007044b    # un commit précis
/config/scripts/gardena-update.sh list               # branches et PR disponibles
/config/scripts/gardena-update.sh status             # ce qui est installé, sauvegardes
/config/scripts/gardena-update.sh rollback           # revenir à la version précédente
/config/scripts/gardena-update.sh uninstall          # revenir à l'intégration du cœur
```

Ce que fait `install` :

1. récupère la révision demandée dans `/config/.custom-gardena/src` (clone superficiel) ;
2. vérifie le `manifest.json` (domaine, clé `version`) et, si `python3` est présent, la
   syntaxe des fichiers Python ;
3. sauvegarde la version en place dans `/config/.custom-gardena/backups/` (5 conservées) ;
4. remplace `/config/custom_components/gardena_bluetooth/` et liste les fichiers modifiés ;
5. propose `ha core check` puis `ha core restart` (`--restart` pour ne pas demander,
   `--yes` pour tout confirmer).

Au redémarrage, le journal affiche « We found a custom integration gardena_bluetooth which
has not been tested by Home Assistant » et signale le remplacement de l'intégration du
cœur : c'est attendu. L'entrée de configuration existante est reprise telle quelle, sans
nouvel appairage.

Le script se met à jour avec la même commande `curl` ; il n'est pas modifié par `install`.

## Journaux de debug, sans redémarrage

Outils de développement › Actions :

```yaml
action: logger.set_level
data:
  custom_components.gardena_bluetooth: debug
  gardena_bluetooth: debug
```

Revenir ensuite à `warning` avec la même action.

## Phases

| Phase | Contenu | Écrit sur l'appareil | État |
| --- | --- | --- | --- |
| 0 | Copie conforme de 2026.11.2 | non | dans l'historique de `main` |
| 1 | Diagnostic : table GATT, lectures brutes, décodage des programmations classiques | non | dans `main` |
| 2 | Actions de debug ; traductions en/fr | oui | dans `main` |
| 3 | Entités et actions de programmation du classique, corrections (voir ci-dessous) ; les actions de debug en écriture sont retirées | oui | dans `main`, tag `v0.3.3` |
| 4 | Correctifs de la bibliothèque `gardena-bluetooth`, puis tests et PR vers le cœur | — | à faire |

Le journal des connexions (`observe`) n'est pas dans `main` : il vit dans sa propre branche,
voir plus bas.

## Phase 3 : Water Computer classique

Tout ce qui suit n'apparaît que sur un Water Computer **classique**, détecté selon la
règle du début (`classic_protocol.is_classic`). Les autres produits gardent exactement
les entités officielles.

Entités, par emplacement N (1 à 3) :

| Entité | Rôle |
| --- | --- |
| `switch` Programmation N | active/désactive ; attributs : heure, durée, jours, capteur |
| `time` Programmation N : heure de début | modifiable |
| `number` Programmation N : durée | secondes, modifiable |
| `sensor` Programmation N : jours | lecture seule (« Lun, Mer, Ven », « Tous les jours », « Aucun ») ; attribut `weekdays` |

Entités de l'appareil :

| Entité | Rôle |
| --- | --- |
| `number` Réduction de la durée d'arrosage | 0–100 %, comme l'app ; remplace « Ajustement saisonnier » (affiché à tort en jours). **100 % = toutes les programmations à 0 s** |
| `sensor` Prochain arrosage | horodatage, attributs : emplacement, durée, durée effective, réduction |
| `sensor` Dernier arrosage | fin du dernier arrosage ; attributs : début, durée, résultat (arrosé / partiellement / sauté), raison, 10 dernières entrées |
| `sensor` Raison d'activation | corrigée : `none` vanne fermée, `manual` si 0, `schedule` sinon (comme l'app) |

Les jours ne se modifient que par l'action `set_schedule`.

Actions : `gardena_bluetooth.set_schedule` (champs omis = inchangés) et
`gardena_bluetooth.clear_schedule`. Chaque écriture, par action ou par entité, suit la
séquence de l'app : désactivation, écriture des champs, réactivation, puis relecture ;
une relecture différente lève une erreur. Si une écriture échoue en cours de route,
l'emplacement reste désactivé.

```yaml
action: gardena_bluetooth.set_schedule
data:
  slot: 1
  start_time: "06:30:00"
  duration: 600
  weekdays: [mon, wed, fri]
  active: true
```

Programmations et historique sont lus toutes les 30 min, juste après une
écriture, et au relevé qui suit une ouverture ou une fermeture de la vanne.

Limites connues :

- « Prochain arrosage » ne tient compte ni de la pause pluie ni du capteur. Avec une
  réduction de 100 %, il passe à « inconnu » ; l'heure prévue reste dans l'attribut
  `next_schedule_start`.
- La réduction s'applique à la durée de chaque programmation : durée effective =
  durée × (100 − réduction) / 100, arrondie à la seconde inférieure. Mesuré à 0 %,
  9 % (deux fois 120 s → 109 s) et 100 %. La durée notée dans l'historique varie de
  ±1 s (108 et 110 s pour ces deux arrosages).
- L'historique est remis à zéro par un retrait des piles.
- La vanne ne vérifie pas la pression : une programmation s'exécute aussi robinet
  fermé ou vanne débranchée (vérifié sans eau, vanne à plat, à l'intérieur).
- Le texte de « Programmation N : jours » est en français si HA est en français,
  sinon en anglais.

Diagnostic : le téléchargement des diagnostics de l'entrée ajoute une section
`classic` (emplacements, programmations décodées, réduction, historique) à la table
GATT et aux lectures brutes. L'action `gardena_bluetooth.debug_read_schedule` relit un
emplacement directement sur l'appareil.

Tests du protocole, sans Home Assistant :

```sh
python3 tests/test_classic_protocol.py
```

## Branche `observe` (dépannage, jamais fusionnée dans `main`)

`main` plus `observe.py`, qui ne fait qu'enregistrer, pour refaire du dépannage de
connexion si besoin. Chaque tentative de connexion, connexion réussie, déconnexion et déconnexion
inattendue produit une ligne JSON dans `/config/gardena_observe.jsonl` (rotation
à 5 Mo) et une ligne INFO dans le journal de HA
(`custom_components.gardena_bluetooth.observe`).

Installation : `gardena-update.sh install --branch observe`, puis retour à `main` avec
`gardena-update.sh install`. Lecture depuis le shell HA :

```sh
tail -n 20 /config/gardena_observe.jsonl
grep -c connect_fail /config/gardena_observe.jsonl
```
