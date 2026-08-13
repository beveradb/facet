import type { Mock } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { MatSnackBar } from '@angular/material/snack-bar';
import { CuratorService, CuratorCandidates } from '../../core/services/curator.service';
import { AuthService } from '../../core/services/auth.service';
import { CuratorComponent } from './curator.component';

function makeCandidates(overrides: Partial<CuratorCandidates> = {}): CuratorCandidates {
  return {
    target: 100,
    total: 4,
    selected_count: 3,
    needs_run: false,
    buckets: [
      {
        id: 'b1', day: '2026-07-24', location: 'Harenkarspel, NL', event: 'concert', quota: 2,
        items: [
          { id: '/p/1.jpg', type: 'photo', thumb: '/thumbnail?path=%2Fp%2F1.jpg&size=320', caption: 'a', category: 'concert', moment: 'nightlife', score: 8.1, duration: null, selected: true },
          { id: '/p/2.jpg', type: 'photo', thumb: '/thumbnail?path=%2Fp%2F2.jpg&size=320', caption: 'b', category: 'concert', moment: 'nightlife', score: 7.4, duration: null, selected: false },
        ],
      },
      {
        id: 'b2', day: '2026-07-25', location: null, event: 'other', quota: 2,
        items: [
          { id: '/p/3.jpg', type: 'photo', thumb: '/thumbnail?path=%2Fp%2F3.jpg&size=320', caption: 'c', category: null, moment: null, score: 6.0, duration: null, selected: true },
          { id: '/p/4.mp4', type: 'video', thumb: '/thumbnail?path=%2Fp%2F4.mp4&size=320', caption: 'd', category: null, moment: null, score: 5.0, duration: 12, selected: true },
        ],
      },
    ],
    ...overrides,
  };
}

describe('CuratorComponent', () => {
  let component: CuratorComponent;
  let mockCurator: { candidates: Mock; run: Mock; toggle: Mock; saveAlbum: Mock };
  let mockAuth: { isEdition: Mock };
  let mockSnackBar: { open: Mock };

  function setup(): void {
    TestBed.configureTestingModule({
      providers: [
        CuratorComponent,
        { provide: CuratorService, useValue: mockCurator },
        { provide: AuthService, useValue: mockAuth },
        { provide: MatSnackBar, useValue: mockSnackBar },
      ],
    });
    component = TestBed.inject(CuratorComponent);
  }

  beforeEach(() => {
    mockCurator = {
      candidates: vi.fn(() => of(makeCandidates())),
      run: vi.fn(() => of(makeCandidates())),
      toggle: vi.fn((id: string, selected: boolean) => of({ id, selected, selected_count: 0 })),
      saveAlbum: vi.fn(() => of({ album_id: 7, count: 3 })),
    };
    mockAuth = { isEdition: vi.fn(() => true) };
    mockSnackBar = { open: vi.fn() };
  });

  const internals = () => component as unknown as {
    buckets: () => CuratorCandidates['buckets'];
    selectedCount: () => number;
    dayGroups: () => { day: string; selected: number; total: number }[];
    toggle: (item: unknown) => Promise<void>;
    save: () => Promise<void>;
    load: () => Promise<void>;
  };

  it('creates and loads the pool from the service', async () => {
    setup();
    await internals().load();
    expect(mockCurator.candidates).toHaveBeenCalled();
    expect(internals().buckets().length).toBe(2);
  });

  it('derives the running kept count from live item state', async () => {
    setup();
    await internals().load();
    // 3 of 4 items start selected.
    expect(internals().selectedCount()).toBe(3);
  });

  it('groups buckets into day sections with per-day tallies', async () => {
    setup();
    await internals().load();
    const groups = internals().dayGroups();
    expect(groups.map(g => g.day)).toEqual(['2026-07-24', '2026-07-25']);
    expect(groups[0]).toMatchObject({ selected: 1, total: 2 });
    expect(groups[1]).toMatchObject({ selected: 2, total: 2 });
  });

  it('toggle optimistically flips selection, calls the API, and updates the count', async () => {
    setup();
    await internals().load();
    const item = internals().buckets()[0].items[0]; // /p/1.jpg, selected: true
    expect(internals().selectedCount()).toBe(3);

    await internals().toggle(item);

    expect(mockCurator.toggle).toHaveBeenCalledWith('/p/1.jpg', false);
    // The item is now unselected → count drops to 2.
    expect(internals().selectedCount()).toBe(2);
    const patched = internals().buckets()[0].items[0];
    expect(patched.selected).toBe(false);
  });

  it('reverts the optimistic toggle when the API call fails', async () => {
    setup();
    await internals().load();
    mockCurator.toggle.mockReturnValueOnce(throwError(() => new Error('boom')));
    const item = internals().buckets()[1].items[1]; // /p/4.mp4, selected: true

    await internals().toggle(item);

    // Reverted to its original selected state → count unchanged.
    expect(internals().selectedCount()).toBe(3);
    expect(internals().buckets()[1].items[1].selected).toBe(true);
    expect(mockSnackBar.open).toHaveBeenCalled();
  });

  it('does not toggle when the user lacks edition rights', async () => {
    mockAuth.isEdition = vi.fn(() => false);
    setup();
    await internals().load();
    const item = internals().buckets()[0].items[0];

    await internals().toggle(item);

    expect(mockCurator.toggle).not.toHaveBeenCalled();
    expect(internals().selectedCount()).toBe(3);
  });

  it('save calls the album API and notifies with the saved count', async () => {
    setup();
    await internals().load();
    await internals().save();
    expect(mockCurator.saveAlbum).toHaveBeenCalledWith('Curated Final');
    expect(mockSnackBar.open).toHaveBeenCalled();
  });

  it('auto-runs curation when the pool needs computing', async () => {
    mockCurator.candidates = vi.fn(() => of(makeCandidates({ needs_run: true, buckets: [], total: 0, selected_count: 0 })));
    setup();
    await internals().load();
    expect(mockCurator.run).toHaveBeenCalled();
  });
});
