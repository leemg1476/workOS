<script setup>
import { ref, computed } from 'vue'
const props=defineProps({base:{type:String,required:true},taskId:{type:String,required:true}})
const emit=defineEmits(['edit-candidate','changed'])
const open=ref(false), context=ref(null), changes=ref([]), note=ref(''), acknowledged=ref(false), busy=ref(false), error=ref(''), message=ref('')
const candidate=computed(()=>context.value?.latest_revision>context.value?.registered_revision)
const differences=computed(()=>{
  if(!candidate.value) return []
  const old=context.value.plan, next=context.value.latest_plan
  return [['name','업무 이름'],['objective','목표'],['evaluation_criteria','평가 기준']]
    .filter(([key])=>JSON.stringify(old[key])!==JSON.stringify(next[key]))
    .map(([key,label])=>({label,old:Array.isArray(old[key])?old[key].join('\n'):old[key],next:Array.isArray(next[key])?next[key].join('\n'):next[key]}))
})
const unsupported=computed(()=>candidate.value && ['steps','expected_result','open_questions'].some(key=>JSON.stringify(context.value.plan[key])!==JSON.stringify(context.value.latest_plan[key])))
const lastApply=computed(()=>changes.value.find(x=>x.decision==='APPLY' && x.to_version===context.value?.version && !changes.value.some(r=>r.reversed_change_id===x.change_id)))
async function load(){
  const [a,b]=await Promise.all([fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/text-experiment-context`),fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/text-contract-changes`)])
  if(!a.ok || !b.ok) throw Error('업무 계약을 불러오지 못했습니다.')
  const next=await a.json()
  if(context.value && (next.version!==context.value.version || next.latest_revision!==context.value.latest_revision || next.latest_plan_sha256!==context.value.latest_plan_sha256)) acknowledged.value=false
  context.value=next;changes.value=await b.json()
}
async function toggle(){open.value=!open.value;if(open.value){error.value='';try{await load()}catch(e){error.value=e.message}}}
async function action(rollback=false){
  busy.value=true;error.value='';message.value=''
  try{
    const url=rollback?`${props.base}/text-contract-changes/${lastApply.value.change_id}/rollback`:`${props.base}/tasks/${encodeURIComponent(props.taskId)}/text-contract-changes`
    const body={expected_version:context.value.version,note:note.value,idempotency_key:crypto.randomUUID()}
    if(!rollback){body.candidate_revision=context.value.latest_revision;body.candidate_sha256=context.value.latest_plan_sha256}
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json()
    if(!r.ok) throw Error(typeof data.detail==='string'?data.detail:'계약을 변경하지 못했습니다.')
    note.value='';acknowledged.value=false;await load();emit('changed');message.value=rollback?'이전 업무 계약으로 되돌렸습니다.':'사람의 결정으로 새 업무 계약을 적용했습니다.'
  }catch(e){error.value=e.message;await load().catch(()=>{})}finally{busy.value=false}
}
</script>
<template>
  <div class="contract-editor">
    <button class="secondary" @click="toggle">{{ open ? '계약 변경 닫기 ↑' : '목표·평가 기준 변경 →' }}</button>
    <div v-if="open" class="contract-panel">
      <p>활성 업무 v{{ context?.version }}의 목표와 평가 기준을 사람의 결정으로 바꿉니다. 이전 실행·판정·고정 사례는 보존하며, 기존 평가를 통과했거나 성능이 향상됐다는 뜻은 아닙니다. 새 결과도 사람이 검토해야 합니다.</p>
      <button v-if="context" class="secondary" @click="emit('edit-candidate',context.design_id)">업무 초안에서 수정 ↑</button>
      <button class="secondary" @click="load().catch(e=>error=e.message)">수정한 초안 새로고침</button>
      <p v-if="context && !candidate">업무 초안을 수정해 저장한 후 변경 내용을 확인하세요.</p>
      <template v-if="candidate">
        <p>현재 초안 v{{ context.registered_revision }} → 후보 v{{ context.latest_revision }}</p>
        <p v-if="unsupported" class="failure">이 경로는 이름·목표·평가 기준만 바꿀 수 있습니다. 단계나 기대 결과는 별도의 고정 사례 비교로 다뤄 주세요.</p>
        <p v-else-if="!differences.length">적용할 목표 또는 평가 기준 변경이 없습니다.</p>
        <div v-for="item in differences" :key="item.label" class="contract-diff"><b>{{ item.label }}</b><div class="comparison-head"><div><small>현재</small><span>{{ item.old }}</span></div><div><small>변경 후</small><span>{{ item.next }}</span></div></div></div>
      </template>
      <template v-if="candidate && differences.length && !unsupported">
        <label class="contract-check"><input type="checkbox" v-model="acknowledged"> 변경 내용과 평가 미수행 사실을 확인했습니다.</label>
      </template>
      <label v-if="(candidate && differences.length && !unsupported) || lastApply" class="contract-note">결정 이유<textarea v-model="note" rows="2" maxlength="2000" placeholder="변경 또는 복구 이유를 입력하세요"></textarea></label>
      <button v-if="candidate && differences.length && !unsupported" @click="action(false)" :disabled="busy || !acknowledged || !note.trim()">확인한 계약 변경 적용</button>
      <button v-if="lastApply" class="secondary" @click="action(true)" :disabled="busy || !note.trim()">직전 계약 변경 되돌리기</button>
      <p v-if="error" class="failure" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
      <details v-if="changes.length"><summary>계약 변경 이력 · {{ changes.length }}건</summary><p v-for="change in changes" :key="change.change_id">{{ change.decision==='APPLY' ? '적용' : '복구' }} · 업무 v{{ change.from_version }} → v{{ change.to_version }} · {{ change.note }}</p></details>
    </div>
  </div>
</template>
