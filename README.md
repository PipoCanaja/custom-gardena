# custom-gardena

Version de développement de l'intégration Home Assistant `gardena_bluetooth`, chargée
en `custom_components`, pour ajouter la **programmation horaire du Water Computer
classique**. Une fois validé, le travail pourra être proposé en PR sur l'intégration
officielle.

## Base

| Élément | Version |
| --- | --- |
| Home Assistant cible | 2026.11.2 (HA OS) |
| Source copiée | `home-assistant/core`, tag `2026.11.2`, `homeassistant/components/gardena_bluetooth` |
| Bibliothèque | `gardena-bluetooth==2.10.1`, identique au cœur (ne pas changer) |

La branche `main` (v0.0.1) est une **copie conforme** de l'intégration du cœur : seule la
clé `"version"` est ajoutée au `manifest.json`, obligatoire pour une intégration custom.

Règles du projet :

- Seul le **Water Computer classique** est concerné. Tout ajout est conditionné à la
  présence des services `98bd0f10` (vanne) et `98bd0c10` (programmation) sur l'appareil,
  jamais au seul type de produit : `WATER_COMPUTER` couvre aussi les modèles hybrides.
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
| 1 | Diagnostic : table GATT, lectures brutes, décodage des programmations classiques | non |
| 2 | Actions de debug lecture/écriture brute, essais d'écriture | oui |
| 3 | Fonction définitive : lecture des programmations, actions `set_schedule` / `delete_schedule` | oui |
| 4 | Tests automatiques, préparation de la PR vers le cœur | — |
