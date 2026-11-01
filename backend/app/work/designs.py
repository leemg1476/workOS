"""Draft-only natural-language work design; no generated code or automatic execution."""
import json
import os
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, Field
from sqlalchemy import text


class DesignStep(BaseModel):
    id: str
    title: str
    purpose: str
    depends_on: list[str]


class WorkPlan(BaseModel):
    name: str
    objective: str
    expected_result: str
    evaluation_criteria: list[str]
    steps: list[DesignStep]
    open_questions: list[str]


class CreateDesign(BaseModel):
    description: str = Field(min_length=1, max_length=5000)
    mode: Literal['MOCK', 'LIVE_LLM'] = 'MOCK'


class EditDesign(BaseModel):
    revision: int = Field(ge=1)
    plan: WorkPlan


class DesignFeedback(BaseModel):
    revision: int = Field(ge=1)
    feedback: str = Field(min_length=1, max_length=3000)


def validate_plan(plan):
    if not all([plan.name.strip(), plan.objective.strip(), plan.expected_result.strip()]):
        raise ValueError('name, objective and expected result are required')
    if not 1 <= len(plan.steps) <= 12 or not 1 <= len(plan.evaluation_criteria) <= 12:
        raise ValueError('one to twelve steps and evaluation criteria are required')
    if any(not x.strip() for x in plan.evaluation_criteria):
        raise ValueError('empty evaluation criterion')
    ids = [s.id for s in plan.steps]
    if len(set(ids)) != len(ids) or any(not s.id.strip() or not s.title.strip() or not s.purpose.strip() for s in plan.steps):
        raise ValueError('steps require unique IDs, titles and purposes')
    graph = {s.id: s.depends_on for s in plan.steps}
    if any(d not in graph for deps in graph.values() for d in deps):
        raise ValueError('unknown dependency')
    done = set()
    def visit(node, path):
        if node in done:
            return
        if node in path:
            raise ValueError('cyclic dependencies')
        for parent in graph[node]:
            visit(parent, path | {node})
        done.add(node)
    for node in ids:
        visit(node, set())
    return plan


def generate(description, mode):
    if not description.strip():
        raise ValueError('description is required')
    if mode == 'MOCK':
        plan = WorkPlan(name='업무 초안', objective=description.strip(), expected_result='사람이 기대 결과를 입력해야 합니다.',
            evaluation_criteria=['사람이 검증 가능한 기준을 추가해야 합니다.'],
            steps=[DesignStep(id='collect',title='입력 확인',purpose='필요한 자료와 권한을 확인합니다.',depends_on=[]),
                   DesignStep(id='produce',title='결과 작성',purpose='확인된 자료를 바탕으로 결과를 작성합니다.',depends_on=['collect']),
                   DesignStep(id='review',title='검토',purpose='결과와 평가 기준을 대조합니다.',depends_on=['produce'])],
            open_questions=['필요한 입력과 데이터 출처는 무엇인가요?', '어떤 결과를 성공으로 볼 수 있나요?'])
        return validate_plan(plan), {'origin':'MOCK','model_id':None,'response_id':None}
    key = os.getenv('openai_api_key') or os.getenv('OPENAI_API_KEY')
    model = os.getenv('openai_model') or os.getenv('OPENAI_MODEL')
    if not key or not model:
        raise ValueError('LIVE_LLM requires configured API key and model')
    from openai import OpenAI
    response = OpenAI(api_key=key,timeout=45,max_retries=0).responses.parse(
        model=model, store=False, max_output_tokens=2200, text_format=WorkPlan,
        input=[{'role':'system','content':'사용자의 업무 설명을 검토 가능한 업무 초안으로 구조화하세요. 한국어로 답하고 단계 ID는 영문 식별자, depends_on은 선행 단계 ID 배열입니다. 순환을 만들지 마세요. 데이터 출처나 평가 기준을 지어내지 말고 불명확한 내용은 open_questions에 기록하세요. 자동 실행이나 검증이 완료됐다고 주장하지 마세요.'},
               {'role':'user','content':description}])
    if response.status != 'completed' or response.output_parsed is None:
        raise ValueError('model did not return a completed work plan')
    return validate_plan(response.output_parsed), {'origin':'LIVE_LLM','model_id':response.model,'response_id':response.id}


def create(db, description, plan, meta):
    design_id = str(uuid4())
    with db.begin() as c:
        c.execute(text('INSERT INTO work_designs(design_id) VALUES (:id)'), {'id':design_id})
        c.execute(text('''INSERT INTO work_design_revisions(design_id,revision,description,plan,origin,model_id,response_id)
            VALUES (:id,1,:description,CAST(:plan AS jsonb),:origin,:model_id,:response_id)'''),
            {'id':design_id,'description':description,'plan':json.dumps(plan.model_dump(),ensure_ascii=False),**meta})
    return design_id


def revise(db, design_id, revision, plan, meta=None, feedback=None):
    validate_plan(plan)
    with db.begin() as c:
        parent = c.execute(text('SELECT design_id FROM work_designs WHERE design_id=CAST(:id AS uuid) FOR UPDATE'),{'id':design_id}).first()
        if not parent:
            return None
        current = c.execute(text('SELECT * FROM work_design_revisions WHERE design_id=CAST(:id AS uuid) ORDER BY revision DESC LIMIT 1'),{'id':design_id}).mappings().one()
        if current['revision'] != revision:
            raise RuntimeError('revision conflict')
        meta = meta or {'origin':'HUMAN_EDIT','model_id':None,'response_id':None}
        c.execute(text('''INSERT INTO work_design_revisions(design_id,revision,description,plan,origin,model_id,response_id,feedback)
            VALUES (CAST(:id AS uuid),:revision,:description,CAST(:plan AS jsonb),:origin,:model_id,:response_id,:feedback)'''),
            {'id':design_id,'revision':revision+1,'description':current['description'],
             'plan':json.dumps(plan.model_dump(),ensure_ascii=False),'feedback':feedback,**meta})
        return revision+1


def apply_feedback(plan, feedback):
    if not feedback.strip():
        raise ValueError('feedback is required')
    key = os.getenv('openai_api_key') or os.getenv('OPENAI_API_KEY')
    model = os.getenv('openai_model') or os.getenv('OPENAI_MODEL')
    if not key or not model:
        raise ValueError('LIVE_LLM requires configured API key and model')
    from openai import OpenAI
    response = OpenAI(api_key=key,timeout=45,max_retries=0).responses.parse(
        model=model,store=False,max_output_tokens=2200,text_format=WorkPlan,
        input=[{'role':'system','content':'사람의 업무 구조 변경 요청을 반영해 새 초안을 작성하세요. 한국어로 답하고 단계 ID와 depends_on은 유효한 비순환 그래프여야 합니다. 확인되지 않은 데이터와 능력을 지어내지 말고 open_questions에 남기세요. 평가를 통과하거나 실제 업무가 개선됐다고 주장하지 마세요.'},
               {'role':'user','content':json.dumps({'existing_plan':plan,'feedback':feedback},ensure_ascii=False)}])
    if response.status != 'completed' or response.output_parsed is None:
        raise ValueError('model did not return a completed work plan')
    return validate_plan(response.output_parsed), {'origin':'HUMAN_FEEDBACK_LLM','model_id':response.model,'response_id':response.id}
