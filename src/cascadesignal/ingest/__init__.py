"""CascadeSignal ingestion package.

Entry points:
  ArxivAaveV3Ingestor  — CAS-4: arXiv 2512.11363 bootstrap
  DuneIngester         — CAS-5: Dune Analytics protocol ingestion
  audit_coverage       — CAS-6: block-coverage auditing
  fill_gaps            — CAS-6: gap-fill via Dune
  fill_with_cryo       — CAS-6: surgical extraction via cryo
"""

from cascadesignal.ingest.arxiv_aave_v3 import ArxivAaveV3Ingestor, check_arxiv_status
from cascadesignal.ingest.dune import DuneIngester
from cascadesignal.ingest.gap_fill import audit_coverage, fill_gaps, fill_with_cryo
from cascadesignal.ingest.schema import CANONICAL_SCHEMA, normalize, to_arrow

__all__ = [
    "ArxivAaveV3Ingestor",
    "check_arxiv_status",
    "DuneIngester",
    "audit_coverage",
    "fill_gaps",
    "fill_with_cryo",
    "CANONICAL_SCHEMA",
    "normalize",
    "to_arrow",
]
