<script setup>
import { ref, onMounted } from 'vue'
const props=defineProps({base:{type:String,required:true},taskId:{type:String,required:true},artifact:{type:Object,required:true}})
const cases=ref([]), suites=ref([]), selected=ref([]), busy=ref(false), message=ref(''), error=ref('')
const step=ref(props.artifact.content.steps?.[0]?.step_id || ''), input=ref(''), expected=ref(''), feedback=ref(''), required=ref(''), forbidden=ref('')
const lines = value => value.split('\n').map(x=>x.trim()).filter(Boolean)
async function load() {
  const [a,b]=await Promise.all([fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/regression-cases`),fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/regression-suites`)])
  if(!a.ok || !b.ok) throw Error('평가 사례를 불러오지 못했습니다.')
  cases.value=await a.json();suites.value=await b.json()
}
async function createCase() {
  busy.value=true;message.value='';error.value=''
  try { const r=await fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/regression-cases`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({artifact_id:props.artifact.artifact_id,step_id:step.value,input_text:input.value,expected_behavior:expected.value,feedback:feedback.value,required_phrases:lines(required.value),forbidden_phrases:lines(forbidden.value),idempotency_key:crypto.randomUUID()})}); const d=await r.json(); if(!r.ok) throw Error(typeof d.detail==='string'?d.detail:'입력 내용을 확인하세요.'); await load();selected.value=[d.case_id];message.value='사람이 작성한 평가 사례를 저장했습니다. 아직 방법 비교는 실행하지 않았습니다.' }
  catch(e){error.value=e.message}finally{busy.value=false}
}
async function freeze() {
  busy.value=true;message.value='';error.value=''
  try {const r=await fetch(`${props.base}/tasks/${encodeURIComponent(props.taskId)}/regression-suites`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({case_ids:selected.value,idempotency_key:crypto.randomUUID()})});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'평가셋 저장 실패');await load();message.value='선택한 사례를 고정 평가셋으로 저장했습니다. 자동 개선이나 승격은 수행되지 않았습니다.'}
  catch(e){error.value=e.message}finally{busy.value=false}
}
onMounted(()=>load().catch(e=>{error.value=e.message}))
</script>
<template>
  <section class="text-regression" aria-label="사람의 피드백을 평가 사례로 저장">
    <div class="text-regression-head"><div><small>HUMAN FEEDBACK / 고정 사례</small><h4>이 결과를 다음 평가에 활용</h4></div><span>평가셋 준비</span></div>
    <p>검토 의견을 바탕으로 기대 결과와 검사할 문구를 직접 정의합니다. 원래 입력은 복사되지 않습니다. 평가에 사용할 익명화된 예시를 새로 입력하세요.</p>
    <p>이메일·전화번호·주민등록번호로 보이는 일부 패턴은 차단하지만 모든 개인정보를 찾아내지는 못합니다. 입력·피드백·문구에서 개인정보를 직접 제거해 주세요.</p>
    <div class="text-regression-form"><label>검사할 단계<select v-model="step"><option v-for="item in artifact.content.steps" :key="item.step_id" :value="item.step_id">{{ item.step_id }}</option></select></label><label>익명화한 평가 입력<textarea v-model="input" rows="3" maxlength="8000" placeholder="예: 익명 고객이 배송 지연을 문의함"></textarea></label><label>기대 결과<textarea v-model="expected" rows="2" maxlength="2000" placeholder="어떤 결과가 나와야 하나요?"></textarea></label><label>피드백<textarea v-model="feedback" rows="2" maxlength="2000" placeholder="이 사례가 필요한 이유를 개인정보 없이 입력하세요"></textarea></label><div class="text-terms"><label>반드시 포함할 문구 <small>한 줄에 하나</small><textarea v-model="required" rows="2" placeholder="배송"></textarea></label><label>포함되면 안 되는 문구 <small>한 줄에 하나</small><textarea v-model="forbidden" rows="2" placeholder="환불 완료"></textarea></label></div><p>문구는 대소문자를 구분하지 않고 포함 여부만 검사합니다. 최소 하나의 문구가 필요합니다. 기대 결과의 의미는 자동 판단하지 않습니다.</p><button @click="createCase" :disabled="busy || !input.trim() || !expected.trim() || !feedback.trim() || !(required.trim() || forbidden.trim())">평가 사례 저장</button></div>
    <p v-if="error" class="failure" role="alert">{{ error }}</p><p v-if="message" class="detail-note" role="status">{{ message }}</p>
    <div v-if="cases.length" class="text-cases"><h4>이 업무의 평가 사례 · {{ cases.length }}개</h4><label v-for="c in cases" :key="c.case_id" class="text-case"><input type="checkbox" v-model="selected" :value="c.case_id"><span><b>{{ c.snapshot.step_id }} · {{ c.snapshot.expected_behavior }}</b><small>입력: {{ c.snapshot.input_text }} · 필수: {{ c.snapshot.required_phrases.join(', ') || '없음' }} · 금지: {{ c.snapshot.forbidden_phrases.join(', ') || '없음' }}</small><small>검토 {{ c.snapshot.review_decision }} · {{ c.snapshot_sha256.slice(0,12) }}</small></span></label><button class="secondary" @click="freeze" :disabled="busy || !selected.length">선택한 {{ selected.length }}개 사례를 평가셋으로 고정</button></div>
    <div v-if="suites.length" class="text-suites"><h4>고정 평가셋</h4><p v-for="suite in suites" :key="suite.suite_id">v{{ suite.version }} · {{ suite.snapshot.cases.length }}개 사례 · SHA-256 {{ suite.snapshot_sha256.slice(0,16) }}</p></div>
    <p>이 사례와 평가셋은 저장된 기록입니다. 기존 방법과 후보를 같은 입력에서 비교하고 결과를 반영하는 경로는 아직 연결되지 않았습니다.</p>
  </section>
</template>
