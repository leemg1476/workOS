<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'
import TaskKnowledge from './TaskKnowledge.vue'
const props=defineProps({base:String,tasks:Array})
const emit=defineEmits(['open-work','changed'])
const selected=ref(new URLSearchParams(window.location.search).get('task') || ''), data=ref(null), error=ref(''), busy=ref(false), filter=ref('ALL')
const labels={QUEUED:'실행 대기',IN_PROGRESS:'진행 중',REVIEW:'결과 검토',RETRY:'보완 후 이어하기',REVALIDATE:'변경 근거 확인',CHECK_DEPENDENCIES:'선행 작업 확인',RESULT_AVAILABLE:'확정 결과',CANCELLED:'취소됨',CHECK_STATUS:'상태 확인'}
const counts=computed(()=>({working:props.tasks.filter(t=>['READY','RUNNING'].includes(t.status)).length,
  attention:props.tasks.filter(t=>['WAITING_APPROVAL','REWORK_REQUIRED','STALE','BLOCKED'].includes(t.status)).length,
  complete:props.tasks.filter(t=>t.status==='COMMITTED').length}))
const shown=computed(()=>props.tasks.filter(t=>filter.value==='ALL' || (filter.value==='ATTENTION' ? ['WAITING_APPROVAL','REWORK_REQUIRED','STALE','BLOCKED'].includes(t.status) : t.status==='COMMITTED')))
const task=computed(()=>props.tasks.find(t=>t.task_id===selected.value))
let sequence=0
async function load(){
  const id=selected.value, seq=++sequence
  if(!id) return
  try{const r=await fetch(`${props.base}/tasks/${encodeURIComponent(id)}/memory`);if(!r.ok) throw Error('태스크 기억을 불러오지 못했습니다.');const value=await r.json();if(seq===sequence){data.value=value;error.value=''}}
  catch(e){if(seq===sequence) error.value=e.message}
}
watch(()=>props.tasks,()=>{if(!props.tasks.some(t=>t.task_id===selected.value) && props.tasks.length) selected.value=props.tasks[0].task_id;else load()})
watch(selected,()=>{data.value=null;load()})
onUnmounted(()=>{sequence++})
async function resume(work){
  busy.value=true
  try{const r=await fetch(`${props.base}/work/${work.work_id}/retry`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idempotency_key:crypto.randomUUID()})});const value=await r.json();if(!r.ok) throw Error(value.detail || '작업을 이어가지 못했습니다.');await load();emit('changed')}
  catch(e){error.value=e.message}finally{busy.value=false}
}
</script>
<template>
  <section class="task-workspace" aria-label="에이전트의 태스크 작업실">
    <div class="workspace-heading"><div><span class="eyebrow">ONE AGENT · TASK MEMORY</span><h2>기억하고, 이어서 완성하는 작업실</h2><p>태스크마다 목표와 결과, 실패와 검토 의견을 보존합니다. 다음 텍스트 실행은 같은 버전의 기억을 참고합니다.</p></div><div class="workspace-counts"><span><b>{{ counts.working }}</b>진행·대기</span><span><b>{{ counts.attention }}</b>확인 필요</span><span><b>{{ counts.complete }}</b>최근 작업 확정</span></div></div>
    <div class="workspace-layout"><div class="workspace-tasks"><label>태스크 보기<select v-model="filter"><option value="ALL">전체 태스크</option><option value="ATTENTION">확인 필요</option><option value="COMPLETE">최근 작업 확정</option></select></label><button v-for="t in shown" :key="t.task_id" :class="{chosen:selected===t.task_id}" @click="selected=t.task_id"><strong>{{ t.name }}</strong><small>{{ t.status || '시작 전' }} · v{{ t.version }} · {{ t.model_mode || 'DEMO DATA' }}</small></button><p v-if="!shown.length">해당 태스크가 없습니다.</p></div>
      <div class="workspace-memory" aria-live="polite"><p v-if="error" role="alert" class="error">{{ error }}</p><template v-if="data && task"><div class="workspace-heading"><div><span class="eyebrow">{{ task.task_id }} / V{{ data.task_version }}</span><h3>{{ task.name }}</h3></div><button class="secondary" @click="load">기억 새로고침</button></div><p>{{ task.objective }}</p>
        <div class="workspace-memory-stats"><span>채택 결과 <b>{{ data.memory.accepted_results.length }}</b></span><span>최근 실패 <b>{{ data.memory.failures.length }}</b></span><span>검토 의견 <b>{{ data.memory.reviews.length }}</b></span></div>
        <p class="detail-note">현재 버전 · 채택 결과 최대 2개, 실패와 검토 각 3개. 최근 초안은 승인 전 참고자료입니다. 과거 결과는 현재 원천의 사실을 보증하지 않습니다. MOCK은 실제 업무 결과가 아닙니다.</p>
        <TaskKnowledge :base="base" :task-id="selected" @changed="load" @open-work="(work,run)=>emit('open-work',work,run)" />
        <h4>이어갈 작업</h4><div v-for="w in data.recent_work" :key="w.work_id" class="workspace-work"><div><b>{{ labels[w.next_action] }}</b><small>업무 v{{ w.task_version }} · {{ w.status }} · 실행 {{ w.run_status || '대기' }}</small><small>{{ w.work_id }}</small></div><button v-if="w.next_action==='RETRY'" :disabled="busy" @click="resume(w)">같은 작업 이어하기</button><button class="secondary" @click="emit('open-work',w.work_id,w.run_id)">{{ w.next_action==='RESULT_AVAILABLE' ? '결과 보기' : '상태·근거 보기' }}</button></div><p v-if="!data.recent_work.length" class="detail-note">아직 작업이 없습니다. 아래 업무 목록에서 실행을 요청하세요.</p>
        <details><summary>다음 실행에서 참고할 기억</summary><div v-for="d in data.memory.draft_results || []" :key="d.artifact_id" class="workspace-record"><b>이전 초안 · 승인되지 않음</b><pre>{{ d.excerpt }}{{ d.truncated ? "…" : "" }}</pre></div><div v-for="a in data.memory.accepted_results" :key="a.artifact_id" class="workspace-record"><b>채택된 결과 · {{ a.model_id }}</b><small>검증기 {{ a.verifier_version }} · 의미 검토 {{ a.semantic_review }}</small><button class="secondary" @click="emit('open-work',a.work_id,a.run_id)">결과와 검증 근거</button><pre>{{ a.excerpt }}{{ a.truncated ? '…' : '' }}</pre></div><div v-for="f in data.memory.failures" :key="f.run_id" class="workspace-record"><b>{{ f.status }}</b><p>{{ f.reason }}</p></div><div v-for="r in data.memory.reviews" :key="r.event_id" class="workspace-record"><b>{{ r.scope==='AUTOMATED_OPINION_NOT_HUMAN_APPROVAL' ? 'AI 평가 의견 · 미승인' : ('사용자 ' + (r.decision==='ACCEPT' ? '채택' : '보완 요청')) }}</b><p>{{ r.note }}</p></div></details>
        <p class="detail-note">실행 시점의 기억은 해시와 함께 별도 보존됩니다. 워커 중단은 lease로 복구하고, 일시적인 모델 연결·서버 오류는 같은 작업에서 총 3회까지 자동 시도합니다. 그 외 실패는 확인 후 이어갑니다. 방법 변경은 고정 평가와 사용자 적용을 거칩니다.</p>
      </template><p v-else-if="!error">{{ selected ? '태스크 기억을 불러오는 중…' : '등록한 태스크가 여기에 표시됩니다.' }}</p></div></div>
  </section>
