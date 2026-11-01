"""Deterministic example plugin; enable explicitly, never loaded by default."""
from app.workloads.registry import Verifier, register_executor, register_verifier


def execute(claim):
    numbers = claim['input_snapshot']['numbers']
    return {'total': sum(numbers)}


def verify(conn, run, work, task, content, artifact_hash):
    numbers = work['input_snapshot'].get('numbers')
    valid = (isinstance(numbers, list) and len(numbers) > 0
             and all(type(n) is int for n in numbers)
             and type(content.get('total')) is int and content['total'] == sum(numbers))
    return {'passed': valid, 'checks': {'total_matches_input': valid}}, {
        'task_id': work['task_id'], 'task_version': work['task_version'], 'sha256': artifact_hash}


register_executor('EXAMPLE-TOTAL', 1, execute)
register_verifier('example_total_v1', Verifier('example_total', '1', 'example-total-validator-v1',
                                               'example_total_deterministic', 'example-total-suite-v1', verify))
