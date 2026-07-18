import {
  AfterViewInit, Component, ElementRef, OnDestroy, ViewChild, inject, signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import OLMap from 'ol/Map';
import View from 'ol/View';
import TileLayer from 'ol/layer/Tile';
import OSM from 'ol/source/OSM';
import VectorLayer from 'ol/layer/Vector';
import VectorSource from 'ol/source/Vector';
import GeoJSON from 'ol/format/GeoJSON';
import Overlay from 'ol/Overlay';
import { transform } from 'ol/proj';
import { Fill, Stroke, Style, Circle as CircleStyle } from 'ol/style';
import type { Coordinate } from 'ol/coordinate';
import type Feature from 'ol/Feature';
import type { FeatureLike } from 'ol/Feature';
import type { StyleFunction } from 'ol/style/Style';

import { ApiService, CrsInfo, LayerMeta, PublishInfo } from '../../core/api.service';
import { DISPLAY_CRS, registerProjections } from '../../core/projections';

interface LegendEntry { min: number; max: number; color: string; }

interface ActiveLayer {
  olLayer: VectorLayer;
  geojson: unknown; // GeoJSON en EPSG:4326 (mis en cache pour re-projeter à la volée)
  meta: LayerMeta;
  legend?: { title: string; entries: LegendEntry[] };
}

const PALETTE = ['#2563eb', '#dc2626', '#16a34a', '#d97706', '#7c3aed', '#0891b2'];

@Component({
  selector: 'app-map',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './map.component.html',
  styleUrl: './map.component.scss',
})
export class MapComponent implements AfterViewInit, OnDestroy {
  private api = inject(ApiService);

  @ViewChild('mapEl', { static: true }) mapEl!: ElementRef<HTMLDivElement>;
  @ViewChild('popupEl', { static: true }) popupEl!: ElementRef<HTMLDivElement>;

  private map!: OLMap;
  private popup!: Overlay;
  private active = new Map<number, ActiveLayer>();
  // Incrémenté à chaque mutation de `active` (Map non-signal) : lu par
  // isActive()/hasActive()/activeLegends() pour forcer leur ré-évaluation
  // par le moteur de réactivité, y compris quand la mutation vient d'un
  // callback subscribe() ou d'un événement OpenLayers (map.on(...)) — aucun
  // des deux n'est un événement natif Angular, donc aucun des deux ne
  // déclenche de rafraîchissement de vue tout seul dans cette app zoneless.
  private activeVersion = signal(0);

  // Signals : app zoneless (pas de zone.js). Tout champ lu par le template et
  // muté depuis un callback subscribe() OU un événement OpenLayers (map.on)
  // doit être un signal — ni l'un ni l'autre n'est un événement natif Angular,
  // donc aucun des deux ne déclenche de rafraîchissement automatique.
  // À L'INVERSE : les champs liés en DEUX-VOIES via [(ngModel)] (uploadName,
  // uploadSrid, tp.*) DOIVENT rester des champs simples — [(ngModel)] ne
  // fonctionne pas sur un signal (l'assignation générée réécrirait la
  // référence du signal au lieu d'appeler .set()).
  layers = signal<LayerMeta[]>([]);
  cursor = signal<{ x: number; y: number } | null>(null);
  popupProps = signal<Record<string, unknown> | null>(null);
  publishInfo = signal<PublishInfo | null>(null);
  publishing = signal(false);
  importing = signal(false);
  message = signal('');
  error = signal('');
  tpResult = signal<{ x: number; y: number; srid: number } | null>(null);

  crsList: CrsInfo[] = [];
  displayCrsOptions = DISPLAY_CRS;
  displayCrs = 3857;

  // Import (liés via [(ngModel)] / (change) natif → restent des champs simples)
  selectedFile: File | null = null;
  uploadName = '';
  uploadSrid: number | null = null;

  // Outil de conversion ponctuelle (lié via [(ngModel)])
  tp = { x: 0, y: 0, from: 4326, to: 2154 };

  ngAfterViewInit(): void {
    registerProjections();
    this.map = new OLMap({
      target: this.mapEl.nativeElement,
      layers: [new TileLayer({ source: new OSM() })],
      view: this.buildView(this.displayCrs),
    });

    this.popup = new Overlay({
      element: this.popupEl.nativeElement,
      autoPan: true,
      positioning: 'bottom-center',
      offset: [0, -12],
    });
    this.map.addOverlay(this.popup);

    this.map.on('pointermove', (evt) => {
      const c = evt.coordinate;
      this.cursor.set({ x: this.round(c[0]), y: this.round(c[1]) });
    });
    this.map.on('singleclick', (evt) => this.onMapClick(evt.coordinate, evt.pixel));

    this.loadLayers();
    this.api.getCommonCrs().subscribe((l) => (this.crsList = l));
  }

  ngOnDestroy(): void {
    this.map?.setTarget(undefined);
  }

  private round(n: number): number {
    // degrés → 6 décimales ; mètres → 2. Heuristique sur l'amplitude.
    return Math.abs(n) < 1000 ? Math.round(n * 1e6) / 1e6 : Math.round(n * 100) / 100;
  }

  private buildView(srid: number): View {
    const proj = `EPSG:${srid}`;
    const center = transform([2.5, 46.6], 'EPSG:4326', proj); // centre France
    return new View({ projection: proj, center, zoom: srid === 4326 ? 5 : 5.3 });
  }

  // ── Couches ──────────────────────────────────────────────────────────────
  loadLayers(): void {
    this.api.getLayers().subscribe((l) => this.layers.set(l));
  }

  isActive(id: number): boolean {
    this.activeVersion();
    return this.active.has(id);
  }

  toggleLayer(layer: LayerMeta): void {
    if (layer.layer_type !== 'vector') {
      this.error.set('Aperçu raster non disponible dans ce lot (métadonnées seules).');
      return;
    }
    const existing = this.active.get(layer.id);
    if (existing) {
      this.map.removeLayer(existing.olLayer);
      this.active.delete(layer.id);
      this.activeVersion.update((v) => v + 1);
      return;
    }
    this.api.getLayerGeoJSON(layer.id).subscribe({
      next: (gj) => this.addVector(layer, gj),
      error: (e) => this.error.set(e?.error?.detail ?? 'Erreur de chargement de la couche.'),
    });
  }

  /** Style par entité : couleur choroplèthe (__color) si présente, sinon palette. */
  private styleFor(idx: number): StyleFunction {
    const base = PALETTE[idx % PALETTE.length];
    return (feature: FeatureLike) => {
      const color = (feature.get('__color') as string) || base;
      return new Style({
        stroke: new Stroke({ color: feature.get('__color') ? '#334155' : color, width: feature.get('__color') ? 0.6 : 2 }),
        fill: new Fill({ color: color + (feature.get('__color') ? 'cc' : '33') }),
        image: new CircleStyle({ radius: 5, fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 1 }) }),
      });
    };
  }

  private addVector(layer: LayerMeta, geojson: unknown): void {
    const features = new GeoJSON().readFeatures(geojson, {
      dataProjection: 'EPSG:4326',
      featureProjection: `EPSG:${this.displayCrs}`,
    });
    const source = new VectorSource({ features });
    const olLayer = new VectorLayer({ source, style: this.styleFor(this.active.size) });
    this.map.addLayer(olLayer);

    // Légende choroplèthe (métadonnées de la couche calculée).
    const choro = (layer.metadata as Record<string, unknown>)?.['choropleth'] as
      { title?: string; legend?: LegendEntry[] } | undefined;
    const legend = choro?.legend
      ? { title: choro.title || layer.name, entries: choro.legend }
      : undefined;

    this.active.set(layer.id, { olLayer, geojson, meta: layer, legend });
    this.activeVersion.update((v) => v + 1);

    const extent = source.getExtent();
    if (extent && isFinite(extent[0])) {
      this.map.getView().fit(extent, { padding: [40, 40, 40, 40], maxZoom: 14, duration: 300 });
    }
  }

  /** Légendes des couches actives (pour le panneau). */
  activeLegends(): { title: string; entries: LegendEntry[] }[] {
    this.activeVersion();
    return [...this.active.values()].filter((a) => a.legend).map((a) => a.legend!);
  }

  hasActive(): boolean { this.activeVersion(); return this.active.size > 0; }

  // ── Exports ────────────────────────────────────────────────────────────────
  exportPng(): void {
    this.map.once('rendercomplete', () => {
      const mapCanvas = document.createElement('canvas');
      const size = this.map.getSize();
      if (!size) return;
      mapCanvas.width = size[0]; mapCanvas.height = size[1];
      const ctx = mapCanvas.getContext('2d')!;
      this.mapEl.nativeElement.querySelectorAll('.ol-layer canvas').forEach((c) => {
        const canvas = c as HTMLCanvasElement;
        if (canvas.width > 0) ctx.drawImage(canvas, 0, 0);
      });
      const a = document.createElement('a');
      a.href = mapCanvas.toDataURL('image/png');
      a.download = 'carte.png';
      a.click();
    });
    this.map.renderSync();
  }

  exportGeoJSON(): void {
    const first = [...this.active.values()][0];
    if (!first) { this.error.set('Activez une couche à exporter.'); return; }
    this.api.getLayerGeoJSON(first.meta.id).subscribe((gj) => {
      const blob = new Blob([JSON.stringify(gj)], { type: 'application/geo+json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${first.meta.name}.geojson`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  deleteLayer(layer: LayerMeta, ev: Event): void {
    ev.stopPropagation();
    if (!confirm(`Supprimer la couche « ${layer.name} » ?`)) return;
    this.api.deleteLayer(layer.id).subscribe(() => {
      const a = this.active.get(layer.id);
      if (a) { this.map.removeLayer(a.olLayer); this.active.delete(layer.id); this.activeVersion.update((v) => v + 1); }
      this.loadLayers();
    });
  }

  // ── Publication QGIS (OGC API – Features) ──────────────────────────────────
  publish(layer: LayerMeta, ev: Event): void {
    ev.stopPropagation();
    this.publishing.set(true); this.error.set(''); this.message.set('');
    this.api.publishLayer(layer.id).subscribe({
      next: (info) => {
        this.publishing.set(false);
        this.publishInfo.set(info);
        this.message.set(`Couche « ${layer.name} » publiée pour QGIS.`);
        this.loadLayers();
      },
      error: (e) => { this.publishing.set(false); this.error.set(e?.error?.detail ?? 'Échec de la publication.'); },
    });
  }

  showPublishInfo(layer: LayerMeta, ev: Event): void {
    ev.stopPropagation();
    this.api.getPublishInfo(layer.id).subscribe((info) => this.publishInfo.set(info));
  }

  unpublish(layer: LayerMeta, ev: Event): void {
    ev.stopPropagation();
    if (!confirm(`Dépublier « ${layer.name} » (la table QGIS sera supprimée) ?`)) return;
    this.api.unpublishLayer(layer.id).subscribe(() => {
      this.publishInfo.set(null);
      this.message.set(`« ${layer.name} » dépubliée.`);
      this.loadLayers();
    });
  }

  closePublishInfo(): void { this.publishInfo.set(null); }

  copy(text: string): void { navigator.clipboard?.writeText(text); }

  // ── CRS d'affichage ────────────────────────────────────────────────────────
  changeDisplayCrs(srid: number | string): void {
    this.displayCrs = Number(srid);
    this.map.setView(this.buildView(this.displayCrs));
    // Re-projette les couches actives à partir du GeoJSON 4326 mis en cache.
    for (const [, a] of this.active) {
      const features = new GeoJSON().readFeatures(a.geojson, {
        dataProjection: 'EPSG:4326',
        featureProjection: `EPSG:${this.displayCrs}`,
      });
      const src = a.olLayer.getSource();
      src?.clear();
      src?.addFeatures(features);
    }
  }

  // ── Import ─────────────────────────────────────────────────────────────────
  onFile(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    this.selectedFile = input.files?.[0] ?? null;
    if (this.selectedFile && !this.uploadName) this.uploadName = this.selectedFile.name;
  }

  upload(): void {
    if (!this.selectedFile) return;
    this.importing.set(true); this.message.set(''); this.error.set('');
    this.api.uploadLayer(this.selectedFile, this.uploadName, this.uploadSrid).subscribe({
      next: (layer) => {
        this.importing.set(false);
        this.message.set(`Couche « ${layer.name} » importée (${layer.feature_count} entités, CRS source ${layer.srid_source ?? '?'}).`);
        this.selectedFile = null; this.uploadName = ''; this.uploadSrid = null;
        this.loadLayers();
      },
      error: (e) => {
        this.importing.set(false);
        this.error.set(e?.error?.detail ?? "Échec de l'import.");
      },
    });
  }

  // ── Popup d'attributs ────────────────────────────────────────────────────
  private onMapClick(coord: Coordinate, pixel: number[]): void {
    let found: Feature | null = null;
    this.map.forEachFeatureAtPixel(pixel, (f) => { found = f as Feature; return true; });
    if (found) {
      this.popupProps.set((found as Feature).get('__na__') ?? this.featureProps(found));
      this.popup.setPosition(coord);
    } else {
      this.popupProps.set(null);
      this.popup.setPosition(undefined);
    }
  }

  private featureProps(f: Feature): Record<string, unknown> {
    const props = { ...f.getProperties() };
    delete (props as Record<string, unknown>)['geometry'];
    return props as Record<string, unknown>;
  }

  closePopup(): void {
    this.popupProps.set(null);
    this.popup.setPosition(undefined);
  }

  propEntries(o: Record<string, unknown>): { k: string; v: unknown }[] {
    return Object.entries(o).map(([k, v]) => ({ k, v }));
  }

  // ── Conversion ponctuelle de coordonnées ───────────────────────────────────
  runTransform(): void {
    this.tpResult.set(null);
    this.api.transformPoint(this.tp.x, this.tp.y, this.tp.from, this.tp.to).subscribe({
      next: (r) => this.tpResult.set(r),
      error: (e) => this.error.set(e?.error?.detail ?? 'Conversion impossible.'),
    });
  }
}
