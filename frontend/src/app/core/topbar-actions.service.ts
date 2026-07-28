import { Injectable, signal } from '@angular/core';

/**
 * Bouton d'action propre à la page active, affiché dans la topbar mobile à
 * côté du burger de navigation. `icon`/`label`/`expanded` sont des fonctions
 * (pas des valeurs figées) pour rester réactives à l'état de la page qui les
 * fournit — le template de la topbar les invoque directement.
 */
export interface TopbarAction {
  label: () => string;
  icon: () => string;
  expanded: () => boolean;
  onClick: () => void;
}

@Injectable({ providedIn: 'root' })
export class TopbarActionsService {
  readonly action = signal<TopbarAction | null>(null);

  set(action: TopbarAction): void {
    this.action.set(action);
  }

  clear(): void {
    this.action.set(null);
  }
}
