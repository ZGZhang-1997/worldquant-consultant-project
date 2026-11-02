"""Data acquisition for the WorldQuant BRAIN consultant project.

Fetches every data set and data field available for the configured
instrument/region/delay/universe combinations and stores them in SQLite.
"""

from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_DELAY,
    DEFAULT_INSTRUMENT_TYPE,
    DEFAULT_REGION,
    UNIVERSE_CANDIDATES,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_DELAY",
    "DEFAULT_INSTRUMENT_TYPE",
    "DEFAULT_REGION",
    "UNIVERSE_CANDIDATES",
]
