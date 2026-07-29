"""Source connectors — one module per source (whatsapp, x, substack, email).

``whatsapp`` is imported as a submodule rather than re-exported here: it
carries a FastAPI router, and this package is also imported by the RQ worker,
which has no business pulling in the web framework.
"""

from catchment.ingestion.base import Connector, IngestSummary, RawRecord, ingest_records
from catchment.ingestion.email_imap import ImapConnector, ImapError

__all__ = [
    "Connector",
    "ImapConnector",
    "ImapError",
    "IngestSummary",
    "RawRecord",
    "ingest_records",
]
