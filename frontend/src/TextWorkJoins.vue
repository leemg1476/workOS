<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
const props=defineProps({base:{type:String,required:true},tasks:{type:Array,required:true}})
const emit=defineEmits(['open-work'])
const joins=ref([]), selected=ref([]), target=ref(''), consent=ref(false), busy=ref(false), error=ref(''), message=ref(''), open=ref(false)
const choices=computed(()=>props.tasks.filter(t=>t.execution_kind==='provided_text_workflow_v1'))
const targetTask=computed(()=>choices.value.find(t=>t.task_id===target.value))
const name=id=>choices.value.find(t=>t.task_id===id)?.name || id
const invalid=computed(()=>selected.value.length<2 || selected.value.length>8 || !target.value || selected.value.includes(target.value))
async function load(){const r=await fetch(`${props.base}/text-work-joins`);if(!r.ok)throw Error('결합 설정을 불러오지 못했습니다.');joins.value=await r.json()}
async function create(){
  busy.value=true;error.value='';message.value=''
  try{const r=await fetch(`${props.base}/text-work-joins`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_task_ids:selected.value,target_task_id:target.value,idempotency_key:crypto.randomUUID()})});const d=await r.json();if(!r.ok)throw Error(d.detail||'결합을 만들지 못했습니다.');consent.value=false;await load();message.value='결합을 만들었습니다. 각 선행 업무의 새 채택 결과를 기다립니다.'}catch(e){error.value=e.message}finally{busy.value=false}
}
async function toggle(join){
  busy.value=true;error.value='';message.value=''
  try{const r=await fetch(`${props.base}/text-work-joins/${join.join_id}/state`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!join.enabled,expected_version:join.version,idempotency_key:crypto.randomUUID()})});const d=await r.json();if(!r.ok)throw Error(d.detail||'결합 상태를 바꾸지 못했습니다.');await load();message.value=join.enabled?'결합을 중지했습니다. 이미 생성된 작업은 유지됩니다.':'재개했습니다. 각 선행 업무의 새로운 채택 결과부터 기다립니다.'}catch(e){error.value=e.message;await load().catch(()=>{})}finally{busy.value=false}
}
let timer
onMounted(()=>{load().catch(e=>error.value=e.message);timer=setInterval(()=>load().catch(()=>{}),5000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
  <section class="work-links work-joins" aria-label="여러 업무 결과 결합">
    <div class="work-links-head"><div><small>WORK JOIN / 여러 선행 결과</small><h2>결과를 모아 후속 업무 수행</h2><p>선행 업무의 새 결과가 모두 준비되면 한 작업으로 이어집니다.</p></div><button class="secondary" @click="open=!open">{{ open ? '결합 설정 닫기 ↑' : '결합 설정 →' }}</button></div>
    <div v-if="open" class="work-links-body">
      <p>결합을 만든 뒤 또는 직전 결합 이후 각 선행 업무에서 사람이 채택한 최신 결과가 하나씩 필요합니다. 같은 업무에서 여러 결과가 먼저 오면 그중 최신 결과를 사용합니다. 이전 결합에 사용된 결과는 다시 사용하지 않습니다.</p>
      <p>시스템은 결과가 같은 기간이나 사건에 속한다고 판단하지 않습니다. 조합이 적절한지는 사람이 확인해야 합니다. 선행 결과가 오래되거나 변경되면 새로 채택될 때까지 기다립니다. 중지 후 재개하면 이전 대기 결과는 사용하지 않습니다.</p>
      <p>선행 결과 전체가 후속 업무 입력에 포함됩니다. 후속 업무가 LIVE LLM이면 모델 서비스로 전송됩니다. 일정 중지와 결합 중지는 별개이며, 중지 전 생성된 작업은 계속 남습니다.</p>
      <fieldset class="join-choices"><legend>선행 업무 선택 · 2~8개</legend><label v-for="task in choices" :key="task.task_id"><input type="checkbox" :value="task.task_id" v-model="selected" @change="consent=false"> {{ task.name }} <small>{{ task.task_id }}</small></label></fieldset>
      <label class="join-target">후속 업무<select v-model="target" @change="consent=false"><option value="">업무 선택</option><option v-for="task in choices" :key="task.task_id" :value="task.task_id">{{ task.name }} · {{ task.model_mode }}</option></select></label>
      <p v-if="targetTask">후속 실행 모드: <b>{{ targetTask.model_mode }}</b> · {{ targetTask.model_mode==='LIVE_LLM' ? '선행 결과가 모델 서비스로 전송됩니다.' : 'MOCK 예시이며 실제 업무 결과가 아닙니다.' }}</p>
      <p v-if="selected.includes(target) && target" class="failure">후속 업무는 선행 업무에서 제외해 주세요.</p>
      <label class="contract-check"><input type="checkbox" v-model="consent"> 결합 규칙과 입력 전달 범위를 확인했습니다.</label>
      <button @click="create" :disabled="busy || invalid || !consent">결합 만들기</button>
      <p v-if="choices.length<3">사용자 텍스트 업무가 세 개 이상 필요합니다.</p>
      <p v-if="error" class="failure" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
      <div v-if="joins.length" class="work-link-list"><h3>등록된 결합</h3><div v-for="join in joins" :key="join.join_id" class="join-row"><div><b>{{ join.inputs.map(x=>name(x.source_task_id)).join(' + ') }} → {{ name(join.target_task_id) }}</b><small>{{ join.enabled ? '결합 중' : '중지됨' }} · 생성된 작업 {{ join.firing_count }}건 · v{{ join.version }}</small><ul><li v-for="input in join.inputs" :key="input.source_task_id">{{ name(input.source_task_id) }} · {{ input.state==='READY' ? '새 결과 준비' : input.state==='STALE' ? '다시 확인 필요' : '새 결과 대기' }}</li></ul></div><button class="secondary" @click="toggle(join)" :disabled="busy">{{ join.enabled ? '결합 중지' : '결합 재개' }}</button><button v-if="join.latest_work_id" class="secondary" @click="emit('open-work',join.latest_work_id)">최근 결합 작업 보기 →</button></div></div>
    </div>
  </section>
</template>
