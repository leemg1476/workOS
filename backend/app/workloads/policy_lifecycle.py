"""Policy-specific follow-up actions after a verified work item is accepted."""
from sqlalchemy import text
from app.work.service import one
from app.workloads.registry import register_acceptance_hook



def schedule_policy_recheck(kernel, c, root_id, cause):
    child = one(c, '''SELECT w.work_id,w.status FROM policy_followups f JOIN work_items w ON w.work_id=f.work_id
        WHERE f.root_work_id=:root AND f.step_no=1''', {'root':root_id})
    if not child or child['status'] not in ('BLOCKED','STALE'):
        return False
    cleared = one(c, '''UPDATE work_dependencies SET pinned_artifact_version=NULL
        WHERE predecessor_id=:parent AND successor_id=:child AND pinned_artifact_version IS NOT NULL
        RETURNING predecessor_id''', {'parent':root_id,'child':child['work_id']})
    if cleared:
        kernel._event(c,'dependency.unpinned',child['work_id'],{'predecessor_id':str(root_id)},cause=cause)
    kernel._transition(c,child['work_id'],'READY',cause)
    c.execute(text('''INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING') ON CONFLICT(work_id)
        DO UPDATE SET status='PENDING',available_at=now(),run_id=NULL,lease_until=NULL,notified_at=NULL'''), {'id':child['work_id']})
    return True


def schedule_policy_index(kernel, c, root_id, cause):
    child = one(c, '''SELECT child.work_id,child.status,previous.work_id AS predecessor
        FROM policy_followups f JOIN work_items child ON child.work_id=f.work_id
        JOIN work_dependencies d ON d.successor_id=child.work_id
        JOIN work_items previous ON previous.work_id=d.predecessor_id AND previous.status='COMMITTED'
        JOIN work_dependencies pd ON pd.successor_id=previous.work_id
        JOIN work_items root ON root.work_id=pd.predecessor_id AND root.work_id=f.root_work_id AND root.status='COMMITTED'
            AND pd.pinned_artifact_version=root.accepted_artifact_id
        JOIN evidence e ON e.artifact_id=previous.accepted_artifact_id AND e.verification_type='policy_followup_recheck'
        WHERE f.root_work_id=:root AND f.step_no=2 AND e.result->>'passed'='true' ''', {'root':root_id})
    if not child or child['status'] not in ('BLOCKED','STALE'):
        return False
    cleared = one(c, '''UPDATE work_dependencies SET pinned_artifact_version=NULL
        WHERE predecessor_id=:parent AND successor_id=:child AND pinned_artifact_version IS NOT NULL
        RETURNING predecessor_id''', {'parent':child['predecessor'],'child':child['work_id']})
    if cleared:
        kernel._event(c,'dependency.unpinned',child['work_id'],{'predecessor_id':str(child['predecessor'])},cause=cause)
    kernel._transition(c,child['work_id'],'READY',cause)
    c.execute(text('''INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING') ON CONFLICT(work_id)
        DO UPDATE SET status='PENDING',available_at=now(),run_id=NULL,lease_until=NULL,notified_at=NULL'''), {'id':child['work_id']})
    return True


def schedule_policy_regression(kernel, c, root_id, cause, *, allow_rework=False):
    child = one(c, '''SELECT w.work_id,w.status,d.predecessor_id FROM policy_followups f
        JOIN work_items w ON w.work_id=f.work_id JOIN work_dependencies d ON d.successor_id=w.work_id
        JOIN work_items p ON p.work_id=d.predecessor_id
        JOIN evidence e ON e.artifact_id=p.accepted_artifact_id AND e.verification_type='policy_index_recheck'
        WHERE f.root_work_id=:root AND f.step_no=3 AND e.result->>'passed'='true' ''', {'root':root_id})
    allowed = ('BLOCKED','STALE','REWORK_REQUIRED') if allow_rework else ('BLOCKED','STALE')
    if not child or child['status'] not in allowed or not kernel._policy_regression_chain_current(c,child['work_id'],True):
        return False
    cleared = one(c, '''UPDATE work_dependencies SET pinned_artifact_version=NULL
        WHERE predecessor_id=:parent AND successor_id=:child AND pinned_artifact_version IS NOT NULL
        RETURNING predecessor_id''', {'parent':child['predecessor_id'],'child':child['work_id']})
    if cleared:
        kernel._event(c,'dependency.unpinned',child['work_id'],{'predecessor_id':str(child['predecessor_id'])},cause=cause)
    kernel._transition(c,child['work_id'],'READY',cause)
    c.execute(text('''INSERT INTO work_queue(work_id,status) VALUES (:id,'PENDING') ON CONFLICT(work_id)
        DO UPDATE SET status='PENDING',available_at=now(),run_id=NULL,lease_until=NULL,notified_at=NULL'''), {'id':child['work_id']})
    return True


