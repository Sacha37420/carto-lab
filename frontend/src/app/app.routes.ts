import { Routes } from '@angular/router';
import { HomeComponent }    from './pages/home/home.component';
import { ProfileComponent } from './pages/profile/profile.component';
import { MapComponent }     from './pages/map/map.component';
import { ProcessingComponent } from './pages/processing/processing.component';
import { MeteoComponent }    from './pages/meteo/meteo.component';

export const routes: Routes = [
  { path: '',        component: HomeComponent },
  { path: 'map',     component: MapComponent },
  { path: 'processing', component: ProcessingComponent },
  { path: 'meteo',   component: MeteoComponent },
  { path: 'profile', component: ProfileComponent },
  { path: '**',      redirectTo: '' },
];
