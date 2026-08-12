"""Album Curator — coverage-constrained narrative selection over a facet DB.

Prototype (2026-08-12). Decoupled, read-only over facet's SQLite DB: reads scored
photos, buckets them by day/location/event, allocates a per-bucket quota summing
to a target, ranks within buckets, runs a soft person-coverage pass, and emits a
candidate set (JSON + contact sheet, optional facet album).

See docs/superpowers/specs/2026-08-12-album-curator-design.md.
"""
from .select import CurationResult, CuratorConfig, curate
