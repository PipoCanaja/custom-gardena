#!/usr/bin/env bash
# gardena-update.sh — installe / met à jour custom_components/gardena_bluetooth
# depuis le dépôt Git, à exécuter dans le shell de Home Assistant OS
# (module complémentaire « Terminal & SSH » ou « Advanced SSH & Web Terminal »).
#
#   gardena-update.sh status
#   gardena-update.sh list
#   gardena-update.sh install [--branch NOM | --pr N | --tag NOM | --ref SHA] [--restart] [--yes]
#   gardena-update.sh rollback  [--restart] [--yes]
#   gardena-update.sh uninstall [--restart] [--yes]
#
# Sans option de source, « install » prend la branche main.
#
# Variables d'environnement facultatives :
#   REPO_URL    dépôt Git (défaut : https://github.com/PipoCanaja/custom-gardena.git)
#   CONFIG_DIR  dossier de configuration HA (défaut : /config)
#   KEEP_BACKUPS nombre de sauvegardes conservées (défaut : 5)

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/PipoCanaja/custom-gardena.git}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
DOMAIN="gardena_bluetooth"

TARGET="$CONFIG_DIR/custom_components/$DOMAIN"
STATE="$CONFIG_DIR/.custom-gardena"
SRC="$STATE/src"
BACKUPS="$STATE/backups"
INSTALLED="$STATE/installed"

RESTART=0
ASSUME_YES=0
LAST_BACKUP=""

log()  { printf '%s\n' "$*"; }
warn() { printf 'ATTENTION : %s\n' "$*" >&2; }
die()  { printf 'ERREUR : %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  printf '%s [o/N] ' "$1"
  read -r answer || answer=""
  case "$answer" in o|O|oui|y|Y|yes) return 0 ;; *) return 1 ;; esac
}

require_git() {
  command -v git >/dev/null 2>&1 && return 0
  die "git est introuvable. Dans « Terminal & SSH » : apk add git (à refaire après un redémarrage du module), ou utiliser « Advanced SSH & Web Terminal »."
}

manifest_value() {
  # manifest_value FICHIER CLE -> valeur d'une clé texte de premier niveau
  sed -n "s/^[[:space:]]*\"$2\":[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" | head -n 1
}

# --- Récupération d'une révision --------------------------------------------

fetch_ref() {
  # fetch_ref REFSPEC LIBELLE : récupère la révision dans $SRC (clone superficiel)
  local refspec="$1" label="$2"
  require_git
  mkdir -p "$STATE"
  if [ ! -d "$SRC/.git" ]; then
    git init -q "$SRC"
    git -C "$SRC" remote add origin "$REPO_URL"
  else
    git -C "$SRC" remote set-url origin "$REPO_URL"
  fi
  log "Récupération de $label depuis $REPO_URL ..."
  git -C "$SRC" fetch -q --depth 1 origin "$refspec" \
    || die "impossible de récupérer $label (nom, numéro de PR ou réseau ?)"
  git -C "$SRC" checkout -q --force FETCH_HEAD
  git -C "$SRC" clean -q -fdx
}

validate_source() {
  local dir="$SRC/custom_components/$DOMAIN" manifest
  manifest="$dir/manifest.json"
  [ -f "$manifest" ] || die "$manifest absent : cette révision ne contient pas l'intégration."
  [ "$(manifest_value "$manifest" domain)" = "$DOMAIN" ] \
    || die "le manifest ne déclare pas le domaine $DOMAIN."
  [ -n "$(manifest_value "$manifest" version)" ] \
    || die "le manifest n'a pas de clé \"version\" (obligatoire pour une intégration custom)."
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$dir" <<'PY' || die "erreur de syntaxe Python dans la révision, installation annulée."
import ast, pathlib, sys
for path in pathlib.Path(sys.argv[1]).rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), str(path))
PY
  fi
}

# --- Sauvegardes ------------------------------------------------------------

