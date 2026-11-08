<script setup>
import { computed } from 'vue'
const props=defineProps({text:String})
const segments=computed(()=>{
  const text=props.text || '',result=[],pattern=/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g
  let start=0,match
  while((match=pattern.exec(text))){
    result.push({text:text.slice(start,match.index)})
    let url=null
    try{const parsed=new URL(match[2]);if(['http:','https:'].includes(parsed.protocol))url=parsed.href}catch{}
    result.push({text:match[1],url});start=pattern.lastIndex
  }
  result.push({text:text.slice(start)});return result
})
</script>
<template><div class="report-text"><template v-for="(segment,i) in segments" :key="i"><a v-if="segment.url" :href="segment.url" target="_blank" rel="noopener noreferrer">{{ segment.text }}</a><template v-else>{{ segment.text }}</template></template></div></template>
<style scoped>.report-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85}.report-text a{color:#345f46;text-decoration:underline;text-underline-offset:3px}</style>
