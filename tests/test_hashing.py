from app.domain.hashing import digest
from app.work.market import digest as legacy_market_digest


def test_canonical_hash_remains_compatible_with_existing_evidence():
    value={'value':'배송','n':3}
    expected='9a6d1724a2e0b387999b256de1e59a421793ab6710d984d5440cc152799a5af5'
    assert digest(value)==expected
    assert legacy_market_digest(value)==expected
    assert digest({'n':3,'value':'배송'})==expected
