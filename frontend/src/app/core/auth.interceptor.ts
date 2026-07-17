import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { from, switchMap, catchError } from 'rxjs';
import { KeycloakService } from './keycloak.service';

export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const kc = inject(KeycloakService);
  const token = kc.getToken();

  // Chemin synchrone : token présent et valide — aucune attente async
  if (token && !kc.isTokenExpired(30)) {
    return next(req.clone({ setHeaders: { Authorization: `Bearer ${token}` } }));
  }

  // Chemin async : token expiré ou absent — tentative de refresh.
  // Si le refresh échoue, kc.getToken() renvoie encore l'ANCIEN token expiré
  // (keycloak-js ne le vide pas) : ne jamais le rattacher dans ce cas, sinon
  // la requête part avec un Bearer qu'on sait déjà invalide.
  return from(kc.updateToken(-1)).pipe(
    switchMap(() => {
      const fresh = kc.getToken();
      if (fresh && !kc.isTokenExpired(0)) {
        req = req.clone({ setHeaders: { Authorization: `Bearer ${fresh}` } });
      }
      return next(req);
    }),
    catchError(() => next(req)),
  );
};
