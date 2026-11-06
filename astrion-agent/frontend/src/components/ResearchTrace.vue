<script setup>
import { computed, ref, watch } from 'vue'
import { request } from '../api.js'
const props = defineProps({ projectId: String, runId: String, busy: Boolean, live: Array })
const events = ref([])
const next = ref(null)
const loading = ref(false)
const error = ref('')
let requestNumber = 0
const shown = computed(() => props.busy || !props.runId ? (props.live || []) : events.value)
const relevant = new Set(['model.call.started', 'model.call.finished', 'model.call.failed', 'tool.call.started', 'tool.call.finished', 'memory.compaction.finished', 'decision.finished'])
const visible = computed(() => shown.value.filter(item => relevant.has(item.event?.type)))
const kinds = { 'model.call.started': '模型输入', 'model.call.finished': '模型输出', 'model.call.failed': '模型失败', 'tool.call.started': '工具输入', 'tool.call.finished': '工具输出', 'memory.compaction.finished': '上下文压缩', 'decision.finished': '决策结果' }
function title(item) {
  const e = item.event; const p = e.payload || {}
  return `${item.role === 'root' ? '主 Agent' : item.role === 'router' ? '委派路由器' : item.worker_id} · 第 ${e.step ?? '—'} 轮 · ${kinds[e.type]}${p.name ? ' · ' + p.name : ''}${p.purpose ? ' · ' + p.purpose : ''}`
}
function detail(item) {
  const e = item.event; const p = e.payload || {}
  if (e.type === 'model.call.started' && !p.request_snapshot) return '该历史运行只记录了输入统计，未保存完整模型输入，无法准确还原。\n' + JSON.stringify(p, null, 2)
  return JSON.stringify(p, null, 2)
}
async function load(more = false) {
  if (!props.runId || !props.projectId || (more && loading.value)) return
  const requestId = ++requestNumber
  const id = props.runId; const project = props.projectId
  loading.value = true; error.value = ''
  try {
    const data = await request(`/api/projects/${project}/runs/${id}/events?offset=${more ? next.value : 0}&limit=10`)
    if (requestId !== requestNumber || id !== props.runId || project !== props.projectId) return
    events.value = more ? [...events.value, ...data.events] : data.events
    next.value = data.next_offset
  } catch (e) { if (requestId === requestNumber) error.value = e.message }
  finally { if (requestId === requestNumber) loading.value = false }
}
watch(() => [props.projectId, props.runId, props.busy], () => {
  requestNumber++
  loading.value = false
  events.value = []; next.value = null
  if (!props.busy && props.runId) load()
}, { immediate: true })
</script>
<template>
  <div class="trace-detail">
    <div class="small text-muted mb-2">展开查看输入、输出和工具结果。内容可能包含项目资料，请谨慎分享。</div>
    <div v-if="error" class="text-danger small">{{ error }}</div>
    <details v-for="(item, index) in visible" :key="`${item.event.event_id}-${index}`" class="mb-2">
      <summary>{{ title(item) }}</summary>
      <pre class="trace-json">{{ detail(item) }}</pre>
    </details>
    <div v-if="!visible.length && !loading" class="small text-muted">尚无可查看的事件。</div>
    <button v-if="!busy && next !== null" class="btn subtle-btn btn-sm" :disabled="loading" @click="load(true)">加载更多事件</button>
    <span v-if="loading" class="small text-muted">正在加载…</span>
  </div>
</template>
<style scoped>
.trace-json { max-height: 32rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: .75rem; background: #f5f6f8; padding: .6rem; border-radius: .3rem; }
summary { cursor: pointer; font-size: .85rem; }
</style>
