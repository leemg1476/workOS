<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
const props = defineProps({ tasks: { type: Array, default: () => [] }, base: { type: String, required: true }, proposals: { type: Array, default: () => [] } })
const emit = defineEmits(['open-work', 'open-improvements'])
const showAll = ref(false)
const taskId = ref('TASK-001'), runs = ref([]), beforeId = ref(''), afterId = ref(''), message = ref(''), busy = ref(false)
let request = 0, timer
const verified = computed(() => runs.value.filter(r => r.status === 'SUCCEEDED' && r.verdict === 'PASS' && r.content))
const before = computed(() => verified.value.find(r => r.run_id === beforeId.value))
const after = computed(() => verified.value.find(r => r.run_id === afterId.value))
const latestEvaluation = computed(() => props.proposals.find(p => p.results))
const selectedTask = computed(() => props.tasks.find(t => t.task_id === taskId.value))
const indicators = computed(() => {
  if (!before.value?.content?.indicators || !after.value?.content?.indicators) return []
  const previous = new Map(before.value.content.indicators.map(x => [x.symbol, x]))
  return after.value.content.indicators.filter(x => previous.has(x.symbol)).map(x => ({ symbol:x.symbol, old:previous.get(x.symbol).current, value:x.current, unit:x.unit, delta:Number(x.current)-Number(previous.get(x.symbol).current) }))
})
const vocChanges = computed(() => {
  const old = before.value?.content?.recommendations, current = after.value?.content?.recommendations
  if (!old || !current) return []
  const categories = [...new Set([...old.map(x=>x.category), ...current.map(x=>x.category)])]
  return categories.map(category => ({ category, old: old.find(x=>x.category===category)?.count ?? 0, value:current.find(x=>x.category===category)?.count ?? 0 }))
})
function when(value) { return value ? new Date(value).toLocaleString('ko-KR', {timeZone:'Asia/Seoul',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '시간 없음' }
function label(r) { return `${when(r.started_at)} · ${r.artifact_version ? '결과 v'+r.artifact_version : '실행 '+r.attempt_no} · ${r.model_id}` }
function status(r) { return r.verdict === 'PASS' && r.status === 'SUCCEEDED' ? '검증 통과' : r.verdict === 'FAIL' || r.status === 'VERIFICATION_FAILED' ? '검증 실패' : r.status === 'FAILED' ? '실행 실패' : r.status === 'SUCCEEDED' ? '검증 결과 없음' : '진행 중' }
function summary(r) {
  const c = r.content
  if (!c) return r.status === 'FAILED' ? '결과 생성 전에 실행이 중단됐습니다.' : '아직 결과물이 없습니다.'
  if (r.kind === 'market_briefing') return `${c.dataset_version} · ${(c.indicators || []).map(x=>`${x.symbol} ${x.current}`).join(' / ')}`
  if (r.kind === 'voc_report') return `${c.method_version} · 추천 ${(c.recommendations || []).length}건 · 미분류 ${(c.unclassified_ids || []).length}건`
  if (r.kind === 'policy_impact') return `변경 조항 ${(c.changes || []).length}건 · 후속 업무 영향 분석`
  return `결과 ${r.kind || '기록'} · 상세에서 확인`
}
async function load() {
  const id = ++request
  try {
    const response = await fetch(`${props.base}/tasks/${encodeURIComponent(taskId.value)}/runs`)
    if (!response.ok) throw Error()
    const data = await response.json()
    if (id !== request) return
    runs.value = data
    const valid = data.filter(r => r.status === 'SUCCEEDED' && r.verdict === 'PASS' && r.content)
    if (!valid.some(r => r.run_id === afterId.value)) afterId.value = valid[0]?.run_id || ''
    if (!valid.some(r => r.run_id === beforeId.value)) beforeId.value = valid[1]?.run_id || ''
    message.value = ''
  } catch { if (id === request) message.value = '실행 기록을 불러오지 못했습니다.' }
  finally { if (id === request) busy.value = false }
}
watch(taskId, () => { showAll.value=false; beforeId.value=''; afterId.value=''; runs.value=[]; busy.value=true; load() })
onMounted(() => { load(); timer=setInterval(load, 5000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <section class="history-board" aria-labelledby="history-heading">
    <div class="history-intro"><div><span class="history-kicker">WORK HISTORY / 실행과 변화</span><h2 id="history-heading">무엇이 달라졌나요?</h2><p>업무를 고르면 실행 결과와 이전 기록을 함께 볼 수 있습니다.</p></div><span class="history-demo">DEMO DATA</span></div>
    <div class="history-tabs" role="group" aria-label="비교할 업무 선택"><button v-for="t in tasks.filter(t => ['TASK-001','TASK-002','TASK-003'].includes(t.task_id))" :key="t.task_id" :class="{active:taskId===t.task_id}" :aria-pressed="taskId===t.task_id" @click="taskId=t.task_id"><small>{{ t.task_id }}</small>{{ t.name }}</button></div>
    <div v-if="taskId==='TASK-003' && latestEvaluation" class="history-evaluation"><div><small>최근 생성 개선안의 고정 평가 · {{ latestEvaluation.suite_version }} · {{ latestEvaluation.status }}</small><b>{{ latestEvaluation.champion_version }} {{ latestEvaluation.results.champion_correct }}/{{ latestEvaluation.results.case_count }} → {{ latestEvaluation.challenger_version }} {{ latestEvaluation.results.challenger_correct }}/{{ latestEvaluation.results.case_count }}</b><span>개선 {{ latestEvaluation.results.gains.length }}건 · 퇴행 {{ latestEvaluation.results.regressions.length + (latestEvaluation.results.regression_suite?.regressions.length || 0) }}건 · {{ latestEvaluation.verdict }} · 합성 평가 사례</span></div><button @click="emit('open-improvements')">평가 근거 보기 →</button></div>
    <p v-if="message" class="history-alert" role="alert">{{ message }}</p>
    <div v-if="busy" class="history-empty">실행 기록을 불러오는 중입니다.</div>
    <div v-else-if="!runs.length" class="history-empty"><b>아직 실행 기록이 없습니다.</b><p>아래 업무 카드에서 실행을 요청하면 이곳에 결과가 표시됩니다.</p></div>
    <template v-else>
      <div class="history-columns"><div class="history-timeline"><div class="history-label">최근 실행 <span>최대 100건 · {{ showAll ? runs.length : Math.min(6,runs.length) }}건 표시</span></div><div v-for="r in runs.slice(0, showAll ? 100 : 6)" :key="r.run_id" class="history-entry"><div class="entry-top"><b :class="{'good':status(r)==='검증 통과','bad':status(r).includes('실패')}">{{ status(r) }}</b><time>{{ when(r.started_at) }}</time></div><p>{{ summary(r) }}</p><small>시도 {{ r.attempt_no }} · {{ r.model_id }}<template v-if="r.eval_suite_version"> · {{ r.eval_suite_version }}</template></small><button class="entry-link" @click="emit('open-work', r.work_id, r.run_id)">이 작업의 실행 상세 보기 →</button></div><button v-if="runs.length > 6" class="entry-link" @click="showAll=!showAll">{{ showAll ? '간략히 보기 ↑' : '이전 실행 더 보기 ↓' }}</button></div>
      <div class="history-comparison"><div class="history-label">검증 통과 결과 비교 <span>{{ selectedTask?.name }}</span></div><p v-if="verified.length < 2" class="history-empty">비교할 검증 통과 결과가 두 개 필요합니다. 실행 후 검증을 통과하면 여기에 표시됩니다.</p><template v-else><div class="history-selectors"><label>이전 결과<select v-model="beforeId"><option v-for="r in verified" :key="r.run_id" :value="r.run_id">{{ label(r) }}</option></select></label><label>비교 결과<select v-model="afterId"><option v-for="r in verified" :key="r.run_id" :value="r.run_id">{{ label(r) }}</option></select></label></div>
      <p v-if="beforeId===afterId" class="history-alert">서로 다른 두 실행을 선택해 주세요.</p>
      <template v-else-if="before && after"><div class="comparison-head"><div><small>이전</small><b>{{ label(before) }}</b><span>{{ summary(before) }}</span></div><div><small>비교</small><b>{{ label(after) }}</b><span>{{ summary(after) }}</span></div></div>
      <template v-if="taskId==='TASK-001'"><div class="change-row" v-for="n in indicators" :key="n.symbol"><b>{{ n.symbol }}</b><span>{{ n.old }} → {{ n.value }} {{ n.unit }}</span><strong>{{ n.delta > 0 ? '+' : '' }}{{ Number(n.delta.toFixed(4)) }}</strong></div><p class="history-caution">지표의 증감은 데이터 변화이며 업무 성과나 개선을 뜻하지 않습니다. 모델 해석은 의미 검증을 받지 않았습니다.</p></template>
      <template v-else-if="taskId==='TASK-003'"><div class="change-row" v-for="n in vocChanges" :key="n.category"><b>{{ n.category }}</b><span>{{ n.old }}건 → {{ n.value }}건</span><strong>{{ n.value-n.old > 0 ? '+' : '' }}{{ n.value-n.old }}건</strong></div><p class="history-caution">범주별 건수 변화는 품질 향상이 아닙니다. 동일한 고정 사례에서 평가한 개선·퇴행은 개선 평가 화면에서 확인하세요.</p><button class="entry-link" @click="emit('open-improvements')">Champion / Challenger 평가 보기 →</button></template>
      <template v-else><div class="change-row"><b>변경 조항</b><span>{{ before.content.changes?.length || 0 }}건 → {{ after.content.changes?.length || 0 }}건</span><strong>기록 비교</strong></div><p class="history-caution">정책 분석 기록을 비교합니다. 건수 변화는 정책 품질이나 검토 완료를 뜻하지 않습니다.</p></template>
      </template></template></div></div>
    </template>
  </section>
</template>
