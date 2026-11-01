"""Registration of trusted code. Configuration never imports a user-provided path."""
from dataclasses import dataclass
from typing import Callable
import importlib
import os


@dataclass(frozen=True)
class Verifier:
    artifact_kind: str
    schema_version: str
    verifier_version: str
    verification_type: str
    suite_version: str
    verify: Callable


executors = {}
kind_executors = {}
verifiers = {}


def register_executor(task_id, version, execute):
    key = (task_id, version)
    if task_id in {'TASK-001', 'TASK-002', 'TASK-003'} or key in executors:
        raise ValueError('duplicate workload executor')
    executors[key] = execute


def register_verifier(kind, verifier):
    reserved = {'mock_receipt','market_briefing_v1','policy_impact_v1','voc_report_v1',
                'voc_report_v2','voc_report_v3','policy_followup_verification_v1',
                'policy_index_v1','policy_regression_v1'}
    if kind in verifiers or kind in reserved:
        raise ValueError('duplicate workload verifier')
    verifiers[kind] = verifier


def register_kind_executor(kind, execute):
    if kind in kind_executors:
        raise ValueError('duplicate workload executor kind')
    kind_executors[kind] = execute


def load_configured():
    """Load deployment-selected trusted modules once in each API/worker process."""
    for name in filter(None, (x.strip() for x in os.getenv('WORKOS_WORKLOAD_MODULES', '').split(','))):
        importlib.import_module(name)
