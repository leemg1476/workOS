"""Run-scoped graph and durable checkpoints. Official Work is changed only by Kernel."""
import os
from typing import Any, TypedDict

# LangGraph installs LangSmith transitively; this demo uses local logs only.
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from app.domain.contracts import MarketBriefing, PolicyImpact, VocReport, VocReportWithHistory, VocReportWithSources
from app.work.market import digest, make_briefing
from app.work.policy import make_policy_report
from app.work.voc import make_report

GRAPH_VERSION = 'workos-langgraph-v1'


class GraphState(TypedDict, total=False):
    run_id: str
    task_id: str
    manifest: dict[str, Any]
    manifest_hash: str
    model_mode: str
    facts: dict[str, Any]
    report: dict[str, Any]
    metadata: dict[str, Any] | None


class GraphRunner:
    def __init__(self, engine, fault_hook=None):
        self.engine = engine
        self.fault_hook = fault_hook

    def _step(self, state, name, action):
        def log(status, error_type=None):
            with self.engine.begin() as conn:
                conn.execute(text('''INSERT INTO run_steps(run_id,graph_version,node_name,status,error_type)
                    VALUES (:run,:graph,:node,:status,:error)'''),
                    {'run':state['run_id'], 'graph':GRAPH_VERSION, 'node':name,
                     'status':status, 'error':error_type})
        log('STARTED')
        try:
            result = action(state)
        except Exception as exc:
            log('FAILED', type(exc).__name__)
            raise
        log('SUCCEEDED')
        return result

    def _inspect(self, state):
        manifest = state['manifest']
        if (manifest['run_id'] != state['run_id'] or manifest['task_id'] != state['task_id']
                or digest(manifest) != state['manifest_hash']):
            raise ValueError('run context does not match graph input')
        if state['task_id'] == 'TASK-001':
            facts = {'source_rows':len(manifest['observations']), 'dataset':manifest['dataset_version']}
        elif state['task_id'] == 'TASK-002':
            facts = {'document_id':manifest['document_id'], 'followups':len(manifest['followups'])}
        elif state['task_id'] == 'TASK-003':
            facts = {'source_rows':len(manifest['records']), 'method':manifest['method']['method_id']}
        else:
            raise ValueError('unsupported task')
        return {'facts':facts}

    def _draft(self, state):
        manifest, task = state['manifest'], state['task_id']
        if task == 'TASK-001':
            report, metadata = make_briefing(manifest, state['model_mode'])
        elif task == 'TASK-002':
            report, metadata = make_policy_report(manifest), None
        else:
            report, metadata = make_report(manifest), None
        return {'report':report, 'metadata':metadata}

    def _finalize(self, state):
        if self.fault_hook:
            self.fault_hook()
        contract = {'TASK-001':MarketBriefing, 'TASK-002':PolicyImpact, 'TASK-003':VocReport}[state['task_id']]
        if state['task_id']=='TASK-003' and state['manifest']['task_version']>=3:
            contract = VocReportWithSources if state['manifest']['task_version']>=4 else VocReportWithHistory
        report = contract.model_validate(state['report']).model_dump()
        if report['task_id'] != state['task_id'] or report['data_mode'] != 'DEMO DATA':
            raise ValueError('report labels do not match the run')
        return {'report':report}

    def execute(self, run_id, task_id=None, manifest=None, model_mode='MOCK', *, resume=False):
        # Do not restart a failed/expired run or access another run's checkpoint.
        with self.engine.connect() as conn:
            row = conn.execute(text('''SELECT r.status,q.lease_until > now() AS live,ctx.sha256
                FROM runs r JOIN work_queue q ON q.run_id=r.run_id
                JOIN run_contexts ctx ON ctx.run_id=r.run_id
                WHERE r.run_id=:id AND q.status='CLAIMED' '''), {'id':run_id}).mappings().first()
        if not row or row['status'] != 'RUNNING' or not row['live']:
            raise ValueError('run is not active')
        if not resume and (manifest is None or digest(manifest) != row['sha256']):
            raise ValueError('run context hash mismatch')
        dsn = self.engine.url.set(drivername='postgresql').render_as_string(hide_password=False)
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.conn.execute('SET search_path TO workos_checkpoints')
            # Saver migrations are separate from Work migrations; serialize setup across workers.
            with self.engine.begin() as conn:
                conn.execute(text('SELECT pg_advisory_xact_lock(947204)'))
                saver.setup()
            graph = StateGraph(GraphState)
            for name, action in (('inspect_context', self._inspect), ('draft', self._draft), ('finalize', self._finalize)):
                graph.add_node(name, lambda state, n=name, a=action: self._step(state, n, a))
            graph.add_edge(START, 'inspect_context')
            graph.add_edge('inspect_context', 'draft')
            graph.add_edge('draft', 'finalize')
            graph.add_edge('finalize', END)
            compiled = graph.compile(checkpointer=saver)
            config = {'configurable': {'thread_id':str(run_id)}}
            if resume:
                checkpoint = compiled.get_state(config)
                if not checkpoint.values or not checkpoint.next:
                    raise ValueError('no incomplete checkpoint for this run')
                if checkpoint.values.get('manifest_hash') != row['sha256']:
                    raise ValueError('checkpoint context hash mismatch')
                initial = None
            else:
                if compiled.get_state(config).values:
                    raise ValueError('checkpoint already exists; use resume')
                initial = {'run_id':str(run_id), 'task_id':task_id, 'manifest':manifest,
                           'manifest_hash':row['sha256'], 'model_mode':model_mode}
            result = compiled.invoke(initial, config, durability='sync')
            return result['report'], result.get('metadata')
