<script setup>
import { computed, ref, watch } from 'vue'
import { request } from '../api.js'

const props = defineProps({ projectId: String, busy: Boolean, selected: String, selectedDeletable: Boolean })
const emit = defineEmits(['open', 'error', 'loading', 'deleted'])
const runs = ref([])
const nextOffset = ref(null)
const loading = ref(false)
const confirming = ref(false)
const statusLabels = { error: '失败', step_limit: '步数耗尽', completed: '已完成',
  no_tool_call: '已返回回答', incomplete_plan: '有未完成子任务', needs_input: '需要补充信息' }
function displayDate(value) {
  const date = value ? new Date(value) : null
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : '时间未知'
}
let generation = 0
watch(loading, value => emit('loading', value), { flush: 'sync' })
const canDelete = computed(() => {
  if (!props.selected) return false
  const row = runs.value.find(run => run.run_id === props.selected)
  return (row ? row.can_delete : props.selectedDeletable) === true
})
watch(() => props.selected, () => { confirming.value = false })

async function load(more = false) {
  const project = props.projectId
  if (!project) return
  const requestGeneration = ++generation
  loading.value = true
  try {
    const data = await request(`/api/projects/${encodeURIComponent(project)}/runs?offset=${more ? nextOffset.value : 0}`)
    if (requestGeneration !== generation || project !== props.projectId) return
    runs.value = more ? [...runs.value, ...data.runs] : data.runs
    nextOffset.value = data.next_offset
  } catch (error) {
    if (requestGeneration === generation && project === props.projectId) emit('error', error.message)
  } finally { if (requestGeneration === generation) loading.value = false }
}
async function open(event) {
  const id = event.target.value
  confirming.value = false
  const requestGeneration = ++generation
  if (!id) { loading.value = false; emit('open', null); return }
  const project = props.projectId
  loading.value = true
  try {
    const data = await request(`/api/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(id)}`)
    if (requestGeneration === generation && project === props.projectId) emit('open', data)
  } catch (error) {
    if (requestGeneration === generation && project === props.projectId) emit('error', error.message)
  } finally { if (requestGeneration === generation) loading.value = false }
}
async function removeSelected() {
  if (!confirming.value || !canDelete.value || !props.projectId || !props.selected || props.busy || loading.value) return
  const project = props.projectId
  const id = props.selected
  const requestGeneration = ++generation
  loading.value = true
  try {
    await request(`/api/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (requestGeneration !== generation || project !== props.projectId) return
    confirming.value = false
    // Parent clears an open deleted version and refreshes the DAG/compare panels.
    emit('deleted', { projectId: project, runId: id })
    await load()
  } catch (error) {
    if (requestGeneration === generation && project === props.projectId) emit('error', error.message)
  } finally { if (requestGeneration === generation) loading.value = false }
}
watch(() => props.projectId, () => {
  generation++
  runs.value = []; nextOffset.value = null; confirming.value = false; loading.value = false
  load()
}, { immediate: true })
defineExpose({ refresh: () => load() })
</script>

<template>
  <div class="mb-3">
    <label class="field-label" for="research-history">研究历史 · 选择已有版本查看或继续完善</label>
    <div class="d-flex gap-2">
      <select id="research-history" class="form-select" :value="selected || ''" :disabled="busy || loading" @change="open">
        <option value="">新建研究</option>
        <option v-for="run in runs" :key="run.run_id" :value="run.run_id">
          {{ displayDate(run.recorded_at) }} · {{ statusLabels[run.status] || run.status || '状态未知' }} · {{ run.task }}{{ run.can_continue ? '' : '（无可恢复上下文）' }}
        </option>
      </select>
      <button v-if="nextOffset !== null" type="button" class="btn subtle-btn text-nowrap" :disabled="busy || loading" @click="load(true)">更早记录</button>
      <button v-if="canDelete && !confirming" type="button" class="btn btn-outline-danger text-nowrap" :disabled="busy || loading" @click="confirming = true">清理失败记录</button>
    </div>
    <div v-if="confirming && canDelete" class="alert alert-warning mt-2 mb-0" role="alert">
      <div>确认清理这条失败或无报告的步数耗尽记录？此操作删除该次记录及其运行日志，无法撤销；项目资料、上下文归档和已保存产物会保留。如该记录已有后续版本或正在被续写，系统会拒绝清理。</div>
      <div class="d-flex gap-2 mt-2">
        <button type="button" class="btn btn-sm btn-danger" :disabled="busy || loading" @click="removeSelected">确认清理</button>
        <button type="button" class="btn btn-sm btn-outline-secondary" :disabled="loading" @click="confirming = false">取消</button>
      </div>
    </div>
  </div>
</template>
