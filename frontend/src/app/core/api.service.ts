import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

interface EnvWindow {
  __env?: { apiUrl?: string };
}

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

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  private get base(): string {
    return (window as unknown as EnvWindow).__env?.apiUrl
      ?? 'http://localhost:8091';
  }

  getMe(): Observable<unknown> {
    return this.http.get(`${this.base}/api/me/`);
  }

  getDepartments(): Observable<unknown[]> {
    return this.http.get<unknown[]>(`${this.base}/api/departments/`);
  }

  getUsers(): Observable<unknown[]> {
    return this.http.get<unknown[]>(`${this.base}/api/users/`);
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
}
