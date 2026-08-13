import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ApiService } from './api.service';

/** One candidate the curator considered — a still or a clip. */
export interface CuratorItem {
  /** Stable id = the photo's DB path. */
  id: string;
  type: 'photo' | 'video';
  /** Ready-to-use thumbnail URL (already url-encoded by the API). */
  thumb: string;
  caption: string | null;
  category: string | null;
  moment: string | null;
  score: number;
  /** Clip length in seconds; null for stills. */
  duration: number | null;
  selected: boolean;
}

/** A coherent event: one day, one place, one moment. */
export interface CuratorBucket {
  id: string;
  day: string;
  location: string | null;
  event: string;
  quota: number;
  items: CuratorItem[];
}

export interface CuratorCandidates {
  target: number;
  total: number;
  selected_count: number;
  /** True until the pool has been computed at least once (client then runs it). */
  needs_run: boolean;
  buckets: CuratorBucket[];
}

export interface CuratorToggleResult {
  id: string;
  selected: boolean;
  selected_count: number;
}

export interface CuratorSaveResult {
  album_id: number;
  count: number;
}

@Injectable({ providedIn: 'root' })
export class CuratorService {
  private api = inject(ApiService);

  /** Recompute the candidate pool (a few seconds) and return it. */
  run(): Observable<CuratorCandidates> {
    return this.api.post('/curator/run', {});
  }

  /** Read the cached candidate pool with the current selection overlaid. */
  candidates(): Observable<CuratorCandidates> {
    return this.api.get('/curator/candidates');
  }

  /** Tick / untick a single candidate. */
  toggle(id: string, selected: boolean): Observable<CuratorToggleResult> {
    return this.api.post('/curator/toggle', { id, selected });
  }

  /** Save the kept set as a facet album. */
  saveAlbum(name: string): Observable<CuratorSaveResult> {
    return this.api.post('/curator/save_album', { name });
  }
}
