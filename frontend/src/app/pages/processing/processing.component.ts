import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import {
  ApiService, LayerMeta, Operation, Recipe, RecipeStep,
} from '../../core/api.service';

interface InputOption { value: string; label: string; }

@Component({
  selector: 'app-processing',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './processing.component.html',
  styleUrl: './processing.component.scss',
})
export class ProcessingComponent implements OnInit {
  private api = inject(ApiService);

  // Signals : app zoneless (pas de zone.js) — un champ simple muté depuis un
  // callback subscribe() ne déclenche aucun rafraîchissement de vue tant
  // qu'aucun événement natif Angular n'a lieu ailleurs. Les champs liés en
  // deux-voies via [(ngModel)]/(click) restent des champs simples : ces
  // événements natifs déclenchent la détection de changement même sans signal.
  operations = signal<Operation[]>([]);
  layers = signal<LayerMeta[]>([]);
  recipes = signal<Recipe[]>([]);
  running = signal(false);
  msg = signal('');
  err = signal('');
  draft = signal<RecipeStep[]>([]);

  // Sélection d'opération partagée (traitement simple + ajout à la recette)
  op: Operation | null = null;
  inputSlots: string[] = [];               // 'L<id>' (couche) ou 'S<idx>' (étape)
  params: Record<string, unknown> = {};

  // Recette en construction
  recipeName = '';

  ngOnInit(): void {
    this.api.getProcessings().subscribe((o) => this.operations.set(o));
    this.reloadLayers();
    this.reloadRecipes();
  }

  reloadLayers(): void {
    this.api.getLayers().subscribe((l) => this.layers.set(l));
  }
  reloadRecipes(): void {
    this.api.getRecipes().subscribe((r) => this.recipes.set(r));
  }

  onOpChange(): void {
    if (!this.op) return;
    this.inputSlots = Array.from({ length: this.op.inputs }, () => '');
    this.params = {};
    for (const p of this.op.params) this.params[p.name] = p.default ?? '';
  }

  /** Options d'entrée : couches existantes + étapes déjà dans la recette. */
  inputOptions(): InputOption[] {
    const opts: InputOption[] = this.layers().map((l) => ({
      value: 'L' + l.id, label: `${l.name} (${l.geom_type || l.layer_type})`,
    }));
    this.draft().forEach((s, i) => opts.push({
      value: 'S' + i, label: `↳ Étape ${i + 1} : ${this.opLabel(s.op)}`,
    }));
    return opts;
  }

  opLabel(name: string): string {
    return this.operations().find((o) => o.name === name)?.label ?? name;
  }

  private buildRefs(): RecipeStep['inputs'] {
    return this.inputSlots.map((v) =>
      v.startsWith('L') ? { layer: +v.slice(1) } : { step: +v.slice(1) });
  }

  // ── Traitement simple ──────────────────────────────────────────────────────
  runSingle(): void {
    if (!this.op) return;
    if (this.inputSlots.some((v) => !v)) { this.err.set('Sélectionnez toutes les couches d’entrée.'); return; }
    if (this.inputSlots.some((v) => v.startsWith('S'))) {
      this.err.set('Une entrée référence une étape : utilisez « Ajouter à la recette ».'); return;
    }
    this.running.set(true); this.msg.set(''); this.err.set('');
    const inputs = this.inputSlots.map((v) => +v.slice(1));
    this.api.runProcessing(this.op.name, inputs, { ...this.params }).subscribe({
      next: (layer) => {
        this.running.set(false);
        this.msg.set(`Couche « ${layer.name} » créée (${layer.feature_count} entités).`);
        this.reloadLayers();
      },
      error: (e) => { this.running.set(false); this.err.set(e?.error?.detail ?? 'Échec du traitement.'); },
    });
  }

  // ── Constructeur de recette ────────────────────────────────────────────────
  addStep(): void {
    if (!this.op) return;
    if (this.inputSlots.some((v) => !v)) { this.err.set('Sélectionnez toutes les entrées de l’étape.'); return; }
    this.err.set('');
    const step: RecipeStep = { op: this.op.name, params: { ...this.params }, inputs: this.buildRefs() };
    this.draft.update((d) => [...d, step]);
  }

  removeStep(i: number): void {
    // Retirer une étape invaliderait les références S>i ; on tronque à partir de i.
    this.draft.update((d) => d.slice(0, i));
  }

  stepSummary(s: RecipeStep): string {
    const ins = s.inputs.map((r) => 'layer' in r ? `couche ${r.layer}` : `étape ${r.step + 1}`).join(', ');
    const ps = Object.entries(s.params ?? {}).map(([k, v]) => `${k}=${v}`).join(', ');
    return `${this.opLabel(s.op)} [${ins}]${ps ? ' — ' + ps : ''}`;
  }

  saveRecipe(run: boolean): void {
    const steps = this.draft();
    if (!steps.length) { this.err.set('Ajoutez au moins une étape.'); return; }
    if (!this.recipeName) { this.err.set('Nommez la recette.'); return; }
    this.running.set(true); this.msg.set(''); this.err.set('');
    this.api.createRecipe(this.recipeName, steps).subscribe({
      next: (rec) => {
        if (run) {
          this.api.runRecipe(rec.id).subscribe({
            next: (layer) => {
              this.running.set(false);
              this.msg.set(`Recette exécutée → « ${layer.name} » (${layer.feature_count} entités).`);
              this.draft.set([]); this.recipeName = '';
              this.reloadLayers(); this.reloadRecipes();
            },
            error: (e) => { this.running.set(false); this.err.set(e?.error?.detail ?? 'Échec de l’exécution.'); this.reloadRecipes(); },
          });
        } else {
          this.running.set(false);
          this.msg.set(`Recette « ${rec.name} » enregistrée.`);
          this.draft.set([]); this.recipeName = '';
          this.reloadRecipes();
        }
      },
      error: (e) => { this.running.set(false); this.err.set(e?.error?.detail ?? 'Échec de l’enregistrement.'); },
    });
  }

  replay(r: Recipe): void {
    this.running.set(true); this.msg.set(''); this.err.set('');
    this.api.runRecipe(r.id).subscribe({
      next: (layer) => {
        this.running.set(false);
        this.msg.set(`« ${r.name} » rejouée → « ${layer.name} » (${layer.feature_count} entités).`);
        this.reloadLayers();
      },
      error: (e) => { this.running.set(false); this.err.set(e?.error?.detail ?? 'Échec.'); },
    });
  }

  deleteRecipe(r: Recipe): void {
    if (!confirm(`Supprimer la recette « ${r.name} » ?`)) return;
    this.api.deleteRecipe(r.id).subscribe(() => this.reloadRecipes());
  }
}