backup_current() {
  LAST_BACKUP=""
  [ -d "$TARGET" ] || return 0
  local base dest n=0
  base="$BACKUPS/$(date +%Y%m%d-%H%M%S)"
  dest="$base-00"
  while [ -e "$dest" ] || [ -e "$dest.installed" ]; do
    n=$((n + 1))
    dest="$base-$(printf %02d "$n")"
  done
  mkdir -p "$BACKUPS"
  cp -a "$TARGET" "$dest"
  [ -f "$INSTALLED" ] && cp "$INSTALLED" "$dest.installed"
  LAST_BACKUP="$dest"
  log "Sauvegarde de la version en place : $dest"
  # ne garder que les KEEP_BACKUPS plus récentes
  ls -1d "$BACKUPS"/*/ 2>/dev/null | LC_ALL=C sort -r | tail -n +"$((KEEP_BACKUPS + 1))" | while read -r old; do
    old="${old%/}"
    rm -rf "$old" "$old.installed"
  done
}

replace_target_with() {
  # replace_target_with DOSSIER_SOURCE : remplacement en deux temps
  local source="$1" staging="$STATE/staging"
  rm -rf "$staging"
  cp -a "$source" "$staging"
  find "$staging" -name '__pycache__' -type d -prune -exec rm -rf {} +
  mkdir -p "$CONFIG_DIR/custom_components"
  rm -rf "$TARGET"
  mv "$staging" "$TARGET"
}

show_changes() {
  local before="$1"
  [ -n "$before" ] && [ -d "$before" ] || { log "Première installation."; return 0; }
  log "Fichiers modifiés par rapport à la version précédente :"
  diff -rq -x '__pycache__' "$before" "$TARGET" | sed 's/^/  /' || true
}

# --- Redémarrage ------------------------------------------------------------

maybe_restart() {
  if ! command -v ha >/dev/null 2>&1; then
    log "Commande « ha » indisponible : redémarrer Home Assistant depuis l'interface (Paramètres › Système › Redémarrer)."
    return 0
  fi
  if [ "$RESTART" != 1 ]; then
    confirm "Vérifier la configuration et redémarrer Home Assistant maintenant ?" || {
      log "Pas de redémarrage. La nouvelle version sera chargée au prochain redémarrage de HA."
      return 0
    }
  fi
  log "Vérification de la configuration ..."
  ha core check || die "la vérification a échoué : pas de redémarrage. Utiliser « $0 rollback » si besoin."
  log "Redémarrage de Home Assistant ..."
  ha core restart
}

# --- Commandes --------------------------------------------------------------

cmd_status() {
  if [ -d "$TARGET" ]; then
    log "Installé : $TARGET"
    log "  version du manifest : $(manifest_value "$TARGET/manifest.json" version)"
    if [ -f "$INSTALLED" ]; then sed 's/^/  /' "$INSTALLED"; fi
  else
    log "Pas de custom_component $DOMAIN : c'est l'intégration du cœur de HA qui est utilisée."
  fi
  if [ -d "$BACKUPS" ] && ls -1d "$BACKUPS"/*/ >/dev/null 2>&1; then
    log "Sauvegardes disponibles :"
    ls -1d "$BACKUPS"/*/ | LC_ALL=C sort -r | sed "s|$BACKUPS/||; s|/$||; s/^/  /"
  fi
}

cmd_list() {
  require_git
  log "Branches :"
  git ls-remote --heads "$REPO_URL" | sed 's|.*refs/heads/|  |'
  local owner_repo
  owner_repo="$(printf '%s' "$REPO_URL" | sed -n 's|.*github\.com[:/]\([^/]*/[^/.]*\).*|\1|p')"
  if [ -n "$owner_repo" ] && command -v curl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    log "Pull requests ouvertes :"
    curl -fsSL "https://api.github.com/repos/$owner_repo/pulls?state=open" \
      | jq -r '.[] | "  #\(.number)  \(.head.ref)  —  \(.title)"' || warn "API GitHub injoignable."
  else
    log "Pull requests (toutes, y compris fermées) :"
    git ls-remote "$REPO_URL" 'refs/pull/*/head' | sed 's|.*refs/pull/\([0-9]*\)/head|  #\1|'
  fi
}

