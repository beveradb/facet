import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { CuratorService, CuratorCandidates } from './curator.service';

const EMPTY: CuratorCandidates = {
  target: 100, total: 0, selected_count: 0, needs_run: false, buckets: [],
};

// Pins the frontend↔backend route contract: the component spec mocks this
// service, so a wrong endpoint path would otherwise go uncaught. ApiService
// prepends '/api', so these assert the final '/api/curator/...' URLs.
describe('CuratorService', () => {
  let service: CuratorService;
  let httpTesting: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [CuratorService, provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(CuratorService);
    httpTesting = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpTesting.verify());

  it('run() POSTs /api/curator/run', () => {
    service.run().subscribe();
    const req = httpTesting.expectOne('/api/curator/run');
    expect(req.request.method).toBe('POST');
    req.flush(EMPTY);
  });

  it('candidates() GETs /api/curator/candidates', () => {
    service.candidates().subscribe();
    const req = httpTesting.expectOne('/api/curator/candidates');
    expect(req.request.method).toBe('GET');
    req.flush(EMPTY);
  });

  it('toggle() POSTs /api/curator/toggle with id + selected', () => {
    service.toggle('/p/1.jpg', false).subscribe();
    const req = httpTesting.expectOne('/api/curator/toggle');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ id: '/p/1.jpg', selected: false });
    req.flush({ id: '/p/1.jpg', selected: false, selected_count: 0 });
  });

  it('saveAlbum() POSTs /api/curator/save_album with name', () => {
    service.saveAlbum('Curated Final').subscribe();
    const req = httpTesting.expectOne('/api/curator/save_album');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ name: 'Curated Final' });
    req.flush({ album_id: 1, count: 3 });
  });
});
