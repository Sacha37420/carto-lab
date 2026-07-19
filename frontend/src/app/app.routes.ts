import { Routes } from '@angular/router';
import { MapComponent }     from './pages/map/map.component';
import { ProcessingComponent } from './pages/processing/processing.component';
import { MeteoComponent }    from './pages/meteo/meteo.component';

export const routes: Routes = [
  { path: '',        redirectTo: 'map', pathMatch: 'full' },
  { path: 'map',     component: MapComponent },
  { path: 'processing', component: ProcessingComponent },
  { path: 'meteo',   component: MeteoComponent },
  { path: '**',      redirectTo: 'map' },
];
