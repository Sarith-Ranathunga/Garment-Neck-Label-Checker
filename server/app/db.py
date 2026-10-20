"""Connection helper. One place, so the DSN is configured once."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .config import settings


@contextmanager
def cursor(commit: bool = False):
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