</template>
<style scoped>
.task-workspace{margin:28px 0;padding:26px;background:#fffef8;border:1px solid #cbd5c6;border-top:4px solid #34573f}.workspace-heading{display:flex;justify-content:space-between;align-items:start;gap:20px}.workspace-heading h2{font-size:23px;letter-spacing:-.04em;margin:10px 0}.workspace-heading p,.workspace-memory>p{font-size:12px;line-height:1.8;color:#596d5e}.workspace-counts{display:flex;gap:22px;white-space:nowrap}.workspace-counts span{display:grid;gap:5px;font-size:10px;color:#596d5e}.workspace-counts b{font-size:28px;color:#264832}.workspace-layout{display:grid;grid-template-columns:minmax(180px,28%) minmax(0,1fr);margin-top:22px;border-top:1px solid #dce2d9}.workspace-tasks{padding:18px 18px 0 0;border-right:1px solid #dce2d9}.workspace-tasks label{display:grid;gap:8px;font-size:11px;margin-bottom:14px}.workspace-tasks select{padding:8px;background:white;border:1px solid #cbd5c6}.workspace-tasks button{display:grid;width:100%;gap:6px;text-align:left;background:transparent;color:#34513c;border:1px solid transparent;padding:12px;margin-bottom:6px}.workspace-tasks button.chosen{background:#eff3df;border-color:#c8d3ac}.workspace-tasks small,.workspace-work small,.workspace-record small{display:block;font-size:10px;line-height:1.7;color:#637666}.workspace-memory{min-width:0;padding:18px 0 0 24px}.workspace-memory h3{margin:8px 0;font-size:18px}.workspace-memory h4{font-size:12px;margin:24px 0 8px}.workspace-memory-stats{display:flex;flex-wrap:wrap;gap:20px;border-block:1px solid #e5e8db;padding:12px 0;font-size:11px;color:#596d5e}.workspace-memory-stats b{color:#264832;margin-left:8px}.workspace-work{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:12px 0;border-bottom:1px solid #e5e8db;font-size:12px}.workspace-work>div{flex:1;min-width:160px}.workspace-work button{font-size:11px}.workspace-memory details{margin-top:18px;font-size:12px}.workspace-memory summary{cursor:pointer;font-weight:700;color:#34573f}.workspace-record{padding:12px 0;border-bottom:1px solid #e5e8db}.workspace-record pre{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.8 monospace;max-height:200px;overflow:auto;background:#f6f7ef;padding:10px}.workspace-record p{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:900px){.workspace-heading{flex-wrap:wrap}.workspace-layout{grid-template-columns:1fr}.workspace-tasks{border-right:0;padding-right:0;max-height:230px;overflow:auto}.workspace-memory{border-top:1px solid #dce2d9;padding:18px 0 0}.task-workspace{padding:18px}}
</style>
