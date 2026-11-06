<script setup>
import { computed, ref, watch } from 'vue'
import { request } from '../api.js'
import { renderMarkdown } from '../markdown.js'
import { reportData, reportRelation } from './researchCompare.js'

const props = defineProps({ projectId: String, busy: Boolean })
const runs = ref([])
const next = ref(null)
const listLoading = ref(false)
const listError = ref('')
const selected = ref(['', ''])
const reports = ref([null, null])
const detailLoading = ref([false, false])
const detailError = ref(['', ''])
let projectGeneration = 0
let listGeneration = 0
const detailGeneration = [0, 0]
const relation = computed(() => reportRelation(reports.value[0], reports.value[1]))
const descriptions = computed(() => reports.value.map(run => run ? reportData(run) : null))

async function load(more = false) {
  const project = props.projectId
  if (!project || listLoading.value || (more && next.value === null)) return
  const projectId = projectGeneration
  const requestId = ++listGeneration
  listLoading.value = true
  listError.value = ''
  try {
    const offset = more ? next.value : 0
    const data = await request(`/api/projects/${encodeURIComponent(project)}/runs?offset=${offset}&limit=50`)
    if (projectId !== projectGeneration || requestId !== listGeneration || project !== props.projectId) return
    runs.value = more ? [...runs.value, ...data.runs.filter(run => !runs.value.some(old => old.run_id === run.run_id))] : data.runs
    next.value = data.next_offset
  } catch (error) {
    if (projectId === projectGeneration && requestId === listGeneration) listError.value = error.message
  } finally {
    if (projectId === projectGeneration && requestId === listGeneration) listLoading.value = false
  }
}
async function choose(index, id) {
  selected.value[index] = id
  reports.value[index] = null
  detailError.value[index] = ''
  const generation = ++detailGeneration[index]
  const projectId = projectGeneration
  const project = props.projectId
  if (!id || !project) { detailLoading.value[index] = false; return }
  detailLoading.value[index] = true
  try {
    const run = await request(`/api/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(id)}`)
    if (projectId === projectGeneration && generation === detailGeneration[index] && project === props.projectId) reports.value[index] = run
  } catch (error) {
    if (projectId === projectGeneration && generation === detailGeneration[index]) detailError.value[index] = error.message
  } finally {
    if (projectId === projectGeneration && generation === detailGeneration[index]) detailLoading.value[index] = false
  }
}
watch(() => props.projectId, () => {
  projectGeneration++
  listGeneration++
  detailGeneration[0]++; detailGeneration[1]++
  runs.value = []; next.value = null; listLoading.value = false; listError.value = ''
  selected.value = ['', '']; reports.value = [null, null]
  detailLoading.value = [false, false]; detailError.value = ['', '']
  load()
}, { immediate: true })
function removeRun(id) {
  // Discard an in-flight listing that may still contain the deleted version.
  listGeneration++
  listLoading.value = false
  listError.value = ''
  for (const index of [0, 1]) {
    if (selected.value[index] === id) choose(index, '')
  }
  runs.value = runs.value.filter(run => run.run_id !== id)
}
defineExpose({ refresh: () => load(), removeRun })
</script>

<template>
  <details class="compare mb-3">
    <summary>对比研究版本</summary>
    <p class="small text-muted mt-2 mb-2">并排查看两个版本的原报告和运行信息。文本变化不代表结论正确或研究效果提升。</p>
    <div v-if="listError" class="text-danger small">{{ listError }}</div>
    <div class="row g-3">
      <section v-for="index in [0, 1]" :key="index" class="col-lg-6">
        <label class="form-label small" :for="`compare-version-${index}`">{{ index === 0 ? '左侧版本' : '右侧版本' }}</label>
        <select :id="`compare-version-${index}`" class="form-select form-select-sm" :value="selected[index]" :disabled="busy || listLoading" @change="choose(index, $event.target.value)">
          <option value="">选择研究版本</option>
          <option v-for="run in runs" :key="run.run_id" :value="run.run_id">{{ reportData(run).date }} · {{ run.task || '未记录研究问题' }} · {{ run.run_id.slice(-8) }}</option>
        </select>
        <p v-if="detailLoading[index]" class="small mt-2">正在加载…</p>
        <p v-else-if="detailError[index]" class="small text-danger mt-2">{{ detailError[index] }}</p>
        <template v-else-if="descriptions[index]">
          <dl class="small compare-meta mt-2">
            <dt>任务</dt><dd>{{ descriptions[index].task }}</dd>
            <dt>状态</dt><dd>{{ descriptions[index].status }}</dd>
            <dt>时间</dt><dd>{{ descriptions[index].date }}</dd>
            <dt>耗时</dt><dd>{{ descriptions[index].duration }}</dd>
            <dt>Token</dt><dd>{{ descriptions[index].tokens }}</dd>
            <dt>父版本</dt><dd>{{ descriptions[index].parent }}</dd>
          </dl>
          <div v-if="descriptions[index].answer" class="compare-report border rounded p-2" v-html="renderMarkdown(descriptions[index].answer)"></div>
          <p v-else class="small text-muted">此版本没有保存可显示的报告。</p>
        </template>
      </section>
    </div>
    <p v-if="relation" class="small mt-2 mb-0">{{ relation }}</p>
    <p v-if="!runs.length && !listLoading && !listError" class="small text-muted mt-2 mb-0">当前项目没有研究版本。</p>
    <button v-if="next !== null" type="button" class="btn subtle-btn btn-sm mt-2" :disabled="busy || listLoading" @click="load(true)">加载更早版本</button>
    <span v-if="listLoading" class="small text-muted ms-2">正在加载版本…</span>
  </details>
</template>
<style scoped>
.compare { border: 1px solid #e0e4ea; border-radius: .45rem; padding: .65rem .8rem; background: #fff; }
summary { cursor: pointer; font-size: .9rem; }
.compare-meta { display: grid; grid-template-columns: 3.5em minmax(0, 1fr); gap: .15rem .4rem; }
.compare-meta dt, .compare-meta dd { margin: 0; overflow-wrap: anywhere; }
.compare-report { max-height: 32rem; overflow: auto; overflow-wrap: anywhere; font-size: .85rem; }
</style>
