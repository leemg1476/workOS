import os
from pathlib import Path
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql+psycopg://workos:workos@localhost:5432/workos')
if os.getenv('WORKOS_MODE', 'MOCK').upper() != 'MOCK':
    raise RuntimeError('LIVE mode is not implemented; use WORKOS_MODE=MOCK')
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def migrate():
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(947201)'))
        conn.exec_driver_sql('CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
        root = Path(__file__).resolve().parents[2] / 'migrations'
        for path in sorted(root.glob('[0-9]*.sql')):
            if path.stem.endswith('_down'):
                continue
            if not conn.execute(text('SELECT 1 FROM schema_migrations WHERE version=:v'), {'v': path.stem}).first():
                conn.exec_driver_sql(path.read_text(encoding='utf-8'))
                conn.execute(text('INSERT INTO schema_migrations(version) VALUES (:v)'), {'v': path.stem})
