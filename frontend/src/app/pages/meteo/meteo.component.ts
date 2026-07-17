import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { NavbarComponent } from '../../shared/navbar/navbar.component';
import { ApiService, Job, MeteoOptions, OpParam } from '../../core/api.service';

@Component({
  selector: 'app-meteo',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, NavbarComponent],
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

  // La clé n'est conservée que le temps de la session onglet (sessionStorage).
  apiKey = sessionStorage.getItem('mf_key') ?? '';

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

  saveKey(): void { sessionStorage.setItem('mf_key', this.apiKey); }

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
