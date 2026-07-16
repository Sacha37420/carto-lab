#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-ogc-auth.sh
#
# Crée le client Keycloak "carto-ogc" pour l'oauth2-proxy placé devant
# pg_featureserv (accès QGIS), et renseigne les secrets dans carto-lab/.env.
#
# Modèle : sso-lab/setup-code-server-auth.sh. Comme code-server, ce service est
# protégé EN AMONT par oauth2-proxy (OAUTH2_PROXY_ALLOWED_GROUPS) et n'utilise
# donc pas de flow require-<client> (cf. CLAUDE.md — l'exception documentée).
#
# Prérequis : Keycloak démarré, jq installé.
# Usage :
#   bash carto-lab/setup-ogc-auth.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}→${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
die()     { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${SCRIPT_DIR}/.env"
SSO_ENV="${ROOT_DIR}/sso-lab/.env"
CREATE_CLIENT="${ROOT_DIR}/scripts/create-app-client.sh"
CLIENT_ID="carto-ogc"

[[ -f "$CREATE_CLIENT" ]] || die "create-app-client.sh introuvable dans $ROOT_DIR/scripts"
[[ -f "$ENV_FILE" ]]      || die ".env introuvable dans $SCRIPT_DIR"
command -v jq >/dev/null  || die "'jq' est requis (sudo apt-get install -y jq)"

OGC_PATH=$(grep -E '^OGC_SERVICE_PATH=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]')
OGC_PATH="${OGC_PATH:-carto-ogc}"

upsert_env() {  # $1=clé $2=valeur
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}

# ── Cookie secret (oauth2-proxy exige 16, 24 ou 32 octets) ────────────────────
CURRENT_COOKIE=$(grep "^CARTO_OGC_COOKIE_SECRET=" "$ENV_FILE" | cut -d= -f2- || true)
if [[ -z "$CURRENT_COOKIE" || "$CURRENT_COOKIE" == "CHANGE_ME" ]]; then
  COOKIE_SECRET=$(openssl rand -base64 32 | tr -d '\n=')
  upsert_env CARTO_OGC_COOKIE_SECRET "${COOKIE_SECRET:0:32}"
  success "CARTO_OGC_COOKIE_SECRET généré"
else
  info "CARTO_OGC_COOKIE_SECRET déjà renseigné — conservé"
fi

# ── Client Keycloak ───────────────────────────────────────────────────────────
info "Création/mise à jour du client Keycloak '${CLIENT_ID}'..."

TMP_NAME="_carto-ogc-setup"
TMP_APP="${ROOT_DIR}/${TMP_NAME}"
mkdir -p "$TMP_APP"
echo "KEYCLOAK_CLIENT_SECRET=" > "${TMP_APP}/.env"
trap 'rm -rf "$TMP_APP"' EXIT

# --no-rotate : le secret ne doit pas changer à chaque run (le container
# oauth2-proxy fige Config.Env à sa création → sinon unauthorized_client).
# --caddy-path : ajoute la redirect URI HTTPS https://<DOMAIN>/<path>/oauth2/callback
bash "$CREATE_CLIENT" "$TMP_NAME" \
  --client-id "$CLIENT_ID" \
  --redirect-path /oauth2/callback \
  --caddy-path "$OGC_PATH" \
  --no-rotate

CLIENT_SECRET=$(grep "^KEYCLOAK_CLIENT_SECRET=" "${TMP_APP}/.env" | cut -d= -f2-)
[[ -n "$CLIENT_SECRET" ]] || die "Secret vide — vérifiez la sortie de create-app-client.sh ci-dessus"
upsert_env CARTO_OGC_CLIENT_SECRET "$CLIENT_SECRET"
success "CARTO_OGC_CLIENT_SECRET mis à jour dans carto-lab/.env"

# ── Audience mapper (aud: carto-ogc dans l'access token) ─────────────────────
# Sans ce mapper, oauth2-proxy rejette le token (aud=[account] ≠ carto-ogc) —
# et le Bearer token de QGIS serait refusé.
info "Ajout du mapper d'audience Keycloak (idempotent)..."

KC_PORT=$(grep -E '^PORT_KEYCLOAK=' "$SSO_ENV" | cut -d= -f2 | tr -d '[:space:]' || echo "8080")
KC_ADMIN_USER=$(grep -E '^KEYCLOAK_ADMIN=' "$SSO_ENV" | cut -d= -f2 | tr -d '[:space:]' || echo "admin")
KC_ADMIN_PASS=$(grep -E '^KEYCLOAK_ADMIN_PASSWORD=' "$SSO_ENV" | cut -d= -f2 | tr -d '[:space:]')

# Même logique que create-app-client.sh : depuis un container (code-server), Keycloak
# se joint par son nom de service sur sso-net ; depuis l'hôte, par localhost.
if [ -f /.dockerenv ]; then
  KC_URL="http://keycloak:${KC_PORT}"
else
  KC_URL="http://localhost:${KC_PORT}"
fi

# Sans `|| true`, un échec de curl ferait sortir le script via pipefail SANS message.
KC_TOKEN=$(curl -sf -X POST "${KC_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&grant_type=password&username=${KC_ADMIN_USER}&password=${KC_ADMIN_PASS}" \
  | jq -r '.access_token // empty' || true)
[[ -n "$KC_TOKEN" && "$KC_TOKEN" != "null" ]] || die "Impossible d'obtenir un token admin Keycloak sur ${KC_URL}"

CLIENT_UUID=$(curl -s -H "Authorization: Bearer $KC_TOKEN" \
  "${KC_URL}/admin/realms/ssolab/clients?clientId=${CLIENT_ID}" | jq -r '.[0].id')
[[ -n "$CLIENT_UUID" && "$CLIENT_UUID" != "null" ]] || die "Client ${CLIENT_ID} introuvable dans ssolab"

EXISTING=$(curl -s -H "Authorization: Bearer $KC_TOKEN" \
  "${KC_URL}/admin/realms/ssolab/clients/${CLIENT_UUID}/protocol-mappers/models" \
  | jq -r --arg n "audience-${CLIENT_ID}" '.[] | select(.name == $n) | .name')

if [[ -z "$EXISTING" ]]; then
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${KC_URL}/admin/realms/ssolab/clients/${CLIENT_UUID}/protocol-mappers/models" \
    -H "Authorization: Bearer $KC_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
      \"name\": \"audience-${CLIENT_ID}\",
      \"protocol\": \"openid-connect\",
      \"protocolMapper\": \"oidc-audience-mapper\",
      \"config\": {
        \"included.client.audience\": \"${CLIENT_ID}\",
        \"id.token.claim\": \"false\",
        \"access.token.claim\": \"true\"
      }
    }")
  [[ "$HTTP" == "201" ]] && success "Audience mapper créé" || die "Échec création mapper (HTTP $HTTP)"
else
  info "Audience mapper déjà présent — conservé"
fi

# ── Application du secret à oauth2-proxy ─────────────────────────────────────
# Docker fige Config.Env à la création : --force-recreate est indispensable.
info "Recréation d'oauth2-proxy-ogc pour appliquer le secret..."
docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d --no-deps --force-recreate oauth2-proxy-ogc \
  || warn "Recréation d'oauth2-proxy-ogc impossible (la stack n'est peut-être pas démarrée)"

echo ""
echo "─────────────────────────────────────────────"
success "Terminé — service OGC protégé par oauth2-proxy (groupes : ${KEYCLOAK_REQUIRED_GROUPS:-developers})."
echo "  URL du service : https://<DOMAIN>/${OGC_PATH}"
echo "─────────────────────────────────────────────"
