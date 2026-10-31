from app.integrations import search


def test_search_not_ready_until_all_demo_sources_are_searchable(monkeypatch):
    monkeypatch.setenv('WORKOS_SEARCH_MODE', 'OPENSEARCH')
    found = [{'id':'DOC-FX@1'}, {'id':'DOC-RISK@1'}]
    monkeypatch.setattr(search, 'retrieve_documents', lambda ids: (found, {}))
    assert search.readiness()['status'] == 'indexing'
    found.append({'id':'POLICY-VOC@1'})
    assert search.readiness()['status'] == 'connected'