cmd_install() {
  local refspec="refs/heads/main" label="la branche main"
  while [ $# -gt 0 ]; do
    case "$1" in
      --branch|-b) [ $# -ge 2 ] || die "--branch attend un nom"; refspec="refs/heads/$2"; label="la branche $2"; shift 2 ;;
      --pr|-p)     [ $# -ge 2 ] || die "--pr attend un numéro"
                   case "$2" in *[!0-9]*|'') die "numéro de PR invalide : $2" ;; esac
                   refspec="refs/pull/$2/head"; label="la PR #$2"; shift 2 ;;
      --tag|-t)    [ $# -ge 2 ] || die "--tag attend un nom"; refspec="refs/tags/$2"; label="le tag $2"; shift 2 ;;
      --ref)       [ $# -ge 2 ] || die "--ref attend un SHA"; refspec="$2"; label="le commit $2"; shift 2 ;;
      --restart)   RESTART=1; shift ;;
      --yes|-y)    ASSUME_YES=1; shift ;;
      *) die "option inconnue : $1" ;;
    esac
  done

  fetch_ref "$refspec" "$label"
  validate_source

  local sha version subject
  sha="$(git -C "$SRC" rev-parse --short HEAD)"
  subject="$(git -C "$SRC" log -1 --format=%s)"
  version="$(manifest_value "$SRC/custom_components/$DOMAIN/manifest.json" version)"
  log "Révision : $sha  version $version"
  log "  $subject"
  confirm "Installer cette révision dans $TARGET ?" || die "installation annulée."

  backup_current
  replace_target_with "$SRC/custom_components/$DOMAIN"
  {
    printf 'source   : %s\n' "$label"
    printf 'commit   : %s\n' "$(git -C "$SRC" rev-parse HEAD)"
    printf 'version  : %s\n' "$version"
    printf 'installé : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  } > "$INSTALLED"
  log "Installé : $label ($sha, version $version)."
  show_changes "$LAST_BACKUP"
  maybe_restart
}

cmd_rollback() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --restart) RESTART=1; shift ;;
      --yes|-y)  ASSUME_YES=1; shift ;;
      *) die "option inconnue : $1" ;;
    esac
  done
  local latest
  latest="$(ls -1d "$BACKUPS"/*/ 2>/dev/null | LC_ALL=C sort -r | head -n 1 || true)"
  [ -n "$latest" ] || die "aucune sauvegarde disponible."
  latest="${latest%/}"
  log "Sauvegarde à restaurer : $(basename "$latest")"
  [ -f "$latest.installed" ] && sed 's/^/  /' "$latest.installed"
  confirm "Restaurer cette sauvegarde ?" || die "restauration annulée."
  local restore="$STATE/restore"
  rm -rf "$restore" "$restore.installed"
  mv "$latest" "$restore"
  if [ -f "$latest.installed" ]; then mv "$latest.installed" "$restore.installed"; fi
  backup_current
  replace_target_with "$restore"
  rm -rf "$restore"
  if [ -f "$restore.installed" ]; then mv "$restore.installed" "$INSTALLED"; else rm -f "$INSTALLED"; fi
  log "Restauration terminée."
  maybe_restart
}

cmd_uninstall() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --restart) RESTART=1; shift ;;
      --yes|-y)  ASSUME_YES=1; shift ;;
      *) die "option inconnue : $1" ;;
    esac
  done
  [ -d "$TARGET" ] || { log "Rien à désinstaller."; return 0; }
  confirm "Supprimer $TARGET et revenir à l'intégration du cœur ?" || die "désinstallation annulée."
  backup_current
  rm -rf "$TARGET" "$INSTALLED"
  log "custom_component supprimé (sauvegarde conservée). L'intégration du cœur sera utilisée après redémarrage."
  maybe_restart
}

main() {
  [ $# -ge 1 ] || usage 1
  local cmd="$1"; shift
  case "$cmd" in
    status)    cmd_status ;;
    list)      cmd_list ;;
    install)   cmd_install "$@" ;;
    rollback)  cmd_rollback "$@" ;;
    uninstall) cmd_uninstall "$@" ;;
    -h|--help|help) usage 0 ;;
    *) usage 1 ;;
  esac
}

main "$@"
