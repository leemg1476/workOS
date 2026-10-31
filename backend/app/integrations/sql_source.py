"""Configured read-only source connection; test fixtures can use their isolated engine."""
import os
from functools import lru_cache
from sqlalchemy import create_engine


@lru_cache(maxsize=2)
def _engine(url):
    return create_engine(url,pool_pre_ping=True)


def source_engine(fallback):
    url=os.getenv('WORKOS_SOURCE_DATABASE_URL')
    return _engine(url) if url else fallback


def source_mode():
    return 'POSTGRES_READ_ONLY_ROLE' if os.getenv('WORKOS_SOURCE_DATABASE_URL') else 'FIXTURE_DB_OWNER'
