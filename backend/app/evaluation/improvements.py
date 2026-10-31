"""Explicit, auditable VOC method evaluation and user-controlled promotion."""
from sqlalchemy import text
from app.work.service import Kernel, Conflict, NotFound, uid, canonical, one
from app.work.market import digest
from app.work.voc import classify

EVALUATOR_VERSION = 'voc-classifier-eval-v2'


class ImprovementService:
    def __init__(self, engine):
        self.engine = engine
        self.kernel = Kernel(engine)

    def feedback(self, request):
        data = request.model_dump()
        with self.engine.begin() as c:
            self.kernel._lock(c)
            old = one(c, '''SELECT f.*,p.proposal_id,p.challenger_version FROM voc_feedback f
                JOIN improvement_proposals p ON p.feedback_id=f.feedback_id WHERE f.idempotency_key=:key''', {'key':data['idempotency_key']})
            if old:
                if any(str(old[k]) != str(data[k]) for k in ('artifact_id','recommendation_id','reason','challenger_version')):
                    raise Conflict('feedback idempotency key used for a different request')
                return str(old['proposal_id']), False
            artifact = one(c, '''SELECT a.content,ctx.manifest FROM artifacts a JOIN runs r ON r.run_id=a.run_id
                JOIN run_contexts ctx ON ctx.run_id=r.run_id JOIN evaluations e ON e.artifact_id=a.artifact_id WHERE a.artifact_id=:id AND a.kind='voc_report'
                AND r.status='SUCCEEDED' AND e.verdict='PASS' ''', {'id':data['artifact_id']})
            if not artifact:
                raise Conflict('feedback requires a verified VOC artifact')
            if data['recommendation_id'] not in {x['recommendation_id'] for x in artifact['content']['recommendations']}:
                raise Conflict('recommendation does not belong to artifact')
            if data['challenger_version'] not in ('challenger-v2','challenger-regressed','challenger-overfit'):
                raise Conflict('unknown demo challenger')
            state = one(c,"SELECT * FROM voc_method_state WHERE task_id='TASK-003' FOR UPDATE")
            if not state:
                raise Conflict('VOC champion is unavailable')
            if state['active_method_id'] == data['challenger_version']:
                raise Conflict('challenger must differ from the active method')
            if artifact['content'].get('method_version') != state['active_method_id']:
                raise Conflict('feedback report was created by a different method; run the current champion first')
            source = one(c,"SELECT task_id,document_id,pinned_version,status FROM task_source_dependencies WHERE task_id='TASK-003' AND document_id='POLICY-VOC'")
            source_snapshot = dict(source) if source else None
            if artifact['manifest'].get('source_dependency') != source_snapshot:
                raise Conflict('feedback report used an outdated source dependency; run the task again')
            feedback_id, proposal_id = uid(), uid()
            c.execute(text('''INSERT INTO voc_feedback(feedback_id,artifact_id,recommendation_id,reason,idempotency_key)
                VALUES (:id,:artifact,:recommendation,:reason,:key)'''), {'id':feedback_id,'artifact':data['artifact_id'],
                'recommendation':data['recommendation_id'],'reason':data['reason'],'key':data['idempotency_key']})
            c.execute(text('''INSERT INTO improvement_proposals(proposal_id,feedback_id,task_id,changed_component,champion_version,challenger_version,suite_version,status,champion_state_version,source_dependency_snapshot)
                VALUES (:id,:feedback,'TASK-003','classification_rules',:champion,:challenger,'voc-suite-v1','DRAFT',:version,CAST(:source AS jsonb))'''),
                {'id':proposal_id,'feedback':feedback_id,'champion':state['active_method_id'],'challenger':data['challenger_version'],'version':state['version'],'source':canonical(source_snapshot)})
            self.kernel._event(c,'voc.feedback_received',feedback_id,{'artifact_id':data['artifact_id'],
                'recommendation_id':data['recommendation_id'],'reason':data['reason']},actor='user',key='feedback:'+data['idempotency_key'])
            self.kernel._event(c,'improvement.created',proposal_id,{'feedback_id':feedback_id,'changed_component':'classification_rules',
                'champion':state['active_method_id'],'champion_state_version':state['version'],'challenger':data['challenger_version'],
                'source_dependency':source_snapshot},actor='user',cause=feedback_id)
            return proposal_id, True

    def _idempotent(self, c, proposal_id, action, key):
        prior = one(c, 'SELECT subject_id,event_type FROM work_events WHERE idempotency_key=:key', {'key':f'improvement.{action}:{key}'})
        if prior:
            if prior['subject_id'] != proposal_id or prior['event_type'] != f'improvement.{action}':
                raise Conflict('action idempotency key already used')
            return True
        return False

    def _proposal(self, c, proposal_id):
        p = one(c, 'SELECT * FROM improvement_proposals WHERE proposal_id=:id FOR UPDATE', {'id':proposal_id})
        if not p:
            raise NotFound('improvement proposal not found')
        return p

    def _inputs(self, c, proposal):
        source = one(c,"SELECT task_id,document_id,pinned_version,status FROM task_source_dependencies WHERE task_id='TASK-003' AND document_id='POLICY-VOC'")
        if proposal['source_dependency_snapshot'] is None or (dict(source) if source else None) != proposal['source_dependency_snapshot']:
            raise Conflict('source dependency changed since proposal creation')
        suite = one(c,'SELECT * FROM voc_eval_suites WHERE suite_version=:suite', {'suite':proposal['suite_version']})
        cases = [dict(r) for r in c.execute(text('''SELECT case_id,body,expected_category FROM voc_eval_cases
            WHERE suite_version=:suite ORDER BY case_id'''), {'suite':proposal['suite_version']}).mappings()]
        champion = one(c,'SELECT * FROM voc_method_versions WHERE method_id=:id', {'id':proposal['champion_version']})
        challenger = one(c,'SELECT * FROM voc_method_versions WHERE method_id=:id', {'id':proposal['challenger_version']})
        if not suite or not champion or not challenger or len(cases)<20 or len(cases)!=suite['case_count'] or digest(cases)!=suite['sha256']:
            raise Conflict('fixed evaluation suite missing or changed')
        if digest(champion['rules'])!=champion['sha256'] or digest(challenger['rules'])!=challenger['sha256']:
            raise Conflict('method version hash mismatch')
        if champion['method_id'] == challenger['method_id'] or champion['changed_component'] != 'classification_rules' or challenger['changed_component'] != 'classification_rules':
            raise Conflict('a distinct single-component challenger is required')
        return suite,cases,champion,challenger

    def _regression_inputs(self,c):
        suite = one(c,"SELECT * FROM voc_eval_suites WHERE suite_version='voc-regression-v1'")
        cases = [dict(r) for r in c.execute(text("SELECT case_id,body,expected_category FROM voc_eval_cases WHERE suite_version='voc-regression-v1' ORDER BY case_id")).mappings()]
        if not suite or len(cases)<6 or len(cases)!=suite['case_count'] or digest(cases)!=suite['sha256']:
            raise Conflict('separate regression suite missing or changed')
        return suite,cases

    def evaluate(self, proposal_id, key):
        with self.engine.begin() as c:
            self.kernel._lock(c)
            if self._idempotent(c,proposal_id,'evaluated',key):
                return False
            proposal = self._proposal(c,proposal_id)
            if proposal['status'] != 'DRAFT':
                raise Conflict('only a draft can be evaluated')
            state = one(c,"SELECT * FROM voc_method_state WHERE task_id='TASK-003' FOR UPDATE")
            if state['active_method_id'] != proposal['champion_version'] or state['version'] != proposal['champion_state_version']:
                raise Conflict('champion changed since proposal creation')
            suite,cases,champion,challenger = self._inputs(c,proposal)
            regression_suite,regression_cases = self._regression_inputs(c)
            case_results = []
            for case in cases:
                old = classify(case['body'],champion['rules'])
                new = classify(case['body'],challenger['rules'])
                case_results.append({'case_id':case['case_id'],'expected':case['expected_category'],
                    'champion_prediction':old,'challenger_prediction':new,
                    'champion_correct':old==case['expected_category'],'challenger_correct':new==case['expected_category']})
            old_correct = sum(x['champion_correct'] for x in case_results)
            new_correct = sum(x['challenger_correct'] for x in case_results)
            regressions = [x['case_id'] for x in case_results if x['champion_correct'] and not x['challenger_correct']]
            gains = [x['case_id'] for x in case_results if not x['champion_correct'] and x['challenger_correct']]
            regression_results = []
            for case in regression_cases:
                old = classify(case['body'],champion['rules'])
                new = classify(case['body'],challenger['rules'])
                regression_results.append({'case_id':case['case_id'],'expected':case['expected_category'],
                    'champion_prediction':old,'challenger_prediction':new,
                    'champion_correct':old==case['expected_category'],'challenger_correct':new==case['expected_category']})
            regression_losses = [x['case_id'] for x in regression_results if x['champion_correct'] and not x['challenger_correct']]
            passed = not regressions and not regression_losses and new_correct > old_correct
            results = {'case_count':len(cases),'champion_correct':old_correct,'challenger_correct':new_correct,
                       'champion_accuracy':round(old_correct/len(cases),4),'challenger_accuracy':round(new_correct/len(cases),4),
                       'regressions':regressions,'gains':gains,'cases':case_results,
                       'hard_gates':{'zero_regressions':not regressions,'accuracy_improved':new_correct>old_correct,
                                     'separate_suite_zero_regressions':not regression_losses},
                       'regression_suite':{'suite_version':regression_suite['suite_version'],'sha256':regression_suite['sha256'],
                           'case_count':len(regression_cases),'champion_correct':sum(x['champion_correct'] for x in regression_results),
                           'challenger_correct':sum(x['challenger_correct'] for x in regression_results),
                           'regressions':regression_losses,'cases':regression_results},
                       'evaluator_version':EVALUATOR_VERSION,
                       'scope':'DEMO fixed classification suite; not production quality'}
            evaluation_id = uid()
            self.kernel._event(c,'improvement.transition',proposal_id,{'from':'DRAFT','to':'EVALUATING'},cause=key)
            c.execute(text('''INSERT INTO improvement_evaluations(evaluation_id,proposal_id,suite_version,suite_sha256,champion_sha256,challenger_sha256,results,verdict)
                VALUES (:id,:proposal,:suite,:suite_hash,:old_hash,:new_hash,CAST(:results AS jsonb),:verdict)'''),
                {'id':evaluation_id,'proposal':proposal_id,'suite':suite['suite_version'],'suite_hash':suite['sha256'],
                 'old_hash':champion['sha256'],'new_hash':challenger['sha256'],'results':canonical(results),'verdict':'PASS' if passed else 'FAIL'})
            c.execute(text("UPDATE improvement_proposals SET status='PROPOSED',evaluation_id=:eval WHERE proposal_id=:id"), {'eval':evaluation_id,'id':proposal_id})
            self.kernel._event(c,'improvement.evaluated',proposal_id,{'from':'EVALUATING','to':'PROPOSED','evaluation_id':evaluation_id,
                'verdict':'PASS' if passed else 'FAIL','suite_version':suite['suite_version'],'suite_sha256':suite['sha256']},
                actor='evaluator',key=f'improvement.evaluated:{key}')
            return True

    def decide(self, proposal_id, action, key):
        if action not in ('apply','reject','rollback'):
            raise ValueError('unsupported decision')
        with self.engine.begin() as c:
            self.kernel._lock(c)
            if self._idempotent(c,proposal_id,action,key):
                return False
            proposal = self._proposal(c,proposal_id)
            state = one(c,"SELECT * FROM voc_method_state WHERE task_id='TASK-003' FOR UPDATE")
            if action in ('apply','reject') and proposal['status'] != 'PROPOSED':
                raise Conflict('proposal must be evaluated first')
            if action == 'apply':
                evaluation = one(c,'SELECT * FROM improvement_evaluations WHERE evaluation_id=:id', {'id':proposal['evaluation_id']})
                if not evaluation or evaluation['verdict'] != 'PASS':
                    raise Conflict('only an evaluation that passes all gates can be applied')
                suite,_,champion,challenger = self._inputs(c,proposal)
                regression_suite,_ = self._regression_inputs(c)
                if (state['active_method_id'] != proposal['champion_version'] or state['version'] != proposal['champion_state_version'] or suite['sha256'] != evaluation['suite_sha256']
                    or champion['sha256'] != evaluation['champion_sha256'] or challenger['sha256'] != evaluation['challenger_sha256']
                    or evaluation['results'].get('evaluator_version') != EVALUATOR_VERSION
                    or evaluation['results'].get('regression_suite',{}).get('sha256') != regression_suite['sha256']):
                    raise Conflict('champion, suite, or method version changed after evaluation')
                c.execute(text('''UPDATE voc_method_state SET active_method_id=:method,version=version+1,last_proposal_id=:proposal
                    WHERE task_id='TASK-003' '''), {'method':proposal['challenger_version'],'proposal':proposal_id})
                status = 'PROMOTED'
            elif action == 'reject':
                status = 'REJECTED'
            else:
                if proposal['status'] != 'PROMOTED' or str(state['last_proposal_id']) != proposal_id or state['active_method_id'] != proposal['challenger_version']:
                    raise Conflict('only the currently active promotion can be rolled back')
                c.execute(text('''UPDATE voc_method_state SET active_method_id=:method,version=version+1,last_proposal_id=NULL
                    WHERE task_id='TASK-003' '''), {'method':proposal['champion_version']})
                status = 'ROLLED_BACK'
            if action in ('apply','rollback'):
                self.kernel.policy_method_changed(c,proposal_id)
            c.execute(text('UPDATE improvement_proposals SET status=:status WHERE proposal_id=:id'), {'status':status,'id':proposal_id})
            self.kernel._event(c,f'improvement.{action}',proposal_id,{'from':proposal['status'],'to':status,
                'champion':proposal['champion_version'],'challenger':proposal['challenger_version']},actor='user',prior=state['version'],key=f'improvement.{action}:{key}')
            return True
