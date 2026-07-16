import proj4 from 'proj4';
import { register } from 'ol/proj/proj4';

/**
 * Enregistre les CRS français prioritaires (cf. to_do point 2) auprès de proj4
 * puis d'OpenLayers, pour permettre l'affichage de la carte dans ces projections.
 * EPSG:4326 et EPSG:3857 sont natifs à OpenLayers — inutile de les redéfinir.
 * La reprojection des COUCHES vers un EPSG arbitraire reste assurée côté serveur
 * (endpoint /geojson/?srid=), donc l'UI n'est pas limitée à cette liste.
 */
let done = false;

export function registerProjections(): void {
  if (done) return;

  // Lambert-93 (RGF93) — EPSG:2154
  proj4.defs(
    'EPSG:2154',
    '+proj=lcc +lat_0=46.5 +lon_0=3 +lat_1=49 +lat_2=44 +x_0=700000 ' +
    '+y_0=6600000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs',
  );

  // Coniques Conformes Zone CC42..CC50 (EPSG:3942..3950).
  for (let zone = 42; zone <= 50; zone++) {
    const epsg = 3900 + zone; // 3942..3950
    const y0 = (zone - 42) * 1_000_000 + 1_200_000;
    proj4.defs(
      `EPSG:${epsg}`,
      `+proj=lcc +lat_0=${zone} +lon_0=3 +lat_1=${zone - 0.75} +lat_2=${zone + 0.75} ` +
      `+x_0=1700000 +y_0=${y0} +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs`,
    );
  }

  register(proj4);
  done = true;
}

/** CRS proposés pour l'affichage de la carte (ceux enregistrés ci-dessus + natifs). */
export const DISPLAY_CRS: { srid: number; label: string }[] = [
  { srid: 3857, label: 'Web Mercator (3857)' },
  { srid: 4326, label: 'WGS 84 (4326)' },
  { srid: 2154, label: 'Lambert-93 (2154)' },
  { srid: 3946, label: 'RGF93 / CC46 (3946)' },
];
