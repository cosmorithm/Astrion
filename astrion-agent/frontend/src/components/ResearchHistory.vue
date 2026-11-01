<script setup>
import { ref, watch } from 'vue'
import { request } from '../api.js'

const props = defineProps({ projectId: String, busy: Boolean, selected: String })
const emit = defineEmits(['open', 'error', 'loading'])
const runs = ref([])
const nextOffset = ref(null)
const loading = ref(false)
watch(loading, value => emit('loading', value), { flush: 'sync' })

async function load(more = false) {
  const project = props.projectId
  if (!project) return
  loading.value = true
  try {
    const data = await request(`/api/projects/${project}/runs?offset=${more ? nextOffset.value : 0}`)
    if (project !== props.projectId) return
    runs.value = more ? [...runs.value, ...data.runs] : data.runs
    nextOffset.value = data.next_offset
  } catch (error) { emit('error', error.message) }
  finally { loading.value = false }
}
async function open(event) {
  const id = event.target.value
  if (!id) { emit('open', null); return }
  const project = props.projectId
  loading.value = true
  try {
    const data = await request(`/api/projects/${project}/runs/${id}`)
    if (project === props.projectId) emit('open', data)
  } catch (error) { emit('error', error.message) }
  finally { loading.value = false }
}
watch(() => props.projectId, () => { runs.value = []; nextOffset.value = null; load() }, { immediate: true })
defineExpose({ refresh: () => load() })
</script>

<template>
  <div class="mb-3">
    <label class="field-label" for="research-history">研究历史 · 选择已有版本查看或继续完善</label>
    <div class="d-flex gap-2">
      <select id="research-history" class="form-select" :value="selected || ''" :disabled="busy || loading" @change="open">
        <option value="">新建研究</option>
        <option v-for="run in runs" :key="run.run_id" :value="run.run_id">
          {{ new Date(run.recorded_at).toLocaleString() }} · {{ run.task }}{{ run.can_continue ? '' : '（无可恢复上下文）' }}
        </option>
      </select>
      <button v-if="nextOffset !== null" type="button" class="btn subtle-btn text-nowrap" :disabled="busy || loading" @click="load(true)">更早记录</button>
    </div>
  </div>
</template>
