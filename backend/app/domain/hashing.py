"""Canonical JSON hashing shared by the kernel and workload contracts."""
import hashlib
import json


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()
