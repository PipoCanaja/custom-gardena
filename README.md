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

La branche `main` (v0.0.1) est une **copie conforme** de l'intégration du cœur : seule la
clé `"version"` est ajoutée au `manifest.json`, obligatoire pour une intégration custom.

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
/config/scripts/gardena-update.sh install            # branche main
/config/scripts/gardena-update.sh install --pr 1     # une pull request
/config/scripts/gardena-update.sh install --branch phase-1-diagnostics
/config/scripts/gardena-update.sh install --tag v0.0.1
/config/scripts/gardena-update.sh install --ref 4ced203    # un commit précis
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

| Phase | Contenu | Écrit sur l'appareil |
| --- | --- | --- |
| 0 — `main`, v0.0.1 | Copie conforme de 2026.11.2 | non |
| `observe` — PR #3 | Code officiel + journal des connexions (voir plus bas) | non |
| 1 — `phase-1-diagnostics`, PR #1 | Diagnostic : table GATT, lectures brutes, décodage des programmations classiques | non |
| 2 — `phase-2-schedule-write`, PR #4 | Actions de debug lecture/écriture brute ; traductions en/fr | oui |
| 3 — `phase-3-classic-schedules`, PR #5 | Entités et actions de programmation du classique, corrections (voir ci-dessous) ; les actions de debug en écriture sont retirées | oui |
| 4 | Tests automatiques, préparation de la PR vers le cœur | — |

Chaque branche part de la précédente : `observe` → 1 → 2 → 3. Toutes les branches de
phase contiennent donc aussi le journal des connexions de `observe`.

## Phase 3 : Water Computer classique

Tout ce qui suit n'apparaît que sur un Water Computer **classique** (type
`WATER_COMPUTER`, service vanne `98bd0f10`, pas de service hybride `98bda0xx`).
Les autres produits gardent exactement les entités officielles.

Entités, par emplacement N (1 à 3) :

| Entité | Rôle |
| --- | --- |
| `switch` Programmation N | active/désactive ; attributs : heure, durée, jours, capteur |
| `time` Programmation N : heure de début | modifiable |
| `number` Programmation N : durée | secondes, modifiable |

Entités de l'appareil :

| Entité | Rôle |
| --- | --- |
| `number` Réduction de la durée d'arrosage | 0–100 %, comme l'app ; remplace « Ajustement saisonnier » (affiché à tort en jours). **100 % = toutes les programmations à 0 s** |
| `sensor` Prochain arrosage | horodatage, attributs : emplacement, durée, réduction |
| `sensor` Dernier arrosage | fin du dernier arrosage ; attributs : début, durée, résultat (arrosé / partiellement / sauté), raison, 10 dernières entrées |
| `sensor` Raison d'activation | corrigée : `none` vanne fermée, `manual` si 0, `schedule` sinon (comme l'app) |

Actions : `gardena_bluetooth.set_schedule` (champs omis = inchangés) et
`gardena_bluetooth.clear_schedule`. Les jours ne se règlent que par l'action. Chaque
écriture, par action ou par entité, suit la séquence de l'app : désactivation, écriture
des champs, réactivation, puis relecture ; une relecture différente lève une erreur. Si
une écriture échoue en cours de route, l'emplacement reste désactivé.

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
- L'effet d'une réduction intermédiaire (par exemple 30 %) sur la durée réelle n'a pas
  été mesuré ; seuls 0 % et 100 % ont été vérifiés sur l'appareil.
- L'historique est remis à zéro par un retrait des piles.

Diagnostic : le téléchargement des diagnostics de l'entrée ajoute une section
`classic` (emplacements, programmations décodées, réduction, historique) à la table
GATT et aux lectures brutes. L'action `gardena_bluetooth.debug_read_schedule` relit un
emplacement directement sur l'appareil.

Tests du protocole, sans Home Assistant :

```sh
python3 tests/test_classic_protocol.py
```

## Branche `observe` (debug, à ne pas fusionner)

Code officiel 2026.11.2 inchangé, plus `observe.py` qui ne fait qu'enregistrer. Ce
journal reste actif dans toutes les branches de phase.
Chaque tentative de connexion, connexion réussie, déconnexion et déconnexion
inattendue produit une ligne JSON dans `/config/gardena_observe.jsonl` (rotation
à 5 Mo) et une ligne INFO dans le journal de HA
(`custom_components.gardena_bluetooth.observe`).

Lecture depuis le shell HA :

```sh
tail -n 20 /config/gardena_observe.jsonl
grep -c connect_fail /config/gardena_observe.jsonl
```
