<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
const props=defineProps({base:{type:String,required:true},tasks:{type:Array,required:true}})
const emit=defineEmits(['open-work'])
const links=ref([]), source=ref(''), target=ref(''), consent=ref(false), busy=ref(false), error=ref(''), message=ref(''), open=ref(false)
const textTasks=computed(()=>props.tasks.filter(t=>t.execution_kind==='provided_text_workflow_v1'))
const targetTask=computed(()=>textTasks.value.find(t=>t.task_id===target.value))
const name=id=>textTasks.value.find(t=>t.task_id===id)?.name || id
async function load(){const r=await fetch(`${props.base}/text-work-links`);if(!r.ok)throw Error('업무 연결을 불러오지 못했습니다.');links.value=await r.json()}
async function create(){
  busy.value=true;error.value='';message.value=''
  try{const r=await fetch(`${props.base}/text-work-links`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_task_id:source.value,target_task_id:target.value,idempotency_key:crypto.randomUUID()})});const d=await r.json();if(!r.ok)throw Error(d.detail||'연결을 만들지 못했습니다.');consent.value=false;await load();message.value='연결했습니다. 이후 채택되는 결과부터 후속 작업이 생성됩니다.'}catch(e){error.value=e.message}finally{busy.value=false}
}
async function toggle(link){
  busy.value=true;error.value='';message.value=''
  try{const r=await fetch(`${props.base}/text-work-links/${link.link_id}/state`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!link.enabled,expected_version:link.version,idempotency_key:crypto.randomUUID()})});const d=await r.json();if(!r.ok)throw Error(d.detail||'연결 상태를 바꾸지 못했습니다.');await load();message.value=link.enabled?'연결을 중지했습니다. 이미 생성된 후속 작업은 유지됩니다.':'연결을 다시 시작했습니다. 이후 채택된 결과부터 전달됩니다.'}catch(e){error.value=e.message;await load().catch(()=>{})}finally{busy.value=false}
}
let timer
onMounted(()=>{load().catch(e=>error.value=e.message);timer=setInterval(()=>load().catch(()=>{}),5000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
  <section class="work-links" aria-label="업무 간 연결">
    <div class="work-links-head"><div><small>WORK CONNECTIONS / 지속 업무</small><h2>업무를 이어서 수행하기</h2><p>사람이 채택한 결과를 다른 텍스트 업무의 입력으로 연결합니다.</p></div><button class="secondary" @click="open=!open">{{ open ? '연결 설정 닫기 ↑' : '연결 설정 →' }}</button></div>
    <div v-if="open" class="work-links-body">
      <p>새 연결 이후 선행 결과를 채택할 때마다 후속 작업 하나를 만듭니다. 결과 전체가 후속 업무 입력에 포함되며, 후속 업무가 LIVE LLM이면 모델 서비스로 전송됩니다. 개인정보 등 공유 가능한 내용인지 확인하세요. 선행 결과는 사람이 채택한 것이며 사실 확인을 보증하지 않습니다.</p>
      <p>각 연결은 독립적으로 작동합니다. 연결을 중지해도 이미 만들어진 작업은 유지됩니다. 반복 일정을 중지해도 이 연결은 계속 작동하므로, 자동 후속 작업도 멈추려면 연결을 중지하세요.</p>
      <div class="work-link-form"><label>선행 업무<select v-model="source" @change="consent=false"><option value="">업무 선택</option><option v-for="task in textTasks" :key="task.task_id" :value="task.task_id">{{ task.name }} · {{ task.task_id }}</option></select></label><span class="link-arrow">→</span><label>후속 업무<select v-model="target" @change="consent=false"><option value="">업무 선택</option><option v-for="task in textTasks.filter(t=>t.task_id!==source)" :key="task.task_id" :value="task.task_id">{{ task.name }} · {{ task.model_mode }}</option></select></label></div>
      <p v-if="targetTask">후속 실행 모드: <b>{{ targetTask.model_mode }}</b> · {{ targetTask.model_mode==='LIVE_LLM' ? '선행 결과가 모델 서비스로 전송됩니다.' : 'MOCK 예시이며 실제 업무 결과가 아닙니다.' }}</p>
      <label class="contract-check"><input type="checkbox" v-model="consent"> 연결 방향과 후속 입력의 전달 범위를 확인했습니다.</label>
      <button @click="create" :disabled="busy || !source || !target || source===target || !consent">이 업무 연결하기</button>
      <p v-if="textTasks.length<2">사용자 텍스트 업무를 두 개 이상 등록하면 연결할 수 있습니다.</p>
      <p v-if="error" class="failure" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
      <div v-if="links.length" class="work-link-list"><h3>등록된 연결</h3><div v-for="link in links" :key="link.link_id" class="work-link-row"><div><b>{{ name(link.source_task_id) }} → {{ name(link.target_task_id) }}</b><small>{{ link.enabled ? '연결 중' : '중지됨' }} · 생성된 후속 작업 {{ link.delivery_count }}건 · v{{ link.version }}</small></div><button class="secondary" @click="toggle(link)" :disabled="busy">{{ link.enabled ? '연결 중지' : '연결 재개' }}</button><button v-if="link.latest_work_id" class="secondary" @click="emit('open-work',link.latest_work_id)">최근 후속 작업 보기 →</button></div></div>
    </div>
  </section>
</template>
