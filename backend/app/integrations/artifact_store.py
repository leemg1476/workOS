"""Development artifact adapter backed by the same transaction as the work ledger."""
import hashlib
import json
from sqlalchemy import text


def store_json(conn, artifact_id, run_id, content, kind='mock_receipt', schema_version='1'):
    encoded = json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    version = conn.execute(text('SELECT COALESCE(MAX(artifact_version),0)+1 FROM artifacts WHERE kind=:kind'), {'kind':kind}).scalar_one() if kind in ('market_briefing','policy_impact','voc_report') else None
    conn.execute(text("""INSERT INTO artifacts(artifact_id,run_id,kind,uri,content,sha256,schema_version,artifact_version)
        VALUES (:id,:run,:kind,:uri,CAST(:content AS jsonb),:hash,:schema,:version)"""),
                 {'id': artifact_id, 'run': run_id, 'kind': kind, 'uri': 'postgres://artifacts/'+artifact_id,
                  'content': encoded, 'hash': digest, 'schema':schema_version, 'version':version})
    return digest
