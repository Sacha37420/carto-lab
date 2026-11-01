import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { apiUrl } from './env';


export interface LayerMeta {
  id: number;
  name: string;
  layer_type: 'vector' | 'raster';
  origin: string;
  srid_source: number | null;
  geom_type: string;
  feature_count: number;
  bbox: number[] | null;
  metadata: Record<string, unknown>;
  published_qgis: boolean;
  raster_url: string | null;
  created_at: string;
}

export interface CrsInfo {
  srid: number;
  name: string;
  group?: string;
  projected?: boolean;
  geographic?: boolean;
  unit?: string;
}

export interface GeoJSONFeatureCollection {
  type: 'FeatureCollection';
  crs?: unknown;
  features: unknown[];
}

export interface OpParam {
  name: string;
  type: string;
  label: string;
  default?: unknown;
  options?: { value: string; label: string }[];
}

export interface Operation {
  name: string;
  label: string;
  description: string;
  inputs: number;
  params: OpParam[];
  output_geom: string;
}

export interface RecipeStep {
  op: string;
  params?: Record<string, unknown>;
  inputs: ({ layer: number } | { step: number })[];
  name?: string;
}

export interface Recipe {
  id: number;
  name: string;
  steps: RecipeStep[];
  result_layer: number | null;
  created_at: string;
}

export interface MeteoOptions {
  grandeurs: { key: string; label: string; unit: string }[];
  indicators: { name: string; label: string; params: OpParam[]; column: string | null }[];
  classifications: string[];
  ramps: string[];
}

export interface Job {
  id: number;
  kind: string;
  status: 'PENDING' | 'RUNNING' | 'DONE' | 'ERROR';
  progress: number;
  message: string;
  params: Record<string, unknown>;
  result_layer: number | null;
  created_at: string;
  updated_at: string;
}

export interface PublishInfo {
  published: boolean;
  service_type?: string;
  ogc_url?: string;
  collections_url?: string;
  collection?: string;
  items_url?: string;
  qgis_steps?: string[];
}

export interface MeteoQualityThresholds {
  min_completeness?: number | null;
  max_gap_hours?: number | null;
  max_same_datetime?: number | null;
  max_duplicates?: number | null;
}

export interface MeteoJobRequest {
  grandeur: string;
  year_start: number;
  year_end: number;
  indicator: string;
  indicator_params?: Record<string, unknown>;
  classification?: string;
  n_classes?: number;
  ramp?: string;
  max_stations?: number | null;
  quality_thresholds?: MeteoQualityThresholds;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  private get base(): string {
    return apiUrl();
  }

  getMe(): Observable<unknown> {
    return this.http.get(`${this.base}/api/me/`);
  }



  // ── Couches ─────────────────────────────────────────────────────────────
  getLayers(): Observable<LayerMeta[]> {
    return this.http.get<LayerMeta[]>(`${this.base}/api/layers/`);
  }

  uploadLayer(file: File, name: string, sourceSrid?: number | null): Observable<LayerMeta> {
    const form = new FormData();
    form.append('file', file);
    if (name) form.append('name', name);
    if (sourceSrid) form.append('source_srid', String(sourceSrid));
    return this.http.post<LayerMeta>(`${this.base}/api/layers/`, form);
  }

  deleteLayer(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/layers/${id}/`);
  }

  getLayerGeoJSON(id: number, srid?: number): Observable<GeoJSONFeatureCollection> {
    const q = srid ? `?srid=${srid}` : '';
    return this.http.get<GeoJSONFeatureCollection>(`${this.base}/api/layers/${id}/geojson/${q}`);
  }

  // ── Systèmes de coordonnées ─────────────────────────────────────────────
  getCommonCrs(): Observable<CrsInfo[]> {
    return this.http.get<CrsInfo[]>(`${this.base}/api/crs/`);
  }

  describeCrs(srid: number): Observable<CrsInfo> {
    return this.http.get<CrsInfo>(`${this.base}/api/crs/?srid=${srid}`);
  }

  transformPoint(x: number, y: number, fromSrid: number, toSrid: number):
    Observable<{ x: number; y: number; srid: number }> {
    return this.http.post<{ x: number; y: number; srid: number }>(
      `${this.base}/api/crs/transform-point/`,
      { x, y, from_srid: fromSrid, to_srid: toSrid },
    );
  }

  // ── Moteur de calculs (Feature 3) ────────────────────────────────────────
  getProcessings(): Observable<Operation[]> {
    return this.http.get<Operation[]>(`${this.base}/api/processings/`);
  }

  runProcessing(operation: string, inputs: number[], params: Record<string, unknown>, name?: string):
    Observable<LayerMeta> {
    return this.http.post<LayerMeta>(`${this.base}/api/processings/run/`,
      { operation, inputs, params, name });
  }

  // ── Recettes / constructeur (Feature 6) ──────────────────────────────────
  getRecipes(): Observable<Recipe[]> {
    return this.http.get<Recipe[]>(`${this.base}/api/recipes/`);
  }

  createRecipe(name: string, steps: RecipeStep[]): Observable<Recipe> {
    return this.http.post<Recipe>(`${this.base}/api/recipes/`, { name, steps });
  }

  runRecipe(id: number): Observable<LayerMeta> {
    return this.http.post<LayerMeta>(`${this.base}/api/recipes/${id}/run/`, {});
  }

  deleteRecipe(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/recipes/${id}/`);
  }

  // ── Météo-France (Feature 4) + jobs async ────────────────────────────────
  getMeteoOptions(): Observable<MeteoOptions> {
    return this.http.get<MeteoOptions>(`${this.base}/api/meteo/options/`);
  }

  /** État du coffre de la clé Météo-France de l'utilisateur (jamais la valeur). */
  getMeteoKeyStatus(): Observable<{ meteo_key_configured: boolean }> {
    return this.http.get<{ meteo_key_configured: boolean }>(`${this.base}/api/meteo/key/`);
  }

  /** Enregistre la clé dans le coffre serveur ; chaîne vide ⇒ l'efface. */
  setMeteoKey(value: string): Observable<void> {
    return this.http.put<void>(`${this.base}/api/meteo/key/`, { meteo_key: value });
  }

  /** Lance un job. La clé est lue par le serveur dans le coffre de l'utilisateur. */
  launchMeteoJob(body: MeteoJobRequest): Observable<Job> {
    return this.http.post<Job>(`${this.base}/api/meteo/jobs/`, body);
  }

  getJobs(): Observable<Job[]> {
    return this.http.get<Job[]>(`${this.base}/api/jobs/`);
  }

  getJob(id: number): Observable<Job> {
    return this.http.get<Job>(`${this.base}/api/jobs/${id}/`);
  }

  // ── Publication OGC API – Features / QGIS (Lot 4) ────────────────────────
  publishLayer(id: number): Observable<PublishInfo> {
    return this.http.post<PublishInfo>(`${this.base}/api/layers/${id}/publish/`, {});
  }

  getPublishInfo(id: number): Observable<PublishInfo> {
    return this.http.get<PublishInfo>(`${this.base}/api/layers/${id}/publish/`);
  }

  unpublishLayer(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/layers/${id}/publish/`);
  }
}
