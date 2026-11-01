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
verifier_kind_resolvers = {}
acceptance_hooks = {}
trigger_hooks = {}
retry_guards = {}
revalidate_guards = {}
review_guards = {}
RESERVED_VERIFIERS = {'mock_receipt','market_briefing_v1','policy_impact_v1','voc_report_v1',
                      'voc_report_v2','voc_report_v3','policy_followup_verification_v1',
                      'policy_index_v1','policy_regression_v1'}


def register_executor(task_id, version, execute):
    key = (task_id, version)
    if task_id in {'TASK-001', 'TASK-002', 'TASK-003'} or key in executors:
        raise ValueError('duplicate workload executor')
    executors[key] = execute


def register_verifier(kind, verifier):
    if kind in verifiers or kind in RESERVED_VERIFIERS:
        raise ValueError('duplicate workload verifier')
    verifiers[kind] = verifier


def register_builtin_verifier(kind, verifier):
    if kind not in RESERVED_VERIFIERS or kind in verifiers:
        raise ValueError('invalid or duplicate built-in verifier')
    verifiers[kind] = verifier


def register_verifier_kind_resolver(kind, resolver):
    if kind in verifier_kind_resolvers:
        raise ValueError('duplicate verifier kind resolver')
    verifier_kind_resolvers[kind] = resolver


def register_acceptance_hook(kind, hook):
    if kind in acceptance_hooks:
        raise ValueError('duplicate acceptance hook')
    acceptance_hooks[kind] = hook


def register_trigger_hook(kind, hook):
    if kind in trigger_hooks:
        raise ValueError('duplicate trigger hook')
    trigger_hooks[kind] = hook


def register_retry_guard(kind, guard):
    if kind in retry_guards:
        raise ValueError('duplicate retry guard')
    retry_guards[kind] = guard


def register_revalidate_guard(kind, guard):
    if kind in revalidate_guards:
        raise ValueError('duplicate revalidation guard')
    revalidate_guards[kind] = guard


def register_review_guard(kind, guard):
    if kind in review_guards:
        raise ValueError('duplicate review guard')
    review_guards[kind] = guard


def register_kind_executor(kind, execute):
    if kind in kind_executors:
        raise ValueError('duplicate workload executor kind')
    kind_executors[kind] = execute


def load_builtins():
    """Load the deployment's built-in workload registrations before extensions."""
    for name in ('text_workflow','text_sources','market_verifier','policy_verifiers','voc_verifier','policy_lifecycle','demo_execution'):
        importlib.import_module('app.workloads.' + name)


def load_configured():
    """Load deployment-selected trusted modules once in each API/worker process."""
    load_builtins()
    for name in filter(None, (x.strip() for x in os.getenv('WORKOS_WORKLOAD_MODULES', '').split(','))):
        importlib.import_module(name)
