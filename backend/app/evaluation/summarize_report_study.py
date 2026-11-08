"""Summarize observed study records only; never invent missing checkpoints."""
import argparse
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from statistics import mean
from html import escape
from urllib.parse import urlsplit, parse_qs, urlencode
from app.domain.hashing import digest


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def source_identity(url):
    parsed=urlsplit(url);host=parsed.hostname or '';host=host.removeprefix('www.').removeprefix('m.')
    query=parse_qs(parsed.query)
    if host=='korea.kr' and query.get('newsId'):return 'korea.kr/article/'+query['newsId'][0]
    if host in ('kostat.go.kr','mods.go.kr'):
        if query.get('bid') and query.get('list_no'):return 'mods.go.kr/board/'+query['bid'][0]+'/'+query['list_no'][0]
        if query.get('mid'):return 'mods.go.kr/menu/'+query['mid'][0]
    cleaned={k:v for k,v in query.items() if not k.startswith('utm_')}
    return host+parsed.path+'?'+urlencode(cleaned,doseq=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
    root=Path(args.directory);study=read(root/'study.json');rubric=read(root/'rubric.json')
    rows=[];seen=set();raw_seen=set();search_seen=set();ledger=[];models=set()
    for folder in sorted(root.glob('round-*')):
        if not (folder/'grade.json').exists():continue
        report=read(folder/'report.json');grade=read(folder/'grade.json');step=report['content']['steps'][0]
        if digest(report['content'])!=report['artifact_sha256']:raise ValueError('artifact hash changed')
        if digest(report['memory']['manifest'])!=report['memory']['sha256']:raise ValueError('memory hash changed')
        if grade['rubric_sha256']!=study['rubric_sha256']:raise ValueError('rubric hash changed')
        research=step.get('research',{});urls=set()
        for citation in research.get('citations',[]):urls.add(citation['url'])
        urls.update(re.findall(r'\]\((https?://[^\s)]+)\)',step['text']))
        searched=set()
        for call in research.get('web_calls',[]):
            action=call.get('action',{})
            searched.update(s['url'] for s in action.get('sources',[]) if s.get('url'))
            if action.get('type')=='open_page' and action.get('url'):searched.add(action['url'])
        identities={source_identity(url) for url in urls}
        new=identities-seen;seen.update(identities);raw_seen.update(urls);search_seen.update(searched)
        for identity in sorted(new):ledger.append({'first_cited_round':report['round'],'source_identity':identity,
             'urls':sorted(url for url in urls if source_identity(url)==identity),'scope':'NORMALIZED_CITATION_TARGET_NOT_FACT_CHECKED'})
        models.add(step['model_id'])
        rows.append({'round':report['round'],'score':grade['score'],'ratings':grade['ratings'],
            'critical_errors':grade['critical_errors'],'web_output_items':len(research.get('web_calls',[])),
            'cited_urls':len(urls),'distinct_citation_targets':len(identities),'new_cited_urls':len(new),'cumulative_cited_urls':len(seen),
            'memory_review_count':len(report['memory']['manifest']['reviews']),
            'memory_draft_count':len(report['memory']['manifest'].get('draft_results',[])),
            'writer_input_tokens':step['input_tokens'],'writer_output_tokens':step['output_tokens'],
            'judge_input_tokens':grade['input_tokens'],'judge_output_tokens':grade['output_tokens'],
            'writer_elapsed_ms':step['elapsed_ms'],'work_id':report['work_id'],'run_id':report['run_id'],
            'artifact_id':report['artifact_id'],'artifact_sha256':report['artifact_sha256'],
            'memory_sha256':report['memory']['sha256'],'report_path':str(folder/'report.md')})
    checkpoint_targets=(1,10,30,50)
    if rows and study.get('status','').startswith('STOPPED'):
        checkpoint_targets=tuple(sorted(set(checkpoint_targets+(rows[-1]['round'],))))
    checkpoints=[r for r in rows if r['round'] in checkpoint_targets]
    summary={'study':study,'generated_at':datetime.now(timezone.utc).isoformat(),'completed_rounds':len(rows),
             'checkpoints':checkpoints,'rounds':rows,'returned_writer_models':sorted(models),
             'cited_url_count':len(raw_seen),'normalized_citation_target_count':len(seen),'search_result_url_count':len(search_seen),
             'average_score':mean(r['score'] for r in rows) if rows else None,
             'score_regressions':[{'from_round':a['round'],'to_round':b['round'],'delta':b['score']-a['score']}
                                  for a,b in zip(rows,rows[1:]) if b['score']<a['score']],
             'token_totals':{k:sum(r[k] or 0 for r in rows) for k in ('writer_input_tokens','writer_output_tokens','judge_input_tokens','judge_output_tokens')},
             'interpretation_limits':['one adaptive trajectory on one fixed scenario','same-model automated judge; no human calibration',
                 'additional web information and feedback co-vary; not a causal memory ablation',
                 'source URLs and provider search provenance are not full immutable page snapshots',
                 'high rubric scores do not prove expert human parity or generalization']}
    (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'source_ledger.json').write_text(json.dumps(ledger,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=[f'# 2025년 9월 가계 생활비 보고서: {len(rows)}회 완료 품질평가',
           '',f"등록 Task: `{study['task_id']}` · 실제 작성 모델: {', '.join(sorted(models))} · 완료: {len(rows)}/50회",'',
           '분석 대상은 2025-09-01~2025-09-30으로 고정했다. 매회 웹 자료를 추가 탐색하고 이전 초안·AI 피드백을 태스크 기억에서 재사용했다. 점수는 같은 모델의 별도 요청으로 고정 루브릭을 적용한 참고 평가이며, 사람의 전문가 평가나 인과적 학습효과의 증명이 아니다.',
           '', '| 회차 | AI 점수 /100 | AI 중대오류 경고(미확정) | 본문 출처 URL | 최근 피드백/초안 기억 | 원문 |',
           '|---:|---:|---:|---:|---:|---|']
    by_round={r['round']:r for r in rows}
    for n in checkpoint_targets:
        r=by_round.get(n)
        if r:lines.append(f"| {n} | {r['score']:.0f} | {len(r['critical_errors'])} | {r['cited_urls']} | {r['memory_review_count']}/{r['memory_draft_count']} | [보고서](round-{n:02d}/report.md) · [평가](round-{n:02d}/grade.json) |")
        else:lines.append(f'| {n} | 미완료 | — | — | — | — |')
    lines+=['','## 항목별 평가(0~5)','', '| 항목 / 배점 | '+' | '.join(str(n)+'회차' for n in (1,10,30,50))+' |','|---|'+'---:|'*4]
    for key,weight in rubric['weights'].items():
        lines.append('| '+key+f' / {weight}'+' | '+' | '.join(str(by_round[n]['ratings'][key]) if n in by_round else '—' for n in (1,10,30,50))+' |')
    lines+=['','## 해석의 경계','',*['- '+s for s in summary['interpretation_limits']], '',
            f"실제 본문의 원시 URL {len(raw_seen)}개, 추적 파라미터와 같은 기사·파일의 경로 중복을 정규화한 인용 대상 {len(seen)}개. 검색 결과 URL {len(search_seen)}개는 본문에서 인용·이해했다는 뜻이 아니다. 정규화는 문서 내용의 독립성이나 정확성을 검증하지 않으며 출처 개수를 품질 점수로 환산하지 않았다.",
            '', '산술 기준은 [고정 계산 기준](arithmetic_reference.json), 평가 기준은 [루브릭](rubric.json), 검색 출처의 최초 본문 등장은 [출처 장부](source_ledger.json), 전 회차 지표와 해시는 [원본 집계](summary.json)에 보존했다.']
    (root/'COMPARISON.md').write_text('\n'.join(lines),encoding='utf-8')
    if rows:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.font_manager import FontProperties
        font=FontProperties(fname='C:/Windows/Fonts/malgun.ttf')
        fig,ax=plt.subplots(figsize=(10,4.5),layout='constrained');fig.set_facecolor('#f7f6ed');ax.set_facecolor('#f7f6ed')
        ax.plot([r['round'] for r in rows],[r['score'] for r in rows],color='#355b48',linewidth=2,marker='.',markersize=4)
        for r in checkpoints:
            ax.scatter(r['round'],r['score'],s=70,color='#ad6938',zorder=5)
            ax.annotate(f"{r['round']}회 {r['score']:.0f}점",(r['round'],r['score']),xytext=(3,-20 if r['round']>1 else 12),textcoords='offset points',fontproperties=font,fontsize=10)
        ax.set(xlim=(0,51),ylim=(0,100),xlabel='Iteration',ylabel='Fixed-rubric score / 100')
        ax.set_title('동일 기간 보고서 · 웹 검색 + 태스크 기억 + 매회 AI 피드백',fontproperties=font,pad=14)
        ax.grid(axis='y',alpha=.18);ax.spines[['top','right']].set_visible(False)
        fig.savefig(root/'quality_curve.png',dpi=160);plt.close(fig)
    print(json.dumps({'completed':len(rows),'checkpoints':[(r['round'],r['score']) for r in checkpoints],
                      'regressions':len(summary['score_regressions']),'cited_urls':len(seen)},ensure_ascii=False))


if __name__=='__main__':main()
