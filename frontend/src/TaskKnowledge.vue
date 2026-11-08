<script setup>
import { ref,watch,onUnmounted,computed } from 'vue'
const props=defineProps({base:String,taskId:String})
const emit=defineEmits(['changed','open-work'])
const entries=ref([]),error=ref(''),busy=ref(false),notes=ref({}),query=ref(''),filterKind=ref('ALL'),kind=ref('LESSON'),title=ref(''),text=ref(''),heading=ref('')
const labels={PROPOSED:'검토 후보',APPLY:'사용 중',REJECT:'기각됨',RETIRE:'사용 중지'}
const kinds={LESSON:'작업 교훈',SOURCE_EXCERPT:'원문 직접 인용',CALCULATION:'Python 계산식',CHECKLIST:'보고서 필수 항목'}
const limit=ref(8)
const matched=computed(()=>entries.value.filter(e=>(filterKind.value==='ALL' || e.kind===filterKind.value) && (!query.value || [e.title,JSON.stringify(e.payload),labels[e.decision],kinds[e.kind],e.preview?.value].join(' ').includes(query.value))).sort((a,b)=>(a.decision==='APPLY'?0:1)-(b.decision==='APPLY'?0:1) || (a.kind==='LESSON'?1:0)-(b.kind==='LESSON'?1:0)))
const visible=computed(()=>matched.value.slice(0,limit.value))
watch([query,filterKind],()=>{limit.value=8})
let sequence=0
async function load(){const id=props.taskId,seq=++sequence;try{const r=await fetch(`${props.base}/tasks/${encodeURIComponent(id)}/knowledge`);if(!r.ok)throw Error('태스크 지식을 불러오지 못했습니다.');const data=await r.json();if(seq===sequence){entries.value=data;error.value=''}}catch(e){if(seq===sequence)error.value=e.message}}
watch(()=>props.taskId,load,{immediate:true});onUnmounted(()=>{sequence++})
async function request(path,body){const r=await fetch(props.base+path,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const data=await r.json();if(!r.ok)throw Error(data.detail || '처리하지 못했습니다.');return data}
async function decide(entry,decision){busy.value=true;try{await request(`/knowledge/${entry.entry_id}/decision`,{decision,expected_sha256:entry.sha256,note:notes.value[entry.entry_id],idempotency_key:crypto.randomUUID()});await load();emit('changed')}catch(e){error.value=e.message}finally{busy.value=false}}
async function create(){busy.value=true;try{await request(`/tasks/${props.taskId}/knowledge`,{kind:kind.value,title:title.value,payload:kind.value==='LESSON'?{text:text.value}:{heading:heading.value,instruction:text.value},idempotency_key:crypto.randomUUID()});title.value='';text.value='';heading.value='';await load()}catch(e){error.value=e.message}finally{busy.value=false}}
async function importFeedback(){busy.value=true;try{await request(`/tasks/${props.taskId}/knowledge/import-feedback`);await load()}catch(e){error.value=e.message}finally{busy.value=false}}
</script>
<template>
  <details class="task-knowledge"><summary>오래 유지할 태스크 지식 · 사용 중 {{ entries.filter(e=>e.decision==='APPLY' && e.scope_matches && !e.preview?.invalid).length }} / 후보 {{ entries.filter(e=>e.decision==='PROPOSED').length }}</summary>
    <p>적용한 지식은 최근 의견이 늘어나도 유지합니다. 입력·태스크 버전·원문이 달라지면 다시 검사합니다. AI 후보는 자동 적용하지 않습니다.</p>
    <p>원문 인용은 저장한 텍스트와의 일치, 계산은 Python Decimal로 확인합니다. 작업 교훈은 사용자가 승인한 절차이며 사실 검증을 뜻하지 않습니다. 필수 항목 검사는 누락·형식 검사입니다.</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div class="actions"><button class="secondary" @click="load" :disabled="busy">새로고침</button><button class="secondary" @click="importFeedback" :disabled="busy">과거 AI 의견도 검토 후보로 보존</button></div>
    <label>종류별 보기<select v-model="filterKind"><option value="ALL">전체</option><option v-for="(label,value) in kinds" :key="value" :value="value">{{ label }}</option></select></label>
    <label>지식 찾기<input v-model="query" type="search" placeholder="제목·내용·상태"></label>
    <article v-for="entry in visible" :key="entry.entry_id">
      <b>{{ entry.title }}</b><small>{{ kinds[entry.kind] }} · {{ labels[entry.decision] }} · 업무 v{{ entry.task_version }}</small>
      <button v-if="entry.origin_work_id" class="secondary" @click="emit('open-work',entry.origin_work_id,entry.origin_run_id)">이 의견이 나온 보고서와 근거 보기</button>
      <p v-if="entry.kind==='LESSON'" class="knowledge-text">{{ entry.payload.text }}</p>
      <p v-if="entry.kind==='CHECKLIST'">{{ entry.payload.heading }} — {{ entry.payload.instruction }}</p>
      <p v-if="entry.kind==='CALCULATION' && !entry.preview?.invalid">계산 결과: <strong>{{ entry.preview?.value }} {{ entry.preview?.unit }}</strong></p>
      <blockquote v-if="entry.kind==='SOURCE_EXCERPT'">{{ entry.payload.quote }} <a v-if="entry.preview?.url" :href="entry.preview.url" target="_blank" rel="noopener noreferrer">저장한 원문 출처 ↗</a></blockquote>
      <p v-if="!entry.scope_matches || entry.preview?.invalid" class="knowledge-warning">{{ !entry.scope_matches ? '입력이 달라 현재 실행에서 제외합니다.' : '원문 또는 계산식을 다시 확인해야 합니다.' }}</p>
      <details v-if="entry.kind==='CALCULATION'"><summary>계산식과 근거 확인</summary><pre>{{ JSON.stringify(entry.payload,null,2) }}</pre></details>
      <template v-if="entry.decision!=='REJECT'"><label>검토 이유<textarea v-model="notes[entry.entry_id]" rows="2" maxlength="2000" placeholder="확인한 근거나 중지 이유를 적으세요."></textarea></label><div class="actions"><button v-if="['PROPOSED','RETIRE'].includes(entry.decision)" @click="decide(entry,'APPLY')" :disabled="busy || !notes[entry.entry_id]?.trim() || !entry.scope_matches || !entry.task_version_active || entry.preview?.invalid">검토 후 다음 실행에 적용</button><button v-if="entry.decision==='PROPOSED'" class="secondary" @click="decide(entry,'REJECT')" :disabled="busy || !notes[entry.entry_id]?.trim()">기각</button><button v-if="entry.decision==='APPLY'" class="secondary" @click="decide(entry,'RETIRE')" :disabled="busy || !notes[entry.entry_id]?.trim()">다음 실행에서 사용 중지</button></div></template>
    </article>
    <button v-if="matched.length>limit" class="secondary" @click="limit+=8">지식 후보 더 보기 · {{ visible.length }}/{{ matched.length }}</button>
    <details><summary>직접 교훈·필수 항목 등록</summary><label>종류<select v-model="kind"><option value="LESSON">작업 교훈</option><option value="CHECKLIST">보고서 필수 항목</option></select></label><label>제목<input v-model="title" maxlength="150"></label><label v-if="kind==='CHECKLIST'">보고서 제목<input v-model="heading" maxlength="150"></label><label>내용<textarea v-model="text" rows="3" maxlength="2000"></textarea></label><button @click="create" :disabled="busy || !title.trim() || !text.trim() || (kind==='CHECKLIST' && !heading.trim())">후보로 저장</button></details>
  </details>
</template>
<style scoped>.task-knowledge{margin-top:22px;padding:16px;background:#f3f6eb;border-left:3px solid #6d8760;font-size:12px}.task-knowledge summary{cursor:pointer;font-weight:700;color:#304e3a}.task-knowledge p{line-height:1.8}.task-knowledge article{padding:16px 0;border-bottom:1px solid #d5dfca;overflow-wrap:anywhere}.task-knowledge small{display:block;color:#64765b;margin-top:6px}.task-knowledge label{display:grid;gap:6px;margin:12px 0}.task-knowledge input,.task-knowledge textarea,.task-knowledge select{width:100%;padding:8px;border:1px solid #ccd7c2;background:#fff;font:inherit;color:inherit}.knowledge-text{white-space:pre-wrap}.knowledge-warning{color:#825b2f}.task-knowledge pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:10px;max-height:240px;overflow:auto}.task-knowledge blockquote{margin:12px 0;padding-left:12px;border-left:2px solid #b4c99b}</style>
