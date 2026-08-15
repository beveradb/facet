import { Component, computed, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { firstValueFrom } from 'rxjs';
import {
  CuratorService, CuratorBucket, CuratorItem,
} from '../../core/services/curator.service';
import { AuthService } from '../../core/services/auth.service';

/** A day section: its event buckets plus running per-day selection tallies. */
interface DayGroup {
  day: string;
  buckets: CuratorBucket[];
  selected: number;
  total: number;
}

@Component({
  selector: 'app-curator',
  standalone: true,
  host: { class: 'block px-4 pt-4 pb-24' },
  imports: [
    DecimalPipe, MatIconModule, MatButtonModule, MatTooltipModule,
    MatProgressSpinnerModule, MatSnackBarModule,
  ],
  template: `
    <!-- Sticky header: running count vs target + actions -->
    <div class="sticky top-0 z-20 -mx-4 px-4 py-3 mb-4 flex items-center gap-3
                bg-[var(--mat-sys-surface)] backdrop-blur border-b border-[var(--mat-sys-outline-variant)]">
      <div class="flex flex-col">
        <span class="text-lg font-semibold leading-tight">Album Curator</span>
        <span class="text-xs opacity-60">Review the auto-selection, tick to keep, save when ready</span>
      </div>

      <div class="ml-auto flex items-center gap-3">
        <div class="flex items-baseline gap-1 tabular-nums"
             [class.text-[var(--mat-sys-error)]]="selectedCount() > target()">
          <span class="text-2xl font-bold">{{ selectedCount() }}</span>
          <span class="opacity-60 text-sm">/ {{ target() }} kept</span>
          <span class="opacity-40 text-xs ml-1">({{ total() }} candidates)</span>
        </div>

        @if (canEdit()) {
          <button mat-stroked-button
                  [disabled]="running() || loading()"
                  matTooltip="Recompute the candidate pool from the library"
                  (click)="runCuration()">
            <mat-icon>refresh</mat-icon>
            Recompute
          </button>
          <button mat-flat-button color="primary"
                  [disabled]="saving() || loading() || selectedCount() === 0"
                  (click)="save()">
            <mat-icon>save</mat-icon>
            Save
          </button>
        }
      </div>
    </div>

    @if (loading() || running()) {
      <div class="flex flex-col items-center justify-center py-24 gap-3">
        <mat-spinner diameter="48" />
        <span class="opacity-60 text-sm">{{ running() ? 'Curating…' : 'Loading…' }}</span>
      </div>
    } @else if (dayGroups().length === 0) {
      <div class="text-center py-24 opacity-60">
        <mat-icon class="!text-5xl !w-12 !h-12 mb-4">auto_awesome</mat-icon>
        <p>No candidates yet.</p>
        @if (canEdit()) {
          <button mat-flat-button color="primary" class="!mt-4" (click)="runCuration()">
            Run curation
          </button>
        }
      </div>
    } @else {
      @for (group of dayGroups(); track group.day) {
        <section class="mb-8">
          <!-- Day header -->
          <div class="flex items-baseline gap-2 mb-3 pb-1 border-b border-[var(--mat-sys-outline-variant)]">
            <h2 class="text-base font-semibold">{{ group.day }}</h2>
            <span class="text-xs opacity-60 tabular-nums">{{ group.selected }} / {{ group.total }} kept</span>
          </div>

          <!-- Event sub-groups -->
          @for (bucket of group.buckets; track bucket.id) {
            <div class="mb-5">
              <div class="flex items-center gap-2 mb-2">
                <mat-icon class="!text-base !w-4 !h-4 !leading-4 opacity-60">event</mat-icon>
                <span class="text-sm font-medium capitalize">{{ bucket.event }}</span>
                @if (bucket.location) {
                  <span class="text-xs opacity-50">· {{ bucket.location }}</span>
                }
                <span class="text-[11px] opacity-40 ml-1">quota {{ bucket.quota }}</span>
              </div>

              <!-- Thumbnail grid -->
              <div class="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 gap-2">
                @for (item of bucket.items; track item.id) {
                  <div
                    role="button"
                    [attr.tabindex]="canEdit() ? 0 : -1"
                    [attr.aria-pressed]="item.selected"
                    [attr.aria-label]="(item.selected ? 'Keep' : 'Not kept') + ': ' + (item.caption || item.id)"
                    class="group relative aspect-square rounded-lg overflow-hidden select-none
                           focus-visible:outline-2 focus-visible:outline-[var(--mat-sys-primary)] focus-visible:outline-offset-2"
                    [class.cursor-pointer]="canEdit()"
                    [class.ring-2]="item.selected"
                    [class.ring-[var(--mat-sys-primary)]]="item.selected"
                    (click)="toggle(item)"
                    (keydown.enter)="toggle(item); $event.preventDefault()"
                    (keydown.space)="toggle(item); $event.preventDefault()">
                    <img [src]="item.thumb"
                         [alt]="item.caption || item.id"
                         loading="lazy" decoding="async"
                         class="w-full h-full object-cover transition-opacity"
                         [class.opacity-40]="!item.selected"
                         [class.grayscale]="!item.selected" />

                    <!-- Selected check / empty check -->
                    <div class="absolute top-1 left-1 w-6 h-6 rounded-full flex items-center justify-center
                                transition-colors"
                         [class.bg-[var(--mat-sys-primary)]]="item.selected"
                         [class.bg-black/40]="!item.selected">
                      <mat-icon class="!text-base !w-4 !h-4 !leading-4"
                                [class.text-white]="item.selected"
                                [class.text-white/60]="!item.selected"
                                aria-hidden="true">
                        {{ item.selected ? 'check_circle' : 'radio_button_unchecked' }}
                      </mat-icon>
                    </div>

                    <!-- Video duration badge -->
                    @if (item.type === 'video') {
                      <div class="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-black/60">
                        <mat-icon class="!text-xs !w-3 !h-3 !leading-3 text-white" aria-hidden="true">play_arrow</mat-icon>
                        @if (item.duration) {
                          <span class="text-[10px] text-white tabular-nums">{{ item.duration | number:'1.0-0' }}s</span>
                        }
                      </div>
                    }

                    <!-- Score badge -->
                    <div class="absolute bottom-1 right-1 px-1 py-0.5 rounded bg-black/60 text-[10px] font-bold text-white tabular-nums">
                      {{ item.score | number:'1.1-1' }}
                    </div>
                  </div>
                }
              </div>
            </div>
          }
        </section>
      }
    }
  `,
})
export class CuratorComponent {
  private readonly curator = inject(CuratorService);
  private readonly snackBar = inject(MatSnackBar);
  protected readonly auth = inject(AuthService);

  private readonly albumName = 'Curated Final';

  protected readonly buckets = signal<CuratorBucket[]>([]);
  protected readonly target = signal(100);
  protected readonly total = signal(0);
  protected readonly needsRun = signal(false);
  protected readonly loading = signal(false);
  protected readonly running = signal(false);
  protected readonly saving = signal(false);

  protected readonly canEdit = computed(() => this.auth.isEdition());

  /** Running kept count, derived from the live item state (updates on every tick). */
  protected readonly selectedCount = computed(() =>
    this.buckets().reduce(
      (sum, b) => sum + b.items.filter(it => it.selected).length, 0,
    ),
  );

  /** Buckets grouped into day sections, with per-day tallies precomputed
   *  (so the template never calls a method to count). */
  protected readonly dayGroups = computed<DayGroup[]>(() => {
    const byDay = new Map<string, CuratorBucket[]>();
    for (const b of this.buckets()) {
      const list = byDay.get(b.day) ?? [];
      list.push(b);
      byDay.set(b.day, list);
    }
    return [...byDay.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([day, dayBuckets]) => {
        const items = dayBuckets.flatMap(b => b.items);
        return {
          day,
          buckets: dayBuckets,
          selected: items.filter(it => it.selected).length,
          total: items.length,
        };
      });
  });

  /** Paths with a toggle POST in flight — a second click is dropped, not queued. */
  private readonly toggleInFlight = new Set<string>();

  constructor() {
    void this.load();
  }

  /** Load the cached pool; if it has never been computed, run it (edition only). */
  private async load(): Promise<void> {
    this.loading.set(true);
    try {
      const res = await firstValueFrom(this.curator.candidates());
      this.apply(res.buckets, res.target, res.total, res.needs_run);
      if (res.needs_run && this.canEdit()) {
        await this.runCuration();
      }
    } catch {
      this.notify('Failed to load candidates');
    } finally {
      this.loading.set(false);
    }
  }

  protected async runCuration(): Promise<void> {
    if (this.running()) return;
    this.running.set(true);
    try {
      const res = await firstValueFrom(this.curator.run());
      this.apply(res.buckets, res.target, res.total, res.needs_run);
    } catch {
      this.notify('Curation failed');
    } finally {
      this.running.set(false);
    }
  }

  private apply(buckets: CuratorBucket[], target: number, total: number, needsRun: boolean): void {
    this.buckets.set(buckets);
    this.target.set(target);
    this.total.set(total);
    this.needsRun.set(needsRun);
  }

  /** Tick / untick a candidate. Optimistic, reconciled with server truth. */
  protected async toggle(item: CuratorItem): Promise<void> {
    if (!this.canEdit() || this.toggleInFlight.has(item.id)) return;
    this.toggleInFlight.add(item.id);
    const next = !item.selected;
    this.patchSelected(item.id, next);
    try {
      const res = await firstValueFrom(this.curator.toggle(item.id, next));
      // Reconcile with server truth (in case another edit landed meanwhile).
      this.patchSelected(item.id, res.selected);
    } catch {
      this.patchSelected(item.id, !next); // revert
      this.notify('Could not update selection');
    } finally {
      this.toggleInFlight.delete(item.id);
    }
  }

  /** Immutably patch one item's selected flag across the buckets signal. */
  private patchSelected(id: string, selected: boolean): void {
    this.buckets.update(buckets =>
      buckets.map(b => {
        if (!b.items.some(it => it.id === id)) return b;
        return { ...b, items: b.items.map(it => it.id === id ? { ...it, selected } : it) };
      }),
    );
  }

  protected async save(): Promise<void> {
    if (this.saving()) return;
    this.saving.set(true);
    try {
      const res = await firstValueFrom(this.curator.saveAlbum(this.albumName));
      this.notify(`Saved ${res.count} photos to “${this.albumName}”`);
    } catch {
      this.notify('Save failed');
    } finally {
      this.saving.set(false);
    }
  }

  private notify(message: string): void {
    this.snackBar.open(message, '', {
      duration: 2500, horizontalPosition: 'right', verticalPosition: 'bottom',
    });
  }
}
