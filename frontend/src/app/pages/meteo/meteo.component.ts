import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { ApiService, Job, MeteoOptions, MeteoQualityThresholds, OpParam } from '../../core/api.service';

@Component({
  selector: 'app-meteo',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './meteo.component.html',
  styleUrl: './meteo.component.scss',
})
export class MeteoComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);

  // Signals : cette app tourne sans zone.js (zoneless). Un champ simple muté
  // depuis un callback HTTP/setInterval ne déclenche AUCUN rafraîchissement de
  // vue automatique — seul un signal (ou un événement natif Angular, ex.
  // (click)/[(ngModel)]) le fait. D'où le bug observé : les données arrivaient
  // bien, mais l'écran restait figé jusqu'au prochain clic ailleurs.
  options = signal<MeteoOptions | null>(null);
  optionsError = signal(false);
  jobs = signal<Job[]>([]);
  current = signal<Job | null>(null);
  err = signal('');

  // Identifiant applicatif Météo-France (chaîne Basic du portail, PAS un jeton
  // Bearer déjà émis — celui-ci expire en 1h). Conservé dans le navigateur
  // (localStorage) pour éviter à l'utilisateur de le ressaisir à chaque session.
  // Ne part jamais vers le backend hors du header X-Meteo-Key d'un lancement, et
  // n'est jamais persisté côté serveur (cf. secret_store.py — jeton Redis
  // éphémère ; l'échange contre un jeton Bearer se fait côté worker Celery, cf.
  // meteo_client.fetch_access_token).
  apiKey = localStorage.getItem('mf_key') ?? '';

  form = {
    grandeur: 'temperature',
    year: 2020,
    indicator: 'mean',
    classification: 'quantiles',
    n_classes: 5,
    ramp: 'YlOrRd',
    max_stations: 50 as number | null,
  };
  indicatorParams: Record<string, unknown> = {};

  // Seuils de qualité (laisser vide = pas de seuil). N'affectent que le passage
  // couche ponctuelle → choroplèthe : les stations exclues restent visibles en
  // points, avec leurs métriques de qualité (cf. tasks.py / meteo_pipeline.py).
  qualityForm: Required<MeteoQualityThresholds> = {
    min_completeness: null,
    max_gap_hours: null,
    max_same_datetime: null,
    max_duplicates: null,
  };

  private poll: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.loadOptions();
    this.reloadJobs();
  }
  ngOnDestroy(): void { this.stopPoll(); }

  /** Séparé de ngOnInit pour être rejouable depuis le bouton "Réessayer". */
  loadOptions(): void {
    this.optionsError.set(false);
    this.api.getMeteoOptions().subscribe({
      next: (o) => this.options.set(o),
      error: () => this.optionsError.set(true),
    });
  }

  reloadJobs(): void {
    this.api.getJobs().subscribe({ next: (j) => this.jobs.set(j), error: () => {} });
  }

  saveKey(): void { localStorage.setItem('mf_key', this.apiKey); }

  get indicatorParamDefs(): OpParam[] {
    return this.options()?.indicators.find((i) => i.name === this.form.indicator)?.params ?? [];
  }
  onIndicatorChange(): void {
    this.indicatorParams = {};
    for (const p of this.indicatorParamDefs) this.indicatorParams[p.name] = p.default ?? '';
  }

  launch(): void {
    this.err.set('');
    if (!this.apiKey) { this.err.set('Saisissez votre clé API Météo-France.'); return; }
    this.saveKey();
    this.api.launchMeteoJob(this.apiKey, {
      grandeur: this.form.grandeur,
      year: this.form.year,
      indicator: this.form.indicator,
      indicator_params: { ...this.indicatorParams },
      classification: this.form.classification,
      n_classes: this.form.n_classes,
      ramp: this.form.ramp,
      max_stations: this.form.max_stations,
      quality_thresholds: { ...this.qualityForm },
    }).subscribe({
      next: (job) => { this.current.set(job); this.reloadJobs(); this.startPoll(job.id); },
      error: (e) => { this.err.set(e?.error?.detail ?? 'Échec du lancement.'); },
    });
  }

  private startPoll(id: number): void {
    this.stopPoll();
    this.poll = setInterval(() => {
      this.api.getJob(id).subscribe((job) => {
        this.current.set(job);
        if (job.status === 'DONE' || job.status === 'ERROR') {
          this.stopPoll();
          this.reloadJobs();
        }
      });
    }, 2000);
  }
  private stopPoll(): void { if (this.poll) { clearInterval(this.poll); this.poll = null; } }

  trackJob(_: number, j: Job): number { return j.id; }
}
