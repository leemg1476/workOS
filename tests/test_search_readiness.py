from app.integrations import search


def test_search_not_ready_until_all_demo_sources_are_searchable(monkeypatch):
    monkeypatch.setenv('WORKOS_SEARCH_MODE', 'OPENSEARCH')
    found = [{'id':item} for item in search.DEMO_DOCUMENT_IDS[:-1]]
    monkeypatch.setattr(search, 'retrieve_documents', lambda ids: (found, {}))
    assert search.readiness()['status'] == 'indexing'
    found.append({'id':search.DEMO_DOCUMENT_IDS[-1]})
    assert search.readiness()['status'] == 'connected'
