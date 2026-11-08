<script setup>
import { ref } from 'vue'

const props=defineProps({base:{type:String,required:true},taskId:{type:String,required:true}})
const open=ref(false), current=ref(null), draft=ref(''), busy=ref(false), error=ref(''), message=ref('')
async function load() {
  error.value='';message.value='';busy.value=true
  try {
    const response=await fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/text-source`)
    if(!response.ok) throw Error('입력을 불러오지 못했습니다.')
    current.value=await response.json();draft.value=current.value.source_text
  } catch(e) {error.value=e.message} finally {busy.value=false}
}
async function toggle() {open.value=!open.value;if(open.value) await load()}
async function save() {
  busy.value=true;error.value='';message.value=''
  try {
    const response=await fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/text-source`,{
      method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_text:draft.value,expected_version:current.value.version,idempotency_key:crypto.randomUUID()})})
    const data=await response.json()
    if(!response.ok) throw Error(response.status===409?'입력이 바뀌었거나 내용이 같습니다. 다시 불러와 확인해 주세요.':(typeof data.detail==='string'?data.detail:'저장하지 못했습니다.'))
    const version=data.version
    await load();message.value=`입력 v${version}을 저장했습니다. 다음 실행부터 사용합니다.`
  } catch(e) {error.value=e.message} finally {busy.value=false}
}
</script>

<template>
  <div class="text-source-editor">
    <button class="secondary" @click="toggle" :aria-expanded="open">{{ open ? '업무 입력 닫기 ↑' : '업무 입력 확인·변경 →' }}</button>
    <div v-if="open" class="text-source-panel">
      <p v-if="current"><b>현재 입력 v{{ current.version }}</b> <small>· {{ current.source_sha256.slice(0,12) }}</small></p>
      <p>이 업무가 다음에 수행할 텍스트입니다. 이미 만들어진 작업과 재시도는 당시 입력을 유지합니다.</p>
      <p>LIVE LLM 업무는 다음 실행 시 입력과 같은 버전의 과거 결과·실패·검토 의견이 모델 서비스로 전송될 수 있습니다. 예약이 켜져 있다면 일정에 따라 자동 실행됩니다.</p>
      <label>다음 실행에 사용할 텍스트<textarea v-model="draft" rows="4" maxlength="8000" :disabled="busy || !current"></textarea></label>
      <div class="text-source-actions"><button @click="save" :disabled="busy || !current || !draft.trim() || draft===current.source_text">새 입력 저장</button><button class="secondary" @click="load" :disabled="busy">현재 입력 다시 불러오기</button></div>
      <p v-if="error" class="failure" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    </div>
  </div>
</template>
