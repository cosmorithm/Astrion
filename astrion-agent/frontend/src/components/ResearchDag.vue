<script setup>
import { computed, ref, watch } from 'vue'
import { request } from '../api.js'
import { layoutResearchRuns } from './researchDag.js'

const props = defineProps({ projectId: String, selected: String, busy: Boolean })
const emit = defineEmits(['open', 'error', 'loading'])
const graph = ref(null)
const loading = ref(false)
const layout = computed(() => layoutResearchRuns(graph.value))
const status = { completed: '已完成', no_tool_call: '已返回回答', needs_input: '需要补充信息',
  incomplete_plan: '有未完成子任务', step_limit: '达到步数上限', error: '运行失败' }
let generation = 0
async function load() {
  if (!props.projectId) { graph.value = null; loading.value = false; emit('loading', false); return }
  const requestId = ++generation
  const project = props.projectId
  loading.value = true
  emit('loading', true)
  try {
    const data = await request(`/api/projects/${encodeURIComponent(project)}/runs/graph?limit=500`)
    if (requestId === generation && project === props.projectId) graph.value = data
  } catch (error) {
    if (requestId === generation) { graph.value = null; emit('error', error.message) }
  } finally {
    if (requestId === generation) { loading.value = false; emit('loading', false) }
  }
}
function open(node) {
  if (!props.busy && !loading.value) emit('open', node.run_id, node.continuation_status)
}
function label(node) { return status[node.status] || node.status || '状态未知' }
function recovery(node) {
  if (node.continuation_status === 'checkpoint_present' || node.can_continue === true) return '有恢复记录'
  if (node.continuation_status === 'unavailable' || node.can_continue === false) return '无恢复记录'
  return '恢复状态待验证'
}
function date(value) { const d = new Date(value); return value && !Number.isNaN(d.getTime()) ? d.toLocaleString() : '时间未知' }
watch(() => props.projectId, () => { generation++; graph.value = null; load() }, { immediate: true })
defineExpose({ refresh: load })
</script>

<template>
  <details class="research-dag mb-3">
    <summary>查看研究版本关系图</summary>
    <div class="small text-muted mt-2">连线表示从旧版本继续研究；点击版本可查看报告。是否能够恢复由后端进一步验证。</div>
    <p v-if="graph?.truncated" class="small mt-2 mb-1">仅显示最近 {{ graph.limit }} 条；更早记录可在研究历史中继续加载。</p>
    <p v-if="graph?.skipped_records" class="small mt-2 mb-1">有 {{ graph.skipped_records }} 条历史记录无法读取或不属于当前数据库，未显示在图中。</p>
    <p v-if="layout.missingCount" class="small mt-2 mb-1">{{ layout.missingCount }} 个版本的父版本不在当前结果中，可能已被截断或缺失。</p>
    <p v-if="layout.invalidCount" class="small mt-2 mb-1">{{ layout.invalidCount }} 个版本的父子关系存在循环或依赖循环，已停止绘制相关连线；记录仍可查看。</p>
    <div v-if="loading" class="small mt-2">正在加载版本…</div>
    <div v-else-if="!layout.nodes.length" class="small text-muted mt-2">当前项目还没有可查看的研究版本。</div>
    <div v-else class="dag-scroll mt-2" aria-label="研究版本关系图">
      <div class="dag-canvas" :style="{ width: `${layout.width}px`, height: `${layout.height}px` }">
        <svg class="dag-edges" :width="layout.width" :height="layout.height" aria-hidden="true">
          <defs><marker id="research-dag-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M 0 0 L 6 3 L 0 6 z" class="arrow" /></marker></defs>
          <path v-for="edge in layout.edges" :key="edge.id" :d="`M ${edge.x1} ${edge.y1} C ${edge.x1 + 20} ${edge.y1}, ${edge.x2 - 20} ${edge.y2}, ${edge.x2} ${edge.y2}`" marker-end="url(#research-dag-arrow)" />
        </svg>
        <button v-for="node in layout.nodes" :key="node.run_id" type="button"
          class="dag-node" :class="{ active: selected === node.run_id }" :disabled="busy || loading"
          :style="{ left: `${node.x}px`, top: `${node.y}px` }" @click="open(node)">
          <span class="node-task" :title="node.task || '未记录研究问题'">{{ node.task || '未记录研究问题' }}</span>
          <span class="node-meta">{{ label(node) }} · {{ date(node.recorded_at) }}</span>
          <span class="node-meta">{{ node.has_report ? '有报告' : '无报告' }} · {{ recovery(node) }}</span>
          <span v-if="node.missingParent" class="node-alert">{{ node.parent_invalid ? '父版本标识无效' : '父版本不在当前图中' }}</span>
          <span v-if="node.invalidRelation" class="node-alert">版本关系异常</span>
        </button>
      </div>
    </div>
  </details>
</template>
<style scoped>
.research-dag { border: 1px solid #e0e4ea; border-radius: .45rem; padding: .65rem .8rem; background: #fff; }
summary { cursor: pointer; font-size: .9rem; }
.dag-scroll { overflow: auto; max-height: 32rem; }
.dag-canvas { position: relative; }
.dag-edges { position: absolute; inset: 0; pointer-events: none; }
.dag-edges path { fill: none; stroke: #aeb8c5; stroke-width: 1.5; }
.dag-edges .arrow { fill: #aeb8c5; stroke: none; }
.dag-node { position: absolute; width: 210px; min-height: 80px; display: flex; flex-direction: column; text-align: left; gap: .2rem; padding: .5rem .65rem; border: 1px solid #dce1e8; border-radius: .4rem; background: white; color: #26313d; }
.dag-node:hover, .dag-node.active { border-color: #587da7; background: #f5f9ff; }
.node-task { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: .83rem; font-weight: 600; }
.node-meta, .node-alert { font-size: .72rem; color: #667385; }
.node-alert { color: #9a5c18; }
</style>
