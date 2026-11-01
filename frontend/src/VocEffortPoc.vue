<script setup>
import { computed, ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
const props=defineProps({base:{type:String,required:true}})
const emit=defineEmits(['open-work','refresh'])
const stage=ref('baseline'), records=ref([]), answers=ref({}), reviewed=ref([]), seconds=ref(0), clockRunning=ref(false)
const baselineId=ref(''), measurements=ref([]), availableRuns=ref([]), selectedRun=ref(''), runView=ref(null)
const proposals=ref([]), activeMethod=ref(''), candidate=ref('challenger-v2'), busy=ref(false), error=ref(''), message=ref('')
const schedules=ref([])
const stageNotice=ref('')
const labels={billing:'결제·환불',delivery:'배송·도착',support:'상담·문의',unclassified:'분류 불가'}
const period=computed(()=>stage.value==='week2'?'week2':'week1')
const baselines=computed(()=>measurements.value.filter(x=>x.kind==='BASELINE'))
const currentBaseline=computed(()=>baselines.value.find(x=>x.measurement_id===baselineId.value))
const assisted=computed(()=>measurements.value.filter(x=>x.kind==='ASSISTED' && x.baseline_id===baselineId.value))
const week1=computed(()=>assisted.value.filter(x=>x.period==='week1').at(-1))
const week2=computed(()=>assisted.value.filter(x=>x.period==='week2').at(-1))
const pairedMethods=computed(()=>{
  const all=assisted.value.filter(x=>x.period==='week1');if(all.length<2)return null
  const before=all[0],after=all.at(-1)
  if(before.snapshot.fixture_sha256!==after.snapshot.fixture_sha256 || before.snapshot.gold_sha256!==after.snapshot.gold_sha256)return null
  const gold=before.snapshot.gold_labels,old=before.snapshot.run.predictions,next=after.snapshot.run.predictions
  const keys=Object.keys(gold),gains=keys.filter(id=>old[id]!==gold[id] && next[id]===gold[id]),losses=keys.filter(id=>old[id]===gold[id] && next[id]!==gold[id])
  return {before,after,gains:gains.length,losses:losses.length}
})
const relevantProposals=computed(()=>{
  const ids=new Set(assisted.value.map(x=>x.snapshot?.run?.artifact_id).filter(Boolean))
  return proposals.value.filter(x=>ids.has(x.artifact_id))
})
const allAnswered=computed(()=>records.value.length>0 && records.value.every(x=>labels[answers.value[x.record_id]]))
const answeredCount=computed(()=>records.value.filter(x=>labels[answers.value[x.record_id]]).length)
const changed=computed(()=>runView.value ? records.value.filter(x=>answers.value[x.record_id]!==runView.value.predictions[x.record_id]).map(x=>x.record_id) : [])
const changedReviewed=computed(()=>changed.value.every(x=>reviewed.value.includes(x)))
const nextWeekObserved=computed(()=>!!week2.value && !!currentBaseline.value && Date.parse(week2.value.snapshot.run.started_at)-Date.parse(currentBaseline.value.created_at)>=7*86400000 && Date.parse(week2.value.created_at)-Date.parse(currentBaseline.value.created_at)>=7*86400000)
const schedule=computed(()=>schedules.value.find(x=>x.baseline_id===baselineId.value))
const week1Delta=computed(()=>week1.value && currentBaseline.value ? {seconds:currentBaseline.value.human_seconds-week1.value.human_seconds,correct:week1.value.snapshot.result.correct-currentBaseline.value.snapshot.result.correct} : null)
const timeValid=computed(()=>Number.isInteger(Number(seconds.value)) && Number(seconds.value)>=1 && Number(seconds.value)<=86400)
const saveRequirements=computed(()=>{
  const pending=[]
  if(clockRunning.value)pending.push('시간 측정을 일시 정지하세요')
  else if(!timeValid.value)pending.push('소요 시간을 1초 이상 기록하세요')
  if(!allAnswered.value)pending.push(`문의 ${records.value.length}건 중 ${answeredCount.value}건 분류 완료`)
  if(stage.value!=='baseline' && !changedReviewed.value)pending.push('수정한 항목의 확인함을 체크하세요')
  return pending
})
const formatSeconds=value=>`${Math.floor(value/60)}분 ${String(value%60).padStart(2,'0')}초`
const percent=result=>result?.total ? `${(result.correct/result.total*100).toFixed(1)}%` : '—'
const when=value=>value ? new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'}) : '—'
let startedAt=0, baseSeconds=0, tick, refreshTimer
function syncClock(){if(clockRunning.value) seconds.value=baseSeconds+Math.floor((Date.now()-startedAt)/1000)}
function toggleClock(){if(clockRunning.value){syncClock();clockRunning.value=false;clearInterval(tick)}else{baseSeconds=Number(seconds.value)||0;startedAt=Date.now();clockRunning.value=true;tick=setInterval(syncClock,250)}}
function resetForm(){if(clockRunning.value)toggleClock();seconds.value=0;answers.value={};reviewed.value=[];runView.value=null;selectedRun.value='';error.value='';message.value=''}
function selectStage(next){
  if(next!=='baseline' && !baselines.value.length){stageNotice.value='02·03 단계를 사용하려면 먼저 아래 24건을 직접 분류하고 소요 시간을 기록한 뒤 사람 기준선을 저장하세요.';return}
  stageNotice.value='';stage.value=next
}
async function readJson(r,fallback){const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:fallback);return d}
async function load(){
  const [m,p,s,scheduled]=await Promise.all([fetch(`${props.base}/poc-voc/measurements`),fetch(`${props.base}/improvements`),fetch(`${props.base}/voc/state`),fetch(`${props.base}/poc-voc/schedules`)])
  measurements.value=await readJson(m,'측정 기록을 불러오지 못했습니다.');proposals.value=await readJson(p,'개선 이력을 불러오지 못했습니다.')
  const state=await readJson(s,'현재 방법을 불러오지 못했습니다.');activeMethod.value=state.method.active_method_id
  schedules.value=await readJson(scheduled,'예약 상태를 불러오지 못했습니다.')
  if(!baselines.value.some(x=>x.measurement_id===baselineId.value))baselineId.value=baselines.value.at(-1)?.measurement_id || ''
}
async function loadInputs(){const d=await readJson(await fetch(`${props.base}/poc-voc/inputs/${period.value}`),'사례를 불러오지 못했습니다.');records.value=d.records}
async function loadRuns(){const rows=await readJson(await fetch(`${props.base}/tasks/TASK-003/runs`),'실행 이력을 불러오지 못했습니다.');const expected=period.value==='week1'?'voc-poc-week1-v1':'voc-poc-week2-v1';availableRuns.value=rows.filter(x=>x.status==='SUCCEEDED' && x.verdict==='PASS' && x.content?.source_version===expected)}
watch(stage,()=>{resetForm();loadInputs().then(()=>stage.value!=='baseline' && loadRuns()).catch(e=>error.value=e.message)})
async function save(){
  if(clockRunning.value || !timeValid.value || !allAnswered.value) return
  busy.value=true;error.value='';message.value=''
  try{
    const payload={answers:answers.value,human_seconds:Number(seconds.value),idempotency_key:crypto.randomUUID()}
    let path='baseline'
    if(stage.value!=='baseline'){path='assisted';Object.assign(payload,{baseline_id:baselineId.value,run_id:runView.value.run_id,period:period.value,reviewed_ids:reviewed.value})}
    const d=await readJson(await fetch(`${props.base}/poc-voc/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),'측정 기록을 저장하지 못했습니다.')
    await load();if(path==='baseline')baselineId.value=d.measurement_id;resetForm();stageNotice.value=''
    message.value=path==='baseline'?'사람 기준선을 저장했습니다. 기록은 합성 사례와 자기 기록 시간에 대한 것입니다.':'검토 결과와 자기 기록 시간을 저장했습니다. 실제 업무 성과로 자동 판정하지 않습니다.'
    emit('refresh')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
async function startRun(){
  busy.value=true;error.value='';message.value=''
  try{
    const source_version=period.value==='week1'?'voc-poc-week1-v1':'voc-poc-week2-v1'
    const start=await readJson(await fetch(`${props.base}/tasks/TASK-003/run`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idempotency_key:crypto.randomUUID(),input_snapshot:{source_version}})}),'업무 실행을 요청하지 못했습니다.')
    const deadline=Date.now()+90000;let work
    do{work=await readJson(await fetch(`${props.base}/work/${start.work_id}`),'실행 상태를 불러오지 못했습니다.');if(['COMMITTED','REWORK_REQUIRED'].includes(work.work.status))break;await new Promise(resolve=>setTimeout(resolve,700))}while(Date.now()<deadline)
    if(work.work.status!=='COMMITTED')throw Error(work.work.status==='REWORK_REQUIRED'?'실행 또는 검증에 실패했습니다. 실행 이력에서 원인을 확인하세요.':'아직 실행 중입니다. 잠시 뒤 기존 실행을 불러오세요.')
    selectedRun.value=work.runs.at(-1).run_id;await useRun();await loadRuns();emit('refresh')
  }catch(e){error.value=e.message;await loadRuns().catch(()=>{})}finally{busy.value=false}
}
async function useRun(){
  if(!selectedRun.value)return
  const d=await readJson(await fetch(`${props.base}/poc-voc/runs/${selectedRun.value}?period=${period.value}`),'검증된 실행을 불러오지 못했습니다.')
  runView.value=d;answers.value={...d.predictions};reviewed.value=[];message.value='검증된 실제 Work OS 실행 결과를 불러왔습니다. 항목을 확인하고 필요한 부분을 수정하세요.'
}
async function loadSelectedRun(){busy.value=true;error.value='';try{await useRun()}catch(e){error.value=e.message}finally{busy.value=false}}
async function createSchedule(){busy.value=true;error.value='';message.value='';try{await readJson(await fetch(`${props.base}/poc-voc/schedules`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({baseline_id:baselineId.value,idempotency_key:crypto.randomUUID()})}),'예약을 만들지 못했습니다.');await load();message.value='선택한 기준선의 7일 이후부터 가상 2주차 입력을 매주 실행합니다.'}catch(e){error.value=e.message}finally{busy.value=false}}
async function toggleSchedule(){busy.value=true;error.value='';message.value='';try{await readJson(await fetch(`${props.base}/poc-voc/schedules/${schedule.value.schedule_id}/state`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!schedule.value.enabled,idempotency_key:crypto.randomUUID()})}),'예약을 변경하지 못했습니다.');await load();message.value=schedule.value.enabled?'가상 입력의 주간 예약을 재개했습니다.':'가상 입력의 주간 예약을 중지했습니다. 이미 생성된 작업은 유지됩니다.'}catch(e){error.value=e.message}finally{busy.value=false}}
async function openScheduledRun(){busy.value=true;error.value='';try{stage.value='week2';await nextTick();await loadInputs();selectedRun.value=schedule.value.latest_run_id;await useRun();await loadRuns()}catch(e){error.value=e.message}finally{busy.value=false}}
async function createProposal(){
  busy.value=true;error.value='';message.value=''
  try{
    const d=await readJson(await fetch(`${props.base}/feedback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idempotency_key:crypto.randomUUID(),artifact_id:week1.value.snapshot.run.artifact_id,recommendation_id:'REC-billing',reason:'누락',challenger_version:candidate.value})}),'개선안을 만들지 못했습니다.')
    await load();message.value=`피드백을 기록했습니다. ${d.proposal_id.slice(0,8)} 후보를 고정 사례로 평가하세요.`
  }catch(e){error.value=e.message}finally{busy.value=false}
}
async function proposalAction(p,action){
  busy.value=true;error.value='';message.value=''
  try{await readJson(await fetch(`${props.base}/improvements/${p.proposal_id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idempotency_key:crypto.randomUUID()})}),'개선안 작업을 완료하지 못했습니다.');await load();emit('refresh');message.value={evaluate:'같은 합성 고정 사례의 비교를 기록했습니다.',apply:'사용자가 후보를 적용했습니다. 같은 입력으로 다시 실행해 확인하세요.',reject:'후보를 기각했습니다.',rollback:'이전 방법으로 되돌렸습니다.'}[action]}
  catch(e){error.value=e.message}finally{busy.value=false}
}
onMounted(()=>{Promise.all([load(),loadInputs()]).catch(e=>error.value=e.message);refreshTimer=setInterval(()=>load().catch(()=>{}),10000)})
onUnmounted(()=>{clearInterval(tick);clearInterval(refreshTimer)})
</script>
<template>
  <section class="effort-poc" aria-labelledby="poc-heading">
    <div class="poc-heading"><div><small>HUMAN BASELINE / WORK OS POC</small><h2 id="poc-heading">사람의 시간과 결과를 비교합니다.</h2><p>등록된 TASK-003을 이용해 가상 고객 문의 24건을 직접 분류하고, Work OS 결과를 검토한 뒤 다음 주의 새 사례와 비교합니다.</p></div><span>합성 데이터 · 실제 성과 미입증</span></div>
    <div class="poc-notice"><b>측정 전 확인</b><p>‘1시간’은 확인된 측정값이 아닙니다. 실제로 사용한 준비·검토·수정 시간을 기록하세요. 시간과 검토 여부는 사용자 자기 기록이며 신원이나 활동을 자동 검증하지 않습니다. 정답은 사람이 사전 정의한 합성 라벨이며 실제 고객 업무의 정확도는 아닙니다.</p><p>이 PoC의 TASK-003 분류는 <b>MOCK · 사전 정의된 결정론적 규칙</b>입니다. 실제 고객 데이터나 LLM 분류가 아닙니다. 서로 다른 주의 입력은 다릅니다. 단순 점수 차이는 원인이나 성능 개선의 증명이 아닙니다. 같은 입력의 방법 비교와 별도의 고정 평가 결과도 함께 확인하세요.</p></div>
    <div class="poc-stages" role="group" aria-label="측정 단계"><button :class="{chosen:stage==='baseline'}" @click="selectStage('baseline')">01 사람 기준선</button><button :class="{chosen:stage==='week1'}" @click="selectStage('week1')">02 같은 입력 + Work OS</button><button :class="{chosen:stage==='week2'}" @click="selectStage('week2')">03 다음 주 새 입력</button></div>
    <p v-if="!baselines.length" class="poc-next-step" role="status"><b>지금 할 일:</b> 시간 측정 시작 → 문의 24건 분류 → 측정 일시 정지 → 맨 아래 ‘사람 기준선 저장’. 저장하면 02·03 단계를 사용할 수 있습니다.</p>
    <p v-if="stageNotice" class="poc-next-step" role="status">{{ stageNotice }}</p>
    <p class="poc-caution">단계를 바꾸거나 페이지를 닫으면 저장하지 않은 분류와 타이머가 초기화됩니다. 경과 시간은 탭을 벗어난 시간도 포함하며 제출 전 직접 보정할 수 있습니다.</p>
    <div v-if="baselines.length" class="poc-baseline-picker"><label>비교할 사람 기준선 <select v-model="baselineId"><option v-for="m in baselines" :key="m.measurement_id" :value="m.measurement_id">{{ when(m.created_at) }} · {{ formatSeconds(m.human_seconds) }} · {{ m.snapshot.result.correct }}/{{ m.snapshot.result.total }}</option></select></label></div>
    <div v-if="currentBaseline" class="poc-schedule"><div><b>다음 주 자동 실행 · 선택 사항</b><p>기준선 기록 후 최소 7일이 지난 시점부터 매주 같은 가상 2주차 자료를 실행합니다. 실제 새 문의를 수집하지 않으며, 정기 실행은 사람 검토를 대신하지 않습니다.</p><p v-if="schedule">{{ schedule.enabled?'예약 중':'중지됨' }} · 다음 예정 {{ schedule.next_due_at ? when(schedule.next_due_at) : '중지됨' }} · 서울 시간 · {{ schedule.schedule }}<template v-if="schedule.latest_work_id"> · 최근 작업 {{ schedule.latest_work_status }} / {{ schedule.latest_run_status || '대기' }}</template></p></div><div><button v-if="!schedule" @click="createSchedule" :disabled="busy">주간 예약 켜기</button><button v-else class="secondary" @click="toggleSchedule" :disabled="busy">{{ schedule.enabled?'주간 예약 중지':'주간 예약 재개' }}</button><button v-if="schedule?.latest_run_status==='SUCCEEDED' && schedule?.latest_work_status==='COMMITTED'" class="secondary" @click="openScheduledRun" :disabled="busy">예약 결과 검토하기 →</button><button v-if="schedule?.latest_work_id" class="secondary" @click="emit('open-work',schedule.latest_work_id,schedule.latest_run_id || '')">예약 실행 이력 ↗</button></div></div>
    <div class="poc-layout"><div class="poc-work">
      <div class="poc-section-head"><div><small>{{ stage==='baseline'?'WEEK 1 / HUMAN ONLY':stage==='week1'?'WEEK 1 / SAME INPUT':'WEEK 2 / NEW INPUT' }}</small><h3>{{ stage==='baseline'?'사람이 직접 분류':stage==='week1'?'같은 문의를 Work OS로 처리':'새 문의를 Work OS로 처리' }}</h3></div><b>가상 문의 {{ records.length }}건</b></div>
      <p v-if="stage==='baseline'">모델 결과를 보기 전에 각 문의를 분류하세요. 기준선 저장은 처음 작업한 시간과 답안을 불변 기록합니다.</p>
      <p v-else>시간 측정을 시작한 뒤 실행하고 각 결과를 확인하세요. 수정한 항목은 ‘확인함’으로 표시해야 합니다. 기존 검증 완료 실행도 불러올 수 있지만 기준선 저장 이후 시작된 실행만 기록할 수 있습니다.</p>
      <div class="poc-timer"><div><small>사람이 사용한 시간 · 자기 기록</small><strong>{{ formatSeconds(Number(seconds)||0) }}</strong></div><button class="secondary" @click="toggleClock">{{ clockRunning?'측정 일시 정지':'시간 측정 시작 / 재개' }}</button><label>초 단위 확인·보정<input type="number" min="1" max="86400" step="1" v-model.number="seconds" :disabled="clockRunning"></label></div>
      <p v-if="stage==='baseline' || runView" class="poc-progress" aria-live="polite">분류 {{ answeredCount }} / {{ records.length }}건 · {{ saveRequirements.length ? '저장 전: '+saveRequirements.join(' · ') : '저장할 수 있습니다.' }}</p>
      <div v-if="stage!=='baseline'" class="poc-run-controls"><button @click="startRun" :disabled="busy || !baselineId">{{ busy ? '처리 중…' : '이 입력으로 Work OS 실행 →' }}</button><label v-if="availableRuns.length">기존 검증 Run<select v-model="selectedRun"><option value="">선택</option><option v-for="r in availableRuns" :key="r.run_id" :value="r.run_id">{{ when(r.started_at) }} · {{ r.run_id.slice(0,8) }} · {{ r.model_id }}</option></select></label><button v-if="availableRuns.length" class="secondary" @click="loadSelectedRun" :disabled="busy || !selectedRun">기존 실행 불러오기</button><p v-if="runView">MOCK · Work {{ runView.work_id.slice(0,8) }} · 방법 {{ runView.method_version }} · {{ runView.verifier_version }} · 시스템 실행 {{ runView.run_elapsed_ms }}ms <button class="entry-link" @click="emit('open-work',runView.work_id,runView.run_id)">근거 보기 ↗</button></p></div>
      <div v-if="stage==='baseline' || runView" class="poc-cases"><div v-for="(item,index) in records" :key="item.record_id" class="poc-case"><span class="poc-case-no">{{ String(index+1).padStart(2,'0') }}</span><div><p>{{ item.body }}</p><div class="poc-case-actions"><label>분류 <select v-model="answers[item.record_id]"><option :value="undefined">선택</option><option v-for="(label,key) in labels" :key="key" :value="key">{{ label }}</option></select></label><label v-if="runView" class="poc-reviewed"><input type="checkbox" v-model="reviewed" :value="item.record_id"> 확인함</label><small v-if="runView">초안: {{ labels[runView.predictions[item.record_id]] }}</small></div></div></div></div>
      <p v-if="runView && !changedReviewed" class="failure">수정한 항목을 모두 ‘확인함’으로 표시해 주세요.</p>
      <button v-if="stage==='baseline' || runView" @click="save" :disabled="busy || clockRunning || !timeValid || !allAnswered || (stage!=='baseline' && (!runView || !changedReviewed))" aria-describedby="poc-save-help">{{ stage==='baseline'?'사람 기준선 저장':'검토와 시간 기록 저장' }}</button><p v-if="stage==='baseline' || runView" id="poc-save-help" class="poc-progress" aria-live="polite">{{ busy ? '저장 또는 실행을 처리하고 있습니다.' : saveRequirements.length ? '저장 전: '+saveRequirements.join(' · ') : '저장할 수 있습니다.' }}</p>
      <p v-if="error" role="alert" class="failure">{{ error }}</p><p v-if="message" role="status" class="detail-note">{{ message }}</p>
    </div><div class="poc-results"><small>OBSERVED EVIDENCE</small><h3>기록된 비교</h3>
      <p v-if="!currentBaseline">아직 사람이 수행한 기준선이 없습니다. 1시간을 임의로 채우지 않습니다.</p>
      <template v-else><div class="poc-metric"><span>사람 기준선 · 같은 1주차 입력</span><strong>{{ formatSeconds(currentBaseline.human_seconds) }}</strong><small>합성 정답 {{ currentBaseline.snapshot.result.correct }}/{{ currentBaseline.snapshot.result.total }} · {{ percent(currentBaseline.snapshot.result) }}</small></div>
        <div v-if="week1" class="poc-metric"><span>Work OS + 사람 검토 · 같은 입력</span><strong>{{ formatSeconds(week1.human_seconds) }}</strong><small>초안 {{ week1.snapshot.raw_result.correct }}/{{ week1.snapshot.raw_result.total }} → 검토 후 {{ week1.snapshot.result.correct }}/{{ week1.snapshot.result.total }}</small><small>확인 {{ week1.snapshot.reviewed_count }}건 · 수정 {{ week1.snapshot.changed_ids.length }}건 · {{ week1.snapshot.run.method_version }}</small><small>사람 기준선 대비 {{ week1Delta.seconds>=0 ? '덜 사용' : '더 사용' }} {{ formatSeconds(Math.abs(week1Delta.seconds)) }} · 정답 {{ week1Delta.correct>=0?'+':'' }}{{ week1Delta.correct }}건</small></div><p v-else>같은 입력의 Work OS 검토 기록이 없습니다.</p>
        <div v-if="week2" class="poc-metric"><span>다음 주 입력 · 다른 사례</span><strong>{{ formatSeconds(week2.human_seconds) }}</strong><small>초안 {{ week2.snapshot.raw_result.correct }}/{{ week2.snapshot.raw_result.total }} → 검토 후 {{ week2.snapshot.result.correct }}/{{ week2.snapshot.result.total }}</small><small>확인 {{ week2.snapshot.reviewed_count }}건 · 수정 {{ week2.snapshot.changed_ids.length }}건 · {{ week2.snapshot.run.method_version }}</small><small>사람 기준선 대비 {{ currentBaseline.human_seconds-week2.human_seconds>=0 ? '덜 사용' : '더 사용' }} {{ formatSeconds(Math.abs(currentBaseline.human_seconds-week2.human_seconds)) }} · 정답 {{ week2.snapshot.result.correct-currentBaseline.snapshot.result.correct>=0?'+':'' }}{{ week2.snapshot.result.correct-currentBaseline.snapshot.result.correct }}건 · 서로 다른 사례</small><b>{{ nextWeekObserved ? '기준선 기록 후 7일 이상 경과' : '같은 주 리허설 · 7일 미경과' }}</b></div><p v-else>새 입력의 검토 기록이 없습니다. 현재 시각의 시범 실행은 다음 주의 실측이 아닙니다.</p>
        <p>시간은 사람의 자기 기록, 점수는 공개된 합성 정답 기준입니다. 순서·학습 효과와 동일인 여부를 통제하지 않았습니다. 사례가 다른 주차 간 차이는 실무 개선의 인과관계를 입증하지 않습니다.</p>
        <div v-if="week1" class="poc-same-input"><b>같은 1주차 입력의 Work OS 기록</b><p v-if="pairedMethods">{{ pairedMethods.before.snapshot.run.method_version }} → {{ pairedMethods.after.snapshot.run.method_version }} · 초안 개선 {{ pairedMethods.gains }}건 · 퇴행 {{ pairedMethods.losses }}건 · 합성 고정 입력</p><p v-for="m in assisted.filter(x=>x.period==='week1')" :key="m.measurement_id">{{ when(m.created_at) }} · {{ m.snapshot.run.method_version }} · 초안 {{ m.snapshot.raw_result.correct }}/{{ m.snapshot.raw_result.total }} · 검토 후 {{ m.snapshot.result.correct }}/{{ m.snapshot.result.total }} · {{ formatSeconds(m.human_seconds) }}</p></div>
      </template>
    </div></div>
    <div class="poc-improvement"><small>FEEDBACK → FIXED EVALUATION → HUMAN DECISION</small><h3>피드백을 방법 개선으로 연결</h3><p>1주차 검토 기록에서 사전에 준비된 분류 규칙 후보를 선택합니다. 후보는 사람의 수정에서 자동 학습하거나 생성하지 않습니다. 기존 24건과 별도 6건의 합성 고정 사례에 같은 방법들을 실행하며, PASS 후에도 직접 적용해야 합니다. 이는 2주차 사례나 실제 고객의 품질을 보증하지 않습니다.</p><template v-if="week1"><p>현재 방법 {{ activeMethod }} · 연결할 검토 Run {{ week1.snapshot.run.run_id.slice(0,8) }} · 수정 {{ week1.snapshot.changed_ids.length }}건</p><label>시범 후보 <select v-model="candidate"><option value="challenger-v2" :disabled="activeMethod==='challenger-v2'">누락 보완 후보</option><option value="challenger-regressed" :disabled="activeMethod==='challenger-regressed'">퇴행 후보</option><option value="challenger-overfit" :disabled="activeMethod==='challenger-overfit'">별도 회귀셋 차단 후보</option></select></label> <button class="secondary" @click="createProposal" :disabled="busy || candidate===activeMethod || !week1.snapshot.changed_ids.length">누락 피드백으로 후보 만들기</button><p v-if="!week1.snapshot.changed_ids.length">피드백할 수정이 기록되지 않았습니다.</p></template><p v-else>먼저 같은 입력의 Work OS 검토 결과를 기록하세요.</p>
      <div v-for="p in relevantProposals" :key="p.proposal_id" class="poc-proposal"><b>{{ p.champion_version }} → {{ p.challenger_version }} · {{ p.status }} <template v-if="p.verdict">· {{ p.verdict }}</template></b><p v-if="p.results">동일 기본 사례 {{ p.results.champion_correct }}/{{ p.results.case_count }} → {{ p.results.challenger_correct }}/{{ p.results.case_count }} · 퇴행 {{ p.results.regressions.length }}건 · 별도 회귀 {{ p.results.regression_suite?.regressions?.length ?? '미기록' }}건</p><button v-if="p.status==='DRAFT'" @click="proposalAction(p,'evaluate')" :disabled="busy">같은 고정 사례로 평가</button><template v-if="p.status==='PROPOSED'"><button v-if="p.verdict==='PASS'" @click="proposalAction(p,'apply')" :disabled="busy">검토 후 후보 적용</button> <button class="secondary" @click="proposalAction(p,'reject')" :disabled="busy">후보 기각</button></template><button v-if="p.status==='PROMOTED'" class="secondary" @click="proposalAction(p,'rollback')" :disabled="busy">이전 방법으로 롤백</button></div>
    </div>
  </section>
</template>
