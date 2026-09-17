"""CascadeSignal ingestion package.

Entry points:
 ArxivAaveV3Ingestor —: arXiv 2512.11363 bootstrap
 DuneIngester —: Dune Analytics protocol ingestion
 audit_coverage —: block-coverage auditing
 fill_gaps —: gap-fill via Dune
 fill_with_cryo —: surgical extraction via cryo
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
