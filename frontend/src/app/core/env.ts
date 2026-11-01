/**
 * Configuration injectée au démarrage du conteneur (`assets/env.js`, écrit par
 * `nginx-entrypoint.sh`).
 */
export interface LabEnv {
  keycloakUrl?: string;
  keycloakRealm?: string;
  keycloakClientId?: string;
  appUrl?: string;
  apiUrl?: string;
}

export function labEnv(): LabEnv {
  return (window as unknown as { __env?: LabEnv }).__env ?? {};
}

/** Base de l'API de carto-lab (`…/carto-lab-api`). */
export function apiUrl(): string {
  return labEnv().apiUrl ?? 'http://localhost:8091';
}

/**
 * Les SEULES adresses auxquelles le jeton de l'utilisateur est joint
 * (`auth.interceptor.ts`, `token-scope.ts`) : l'API de l'app, rien d'autre —
 * cette interface n'appelle aucun autre service avec le jeton (les tuiles OSM
 * sont chargées par OpenLayers, hors `HttpClient`, donc sans jeton).
 */
export function tokenBases(): string[] {
  return [apiUrl()];
}
