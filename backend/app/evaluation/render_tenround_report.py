"""Render the completed ten-round observation report; no model calls."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

OUT=Path('docs/experiments/household-tenround-20261108')
def load(name):return json.loads((OUT/name).read_text(encoding='utf-8'))

def main():
    summary=load('summary.json');rows=summary['rounds'];checks=load('final_checks.json')
    if len(rows)!=10 or any(not r['operator_review'] for r in rows):raise RuntimeError('ten reviewed reports required')
    font=Path('C:/Windows/Fonts/malgun.ttf')
    if font.exists():font_manager.fontManager.addfont(str(font));plt.rcParams['font.family']='Malgun Gothic'
    fig,ax=plt.subplots(figsize=(10,4.2),layout='constrained')
    x=[r['round'] for r in rows];y=[r['score'] for r in rows]
    ax.plot(x,y,color='#176B87',marker='o',linewidth=2)
    ax.scatter([1],[y[0]],color='#CE752B',s=70,zorder=3)
    for a,b in zip(x,y):ax.annotate(str(int(b)),(a,b),xytext=(0,9),textcoords='offset points',ha='center',fontsize=10)
    ax.set(xlim=(.6,10.4),ylim=(0,100),xticks=x,xlabel='회차',ylabel='AI 참고점수 / 100',
           title='10회 실험: 점수는 단조 상승하지 않음')
    ax.grid(axis='y',alpha=.2);ax.spines[['top','right']].set_visible(False)
    fig.text(.12,.18,'1회: 평가자 독해 오류 포함 · 동일 모델의 회차당 평가 1회 · 인간 전문가 점수 아님',fontsize=9,color='#555555')
    fig.savefig(OUT/'score_curve.png',dpi=160);plt.close(fig)
    labels=[
        '기준 보고서. 정확한 계산표는 있으나 제목 중복·막연한 행동 안내.',
        '서론에 점검 우선순위, 8월 감면 근거 추가. 중복 제목은 남음.',
        '중복 제목 제거, 가구 기준지출 복원, 선택형 지출 목표 산식 추가.',
        '식품 비중이 가구별 차이를 만드는 이유를 본문에서 설명.',
        '8월→9월 원인 흐름, 이자 가정, 같은 기간의 지출 비교 보강.',
        '할인 종료·사용량·단가·수량을 구분하는 조건별 행동 추가.',
        '이자 산식 추가. 생활비 산식은 미반영. 평가점수는 크게 하락.',
        '산식 피드백을 필수항목으로 전환해 생활비 산식·차이 의미 반영.',
        '통신비 설명의 직접 원문 연결 복원. 링크와 분량 과다.',
        'CPI 기준액 42,000원 추가, 링크·분량 정리. 내부 표식 노출은 남음.'
    ]
    lines=['# 태스크 기억 개선 후 10회 실험 비교 보고서','',
        '작성일: 2026-11-08 · 대상: 2025년 9월 우리 집 체감물가와 생활비 대응 · 작성/검토: Codex','',
        '**판정: 승인 지식의 지속 유지와 보고서의 구체화는 확인했다. 회차마다 자동으로 더 좋아지거나 전문가 수준으로 발전한다는 주장은 입증하지 못했다.**','',
        '10개 보고서의 계산·구조 검사가 통과했고 초기 교훈은 10회까지 유지됐다. 통계 요약 위주의 첫 보고서에 조건별 행동, 가정, 산식과 비교 기준이 추가됐다. 반면 피드백을 저장해도 본문에서 빠지는 경우가 있었고, 평가자 오독·Codex 교훈 오류·최종 내부 표식 노출도 관찰됐다.','',
        '## 실험 조건','',
        '- 같은 2025-09-01~09-30, 같은 A/B 가상 가구 입력, 같은 작성 계약·루브릭·gpt-6-luna를 사용했다. 실제 사용자 생활비 자료가 아니다.',
        '- 새 전용 DB와 새 Task에서 작성10회·분리된 AI 평가10회를 수행했다. 각 회차를 Codex가 읽고 근거를 확인한 피드백만 다음 회차에 적용했다. 이는 인간 전문가 검토가 아니다.',
        '- 사용자가 이번 실험에 한정하여 적용을 명시적으로 승인했다. 초기 계산7개·필수항목3개를 적용하고, 1회 뒤 새 Task에서 수집한 원문 인용1개를 추가했다. 과거42개 보고서·평가 의견은 주입하지 않았다. 초기 방법은 앞선 실험의 교훈을 사용하므로 무경험 기준선은 아니다.',
        '- 8회부터 새 체크리스트1개, 10회에 새 계산1개를 적용했다. 검색 결과와 적용 지식이 바뀌므로 순수 기억 효과나 동일 방법의 성능 비교는 아니다. 사전 계획과 개입 시점은 [PROTOCOL.md](PROTOCOL.md)에 남겼다.',
        '- 유효 보고서10개를 얻기까지 작성 Run은12개였다. 최초 JSON 검증 실패1건과 ValueError로 끝난 실행 실패1건을 보존했다. 후자는 상세 응답/오류 메시지가 저장되지 않아 구체적 원인을 확정할 수 없다. 실패한 초안을 성공 회차로 집계하지 않았다.','',
        '## 회차별 달라진 점','',
        '|회차|AI 참고점수|고정된 활성 지식|원문 문자 수(링크·표 포함)|전 회차 대비 관찰|',
        '|---|---:|---:|---:|---|']
    for r,label in zip(rows,labels):
        n=r['round'];lines.append(f"|[{n}회 원문](round-{n:02d}/report.md)|{r['score']:g}{'*' if n==1 else ''}|{r['active_knowledge']}|{r['chars']:,}|{label}|")
    lines+=['','*1회 62점에는 평가자가 올바른 하락 문장을 상승으로 오독한 감점이 포함된다. 점수는 수정하지 않았다. 2회와 비슷한 표현인데 사실 평가가 2/5→5/5로 달라졌으므로 21점 상승 전부를 개선으로 해석하면 안 된다.','',
        '![AI 참고점수 추이](score_curve.png)','',
        '평균은83.5점이며, 6회와10회가90점으로 같다. 2~5회 평균84.5점, 6~10회 평균87.0점이라는 차이는 있지만 한 번씩의 같은 모델 평가·적응 개입·변동 검색이 섞여 통계적 향상이나 인과효과로 볼 수 없다. 6→7회 90→80점도 확인된 산술 오류 없이 발생했다. 원점수와 [차원별 평가·근거](summary.json)를 함께 보아야 한다.','',
        '## 무엇이 실제로 개선됐나','',
        '**읽고 행동하기:** 3회부터 중복 제목이 사라졌다. 막연한 기록 권고에 선택지출 목표·기간·점검 조건이 붙었고, 6회에는 할인 종료와 사용량, 식품의 단가와 구매량을 분리하도록 안내했다. 링크·표를 포함한 원문은10회가9회보다 약22.7% 짧아졌다. 이는 화면에 보이는 순수 본문 길이와 다르며, 최종본도 통계 문단의 밀도와 가독성 문제가 남았다.','',
        '**검토하기:** 5회부터 대출 스트레스의 원금·금리와 제외항목이 명시됐고, 7회에는 이자 산식이 들어갔다. 8회부터는 생활비 산식, 가중률, CPI 비교액, 차이가 양수라는 의미가 별도 절로 정리됐다. 10회는 검산한 CPI 비교 기준액42,000원을 백엔드 표에 추가했다.','',
        '**유지하기:** 첫 교훈이10회까지 실제 작성 입력에 남았다. 활성 지식은10→20개로 늘었고 모든 보고서의 초기 계산7개는 정확한 표 값으로 유지됐다. 다만 계산표는 코드가 제공하므로 모델이 산술 능력을 학습한 증거는 아니다. 지식 개수 증가 자체도 품질 점수가 아니다.','',
        '**자료 찾기:** 2회부터8월 원문을 근거로 한 달 감면 종료를 설명할 수 있었다. 이후 가중치·개인 체감 안내와 여러 동향 페이지를 검색했지만 후반의 새 검색이 새로운 경제적 발견으로 이어진 경우는 확인하지 못했다. 자료 수보다 주장과 직접 연결되는 원문 선택이 중요했다. 핵심 원문은 [9월 공식 브리핑](https://www.korea.kr/news/policyNewsView.do?newsId=156721918)과 [8월 공식 브리핑](https://www.korea.kr/news/policyNewsView.do?newsId=156721800)이다.','',
        '## 남은 문제와 검토자의 오류','',
        '1. **지식 보존과 실행은 다르다.** 생활비 산식을 설명하라는 교훈이7회에도 누락됐다. 이를8회부터 필수항목으로 바꾸자 본문에 반영됐다. 이번에는 저장한 의견의 수보다 실행 가능한 규칙으로 바꾼 개입이 더 뚜렷한 변화를 만들었다. 단일 사례 관찰이다.',
        '2. **AI 비평을 그대로 학습시키면 안 된다.** 1회 평가자의 부호 오독은 기각했다. 중대오류 표시는 AI가1건 제기했지만 Codex 대조에서는 해당 문장이 원문과 같은 의미여서 확인 오류로 인정하지 않았다. 그렇다고 보고서 전체를 독립 검증 완료했다고 주장하지 않는다.',
        '3. **Codex의 검토도 틀릴 수 있다.** 6회 뒤 교훈에 제가 같은 단가라고 잘못 적었다. 고정한 것은 품목 구성·수량이며 가격은 변한다. 해당 지식을 철회·교정했고 이미 고정된7회 입력은 보존했다. 7회 본문은 이 오류를 반복하지 않았다. [노출 Run과 교정 이력](round-06/operator_correction.json)을 공개한다.',
        '4. **정확한 계산도 편집 검사가 필요하다.** 10회 비교액은42,000원으로 맞지만 행 제목이 검토 교훈09라는 내부 이름으로 출력됐다. 일반 독자를 위한 이름으로 바꾸어야 한다. 평가자도 이를 지적했다. 원본 실험 보고서는 고쳐 쓰지 않았다.',
        '5. **원문 URL은 검증 완료 표지가 아니다.** 읽을 수 없는 PDF·다른 페이지·후행 readback을 구분했다. 생성 당시 평가에 쓴 자료는 각 회차 review_context.json, 이후 추가 읽기는 별도 기록에 남았다.','',
        '## 회차별 검토 상세','']
    for r in rows:
        n=r['round'];v=r['operator_review'];lines += [f'### {n}회 — {r["score"]:g}점(참고)','',
            '**직전 피드백 반영:** '+v['feedback_reflection'],'',
            '**남은 약점·퇴행:** '+v['regressions'],'',
            '**평가 의견 검토:** '+v['judge_audit'],'',
            f'[보고서](round-{n:02d}/report.md) · [AI 평가 원본](round-{n:02d}/grade.json) · [Codex 검토·적용 기록](round-{n:02d}/operator_review.json)','']
    lines+=['## 완료와 검증 범위','',
        f'정확히10개 Work, 작성 Run{checks["writer_runs"]}개, 성공10개·실패2개, 큐10개 모두 DONE이다. 보고서 Work는 모두 WAITING_APPROVAL이며 공식 채택이나 기존 운영 방법 승격을 하지 않았다. 새 Task는 USER-545faf8c-091f-462f-a834-86c909606fd7이고 연구 상태는 STOPPED_AFTER_COMPLETING_AUTHORIZED_TEN_ROUNDS로 잠갔다. 11회차는 없다.','',
        '실제 DB·파일 대조에서10개 Artifact 해시, 같은 입력·작성 계약, 초기 계산값 유지, 오래된 교훈 유지, 미승인 AI 의견의 실제 작성 프롬프트 제외를 확인했다. 기존42개 Artifact의 해시도 모두 일치했고 원래 Task에 적용한 지식은0개다. [최종 검사](final_checks.json) · [모든 시도](attempts.json) · [원 입력](source.json) · [루브릭](rubric.json).','',
        '이번 추가 실행 도구는 py_compile과 중단 연구 실행 차단 회귀1개(1 passed, 6 deselected)로 확인했다. 앞선 전체115개 테스트를 이번 요청에서 재실행한 것으로 표시하지 않는다.','',
        '**사용자 의도와의 일치:** 같은 태스크의 기억을 유지하고 검토를 다음 결과에 반영하는 경로는 작동했다. 그러나 Codex의 회차별 개입, 구조 규칙 강화, 코드 계산이 포함돼 무개입 자율 발전의 입증은 아니다. 여러 태스크 간 격리, 다른 가구로의 일반화, 인간 전문가 대비 품질은 이번10회로 평가하지 않았다.','',
        '**다음 개선 우선순위:** 독자용 계산 이름 검사 → 교훈을 실행 항목으로 전환하는 기준 → 검토자 오류를 잡는 원문 대조 사례 → 다른 가구와 무기억 대조·사람 평가 순서가 적절하다. 이번10회 실험은 여기서 끝낸다.','']
    (OUT/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print('REPORT.md and score_curve.png rendered; no model calls')

if __name__=='__main__':main()