def open_policy_review(kernel, c, root_id, cause):
    row = one(c, '''SELECT w.work_id,w.status,p.work_id AS predecessor FROM policy_followups f
        JOIN work_items w ON w.work_id=f.work_id JOIN work_dependencies d ON d.successor_id=w.work_id
        JOIN work_items p ON p.work_id=d.predecessor_id AND p.status='COMMITTED'
        JOIN evidence e ON e.artifact_id=p.accepted_artifact_id AND e.verification_type='policy_regression_check'
        WHERE f.root_work_id=:root AND f.step_no=4 AND e.result->>'passed'='true' ''',{'root':root_id})
    if not row or row['status']!='STALE' or not kernel._policy_regression_chain_current(c,row['predecessor']):
        return False
    kernel._transition(c,row['work_id'],'WAITING_APPROVAL',cause)
    return True


def policy_method_changed(kernel, c, cause):
    """Invalidate checks pinned to an old VOC method, preserving their history."""
    rows = c.execute(text('''SELECT w.work_id,w.status,f.root_work_id FROM policy_followups f
        JOIN work_items w ON w.work_id=f.work_id WHERE f.step_no=3 AND w.status IN ('COMMITTED','RUNNING')''')).mappings().all()
    for row in rows:
        if row['status']=='COMMITTED':
            kernel._transition(c,row['work_id'],'STALE',cause)
            kernel.stale_descendants(c,row['work_id'],cause)
        else:
            active = one(c,"SELECT run_id FROM runs WHERE work_id=:id AND status='RUNNING'",{'id':row['work_id']})
            if active:
                c.execute(text("UPDATE runs SET status='FAILED',error='VOC method changed',finished_at=now() WHERE run_id=:id"),{'id':active['run_id']})
                c.execute(text("UPDATE work_queue SET status='DONE',lease_until=NULL WHERE work_id=:id"),{'id':row['work_id']})
                kernel._event(c,'run.failed',active['run_id'],{'reason':'VOC method changed'},cause=cause)
            kernel._transition(c,row['work_id'],'REWORK_REQUIRED',cause)
        kernel._schedule_policy_regression(c,row['root_work_id'],cause,allow_rework=True)


def recover_policy_followups(kernel):
    """Schedule eligible older policy analyses left blocked before follow-up support existed."""
    with kernel.engine.begin() as c:
        kernel._lock(c)
        roots = c.execute(text('''SELECT w.work_id,a.run_id FROM policy_change_events p
            JOIN work_items w ON w.work_id=p.root_work_id AND w.status='COMMITTED'
            JOIN artifacts a ON a.artifact_id=w.accepted_artifact_id
            JOIN runs r ON r.run_id=a.run_id AND r.status='SUCCEEDED'
            JOIN evidence e ON e.artifact_id=a.artifact_id AND e.verification_type='policy_deterministic'
            WHERE e.result->>'passed'='true' ''')).mappings().all()
        count = sum(kernel._schedule_policy_recheck(c,row['work_id'],str(row['run_id'])) for row in roots)
        count += sum(kernel._schedule_policy_index(c,row['work_id'],str(row['run_id'])) for row in roots)
        count += sum(kernel._schedule_policy_regression(c,row['work_id'],str(row['run_id'])) for row in roots)
        count += sum(kernel._open_policy_review(c,row['work_id'],str(row['run_id'])) for row in roots)
        return count



def impact_accepted(kernel, c, work, run_id):
    if work['parent_work_id'] is None:
        schedule_policy_recheck(kernel,c,work['work_id'],run_id)


def followup_accepted(kernel, c, work, run_id):
    schedule_policy_index(kernel,c,work['parent_work_id'],run_id)


def index_accepted(kernel, c, work, run_id):
    schedule_policy_regression(kernel,c,work['parent_work_id'],run_id)


def regression_accepted(kernel, c, work, run_id):
    open_policy_review(kernel,c,work['parent_work_id'],run_id)


register_acceptance_hook('policy_impact_v1',impact_accepted)
register_acceptance_hook('policy_followup_verification_v1',followup_accepted)
register_acceptance_hook('policy_index_v1',index_accepted)
register_acceptance_hook('policy_regression_v1',regression_accepted)
