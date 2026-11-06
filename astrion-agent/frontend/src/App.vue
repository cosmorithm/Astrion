<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { request, streamResearch } from './api.js'
import Artifacts from './components/Artifacts.vue'
import ExternalSources from './components/ExternalSources.vue'
import SourceLibrary from './components/SourceLibrary.vue'
import ResearchHistory from './components/ResearchHistory.vue'
import ResearchDag from './components/ResearchDag.vue'
import ResearchCompare from './components/ResearchCompare.vue'
import ResearchTrace from './components/ResearchTrace.vue'
import CitationEvidence from './components/CitationEvidence.vue'
import ClaimAudit from './components/ClaimAudit.vue'
import { renderMarkdown } from './markdown.js'

const projects = ref([])
const projectId = ref('')
const projectName = ref('')
const documents = ref([])
const memories = ref([])
const memoryText = ref('')
const memoryScope = ref('project')
const task = ref('')
const activeTab = ref('research')
const error = ref('')
const notice = ref('')
const busy = ref(false)
const pendingAnswer = ref('')
const progressText = ref('')
const progress = ref([])
const traceEvents = ref([])
const result = ref(null)
const historyPanel = ref(null)
const dagPanel = ref(null)
const comparePanel = ref(null)
const historyLoading = ref(false)
const dagOpening = ref(false)
let dagRequestNumber = 0
const continueRunId = ref('')
const budgets = ref(null)
const delegationMode = ref('baseline')
const runError = ref('')
const fileInput = ref(null)
const currentProject = computed(() => projects.value.find(item => item.id === Number(projectId.value)))
const statusLabels = { completed: '已完成', needs_input: '需要补充信息', no_tool_call: '已返回回答', step_limit: '达到步数上限，未完成', incomplete_plan: '仍有未完成子任务', error: '研究失败，未完成' }
const routeSkipLabels = { insufficient_steps_or_workers: '子 Agent 名额或轮数不足',
  delegation_tools_unavailable: '委派工具未开放', worker_report_reader_unavailable_or_limit_mismatch: '子报告读取工具不可用或名额不匹配',
  independent_client_unavailable: '没有独立模型客户端', worker_client_unavailable: '无法创建子 Agent 客户端', parallel_delegation_tool_unavailable: '并行委派工具未开放',
  continuation_constraints_unavailable: '续写保留全部历史约束，交由主 Agent 决定',
  root_input_token_budget: '主 Agent 上下文预算不足',
  invalid_original_request: '原始任务不可用' }
function routingSummary(routing) {
  const proposal = routing?.proposal || {}
  const dispatch = routing?.dispatch || {}
  if (proposal.status === 'fallback') return `路由回退，主 Agent 继续${proposal.error_code ? `（${proposal.error_code}）` : ''}`
  if (proposal.status === 'skipped') return `未启用路由派发（${routeSkipLabels[proposal.reason] || proposal.reason || '条件不足'}）`
  if (proposal.mode === 'delegate' && dispatch.status === 'returned') return `已派发 ${dispatch.worker_count || 0} 个子任务，结果待核查`
  if (proposal.mode === 'delegate') return `建议委派但未派发（${routeSkipLabels[dispatch.reason] || dispatch.reason || '条件不足'}）`
  return '由主 Agent 直接处理'
}
const reportState = computed(() => busy.value ? '研究中' : runError.value ? '研究失败' : result.value ? (statusLabels[result.value.result.status] || '未完成') : '等待研究')
const reportText = computed(() => {
  if (result.value) {
    if (result.value.result.status === 'error') return ''
    if (result.value.result.answer?.trim()) return result.value.result.answer
    return result.value.result.status === 'step_limit'
      ? '已达到本次最大步数，未生成最终答案。请缩小问题范围后重试。'
      : '本次运行没有生成回答，请查看研究轨迹。'
  }
  return pendingAnswer.value || progressText.value
})
const reportHtml = computed(() => renderMarkdown(reportText.value))
const usageText = computed(() => {
  const usage = result.value?.usage_including_root_review ?? result.value?.usage
  if (!usage || usage.calls === null || usage.calls === usage.unknown_calls) return '未知'
  const total = usage.totals?.total_tokens ?? 0
  return usage.unknown_calls ? `已知 ${total}（另有 ${usage.unknown_calls} 次未知）` : String(total)
})

async function loadProjects() {
  projects.value = await request('/api/projects')
  if (projectId.value && !projects.value.some(item => item.id === Number(projectId.value))) projectId.value = ''
  if (!projectId.value && projects.value.length) projectId.value = String(projects.value[0].id)
}
async function loadProjectData() {
  if (!projectId.value) { documents.value = []; memories.value = []; return }
  const id = encodeURIComponent(projectId.value)
  const [docs, mems] = await Promise.all([
    request(`/api/projects/${id}/documents`), request(`/api/memories?project_id=${id}`),
  ])
  if (encodeURIComponent(projectId.value) !== id) return
  documents.value = docs
  memories.value = mems
}
async function action(fn) {
  error.value = ''; notice.value = ''
  try { await fn() } catch (e) { error.value = e.message }
}
async function createProject() {
  if (!projectName.value.trim()) return
  await action(async () => {
    const created = await request('/api/projects', { method: 'POST', body: JSON.stringify({ name: projectName.value }) })
    await loadProjects()
    projectId.value = String(created.id)
    projectName.value = ''
    notice.value = '项目已创建。'
  })
}
async function addMemory() {
  if (!memoryText.value.trim()) return
  await action(async () => {
    await request('/api/memories', { method: 'POST', body: JSON.stringify({
      scope: memoryScope.value, content: memoryText.value,
      project_id: memoryScope.value === 'project' ? Number(projectId.value) : null,
    }) })
    memoryText.value = ''
    await loadProjectData()
    notice.value = '记忆已保存。'
  })
}
async function forgetMemory(id) {
  await action(async () => {
    await request(`/api/memories/${id}/forget`, { method: 'POST' })
    await loadProjectData()
    notice.value = '记忆已标记为失效，原文仍保存在数据库中。'
  })
}
function describeEvent(item) {
  const e = item.event
  const p = e.payload || {}
  const who = item.role === 'root' ? '主 Agent' : item.role === 'router' ? '委派路由器' : `子 Agent · ${item.worker_id}`
  if (item.role === 'router' && e.type === 'decision.finished') {
    return `委派路由器 · ${routingSummary(p)}`
  }
  if (e.type === 'model.call.started') return item.role === 'router' ? `${who} · 正在判断是否委派` : `${who} · 第 ${e.step} 步 · ${p.input?.finalizing ? '正在整理最终回答' : p.purpose === 'compaction' ? '正在整理上下文' : '正在思考下一步'}`
  if (e.type === 'tool.call.started') {
    const names = { search_knowledge: '检索资料', read_knowledge: '阅读原文', search_memory: '查询记忆', run_worker: '委派子任务', finish: '提交结果', ask_user: '请求补充信息' }
    return `${who} · ${names[p.name] || p.name}`
  }
  if (e.type === 'tool.call.finished' && p.outcome === 'success' && ['search_knowledge', 'read_knowledge'].includes(p.name)) {
    try {
      const data = JSON.parse(p.content)
      return `${who} · 读到 ${data.results.length} 个片段${data.new_ranges === undefined ? '' : `，新增 ${data.new_ranges} 个`}`
    } catch { return '' }
  }
  if (e.type === 'tool.call.finished' && p.outcome !== 'success') return `${who} · ${p.name} ${p.outcome}`
  if (e.type === 'model.call.failed') return `${who} · 模型请求失败`
  if (e.type === 'run.finished') return `${who} · ${statusLabels[p.status] || p.status}`
  return ''
}
async function startResearch() {
  if (!projectId.value || !task.value.trim() || busy.value || historyLoading.value || dagOpening.value || !budgets.value) return
  error.value = ''; runError.value = ''; notice.value = ''; result.value = null
  progress.value = []; traceEvents.value = []; pendingAnswer.value = ''; progressText.value = ''; busy.value = true
  try {
    await streamResearch({ project_id: Number(projectId.value), task: task.value,
      max_steps: budgets.value.max_steps, worker_steps: budgets.value.worker_steps, max_workers: budgets.value.max_workers,
      delegation_mode: delegationMode.value,
      continue_run_id: continueRunId.value || null, context_budget_chars: budgets.value.context.budget_chars }, message => {
      if (message.type === 'model_delta' && message.role === 'root') {
        if (message.kind === 'answer') pendingAnswer.value += message.text
        else progressText.value += message.text
      } else if (message.type === 'model_retry') {
        const who = message.role === 'root' ? '主 Agent' : message.role === 'router' ? '委派路由器' : '子 Agent'
        progress.value.push(`${who} · 模型请求失败，${message.delay_seconds}s 后重试（第 ${message.attempt}/${message.attempts} 次）`)
      } else if (message.type === 'event') {
        traceEvents.value.push({ role: message.role, worker_id: message.worker_id, event: message.event })
        if (message.role === 'root' && message.event.type === 'model.call.started' && message.event.payload?.purpose === 'decision') {
          pendingAnswer.value = ''; progressText.value = ''
        }
        const text = describeEvent(message)
        if (text) progress.value.push(text)
      } else if (message.type === 'done') {
        result.value = message.result
        continueRunId.value = message.result.context_archive ? message.result.run_id : ''
        task.value = ''
        pendingAnswer.value = ''
        progressText.value = ''
        if (message.result.result.status === 'error') runError.value = message.result.result.error?.message || '模型未生成有效报告，本次研究未完成。'
      } else if (message.type === 'error') {
        throw new Error(message.message)
      }
    })
  } catch (e) { error.value = e.message; runError.value = e.message } finally {
    busy.value = false
    await Promise.all([historyPanel.value?.refresh(), dagPanel.value?.refresh(), comparePanel.value?.refresh()])
  }
}
function openRun(run) {
  dagRequestNumber++
  dagOpening.value = false
  notice.value = ''
  result.value = run
  continueRunId.value = run?.context_archive ? run.run_id : ''
  runError.value = run?.error_type
    ? `该运行失败：${run.error_message || run.error_type}${run.context_archive ? ' 可从最近一次完整检查点继续。' : ' 缺少可恢复上下文。'}` : ''
  // Legacy exception records may contain status at the top level.
  if (run && !run.result) result.value = { ...run, result: { status: run.status || 'error' } }
  progress.value = []; traceEvents.value = []; pendingAnswer.value = ''; progressText.value = ''
}
async function onRunDeleted({ projectId: deletedProject, runId }) {
  if (projectId.value !== deletedProject) return
  if (result.value?.run_id === runId || continueRunId.value === runId) openRun(null)
  comparePanel.value?.removeRun(runId)
  notice.value = '失败研究记录及运行日志已清理；项目资料、上下文归档和产物仍保留。'
  await Promise.all([dagPanel.value?.refresh(), comparePanel.value?.refresh()])
}
async function openDagRun(id, continuationStatus) {
  if (busy.value || historyLoading.value || dagOpening.value) return
  const project = projectId.value
  const generation = ++dagRequestNumber
  dagOpening.value = true
  try {
    const run = await request(`/api/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(id)}`)
    if (generation !== dagRequestNumber || project !== projectId.value) return
    openRun(run)
    if (continuationStatus === 'unavailable') {
      continueRunId.value = ''
      notice.value = '该版本没有可用的恢复记录；仍可查看其报告。'
    }
  } catch (e) {
    if (generation === dagRequestNumber) error.value = e.message
  } finally {
    if (generation === dagRequestNumber) dagOpening.value = false
  }
}
watch(projectId, () => {
  dagRequestNumber++
  dagOpening.value = false
  continueRunId.value = ''
  result.value = null; runError.value = ''; progress.value = []; traceEvents.value = []; pendingAnswer.value = ''; progressText.value = ''
  action(loadProjectData)
})
onMounted(() => action(async () => {
  await Promise.all([loadProjects(), request('/api/research/config').then(value => { budgets.value = value })])
}))
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">A</div><div><strong>Astrion Research</strong><span>科研工作台</span></div></div>
      <div class="sidebar-label">工作空间</div>
      <label class="field-label" for="project">当前项目</label>
      <select id="project" v-model="projectId" class="form-select project-select" :disabled="busy || !projects.length">
        <option v-if="!projects.length" value="">先创建一个项目</option>
        <option v-for="project in projects" :key="project.id" :value="String(project.id)">{{ project.name }}</option>
      </select>
      <form class="project-form" @submit.prevent="createProject">
        <input v-model="projectName" class="form-control" placeholder="新项目名称" aria-label="新项目名称" :disabled="busy">
        <button class="btn create-btn" :disabled="busy || !projectName.trim()" aria-label="创建项目">+</button>
      </form>
      <div class="sidebar-label nav-label">功能</div>
      <nav class="workspace-nav" aria-label="工作台导航">
        <button :class="{ selected: activeTab === 'research' }" @click="activeTab = 'research'"><span class="nav-icon">◻</span> 研究工作台</button>
        <button :class="{ selected: activeTab === 'sources' }" @click="activeTab = 'sources'"><span class="nav-icon">▤</span> 资料库 <span class="nav-count">{{ documents.length }}</span></button>
        <button :class="{ selected: activeTab === 'artifacts' }" @click="activeTab = 'artifacts'">数据与产物</button>
        <button :class="{ selected: activeTab === 'external' }" @click="activeTab = 'external'">外部证据</button>
        <button :class="{ selected: activeTab === 'memory' }" @click="activeTab = 'memory'"><span class="nav-icon">◌</span> 长期记忆 <span class="nav-count">{{ memories.length }}</span></button>
      </nav>
      <div class="sidebar-footer"><span class="status-dot"></span><div><strong>本地工作空间</strong></div></div>
    </aside>

    <main class="main-content">
      <header class="topbar"><div><span class="crumb">工作空间</span><span class="crumb-sep">/</span><span>{{ currentProject?.name || '未选择项目' }}</span></div><span class="top-badge"><span class="status-dot"></span> 本地模式</span></header>
      <div class="content-wrap">
        <div v-if="error" class="alert alert-danger alert-dismissible" role="alert">{{ error }}<button type="button" class="btn-close" aria-label="关闭" @click="error = ''"></button></div>
        <div v-if="notice" class="alert alert-success alert-dismissible" role="status">{{ notice }}<button type="button" class="btn-close" aria-label="关闭" @click="notice = ''"></button></div>

        <section v-if="activeTab === 'research'">
          <h1>研究工作台</h1>
          <p class="hero-copy">选择项目、导入资料并提出研究问题。</p>
          <div v-if="!projectId" class="empty-project"><div><strong>请先创建项目</strong><p>在左侧输入项目名称。</p></div></div>
          <template v-else>
            <ResearchHistory ref="historyPanel" :project-id="projectId" :busy="busy || dagOpening" :selected="result?.run_id || continueRunId" :selected-deletable="result?.can_delete === true" @open="openRun" @deleted="onRunDeleted" @error="error = $event" @loading="historyLoading = $event" />
            <ResearchDag ref="dagPanel" :project-id="projectId" :selected="result?.run_id || continueRunId" :busy="busy || historyLoading || dagOpening" @open="openDagRun" @error="error = $event" />
            <ResearchCompare ref="comparePanel" :project-id="projectId" :busy="busy" />
            <form class="question-card" @submit.prevent="startResearch">
              <div class="card-top"><div><strong>{{ continueRunId ? '补充研究要求' : '研究问题' }}</strong></div><span class="hint-pill">{{ continueRunId ? '继承上下文 · 另存新版本' : '基于项目资料研究' }}</span></div>
              <textarea v-model="task" class="form-control question-input" rows="4" placeholder="你想探索什么？例如：多 Agent 在什么情况下能改善研究任务？" :disabled="busy"></textarea>
              <details v-if="budgets" class="my-2">
                <summary>运行预算：主 Agent {{ budgets.max_steps }} 轮，子 Agent {{ budgets.worker_steps }} 轮，最多 {{ budgets.max_workers }} 个</summary>
                <div class="row g-2 mt-1">
                  <label class="col-sm-4">主 Agent 轮数<input v-model.number="budgets.max_steps" class="form-control" type="number" min="1" :max="budgets.limits.steps" required :disabled="busy"></label>
                  <label class="col-sm-4">每个子 Agent 轮数<input v-model.number="budgets.worker_steps" class="form-control" type="number" min="1" :max="budgets.limits.steps" required :disabled="busy"></label>
                  <label class="col-sm-4">子 Agent 总数<input v-model.number="budgets.max_workers" class="form-control" type="number" min="0" :max="budgets.limits.workers" required :disabled="busy"></label>
                </div>
                <label class="d-block mt-2">上下文压缩阈值（字符，非 token）<input v-model.number="budgets.context.budget_chars" class="form-control" type="number" min="1" :max="budgets.context.max_budget_chars" required :disabled="busy"></label>
                <small>模型窗口：{{ budgets.context.window_tokens ? budgets.context.window_tokens.toLocaleString() + ' tokens' : '未知，请核对模型服务' }}。字符阈值是软触发条件，不是 token 上限；调高可能增加费用。独立子任务可并发。</small>
                <label class="d-block mt-2">委派方式
                  <select v-model="delegationMode" class="form-select" :disabled="busy">
                    <option value="baseline">主 Agent 自主决定（默认）</option>
                    <option value="structured">先由独立路由器判断</option>
                  </select>
                </label>
                <small v-if="delegationMode === 'structured'">路由器会额外调用模型；预派发子任务只能读取本地资料，系统仍保存日志和子报告。续写时由主 Agent 决定是否委派；可能增加耗时和费用。</small>
              </details>
              <div class="question-bottom"><span>{{ continueRunId ? '保留旧报告、已读证据与待办；本次计量独立记录。' : '基于当前项目资料进行研究' }}</span><button class="btn primary-btn" :disabled="busy || historyLoading || dagOpening || !task.trim() || !budgets">{{ busy ? '研究中…' : continueRunId ? '继续研究' : '开始研究' }}</button></div>
            </form>
            <div class="research-grid">
              <section class="panel report-panel">
                <div class="panel-header"><div><strong>研究报告</strong></div><span class="panel-meta">{{ reportState }}</span></div>
                <template v-if="result || reportText || runError">
                  <div v-if="busy" class="live-label"><span class="pulse"></span> 正在生成 · 内容可能调整</div>
                  <div v-if="runError" class="alert alert-warning">{{ runError }}{{ reportText ? ' 以下内容尚未完成。' : '' }}</div>
                  <div class="report-text" v-html="reportHtml"></div>
                  <div v-if="result" class="result-meta">
                    <span>状态：{{ result.result.status }}</span><span>耗时：{{ result.duration_seconds?.toFixed(1) }} s</span>
                    <span>子 Agent：{{ result.workers?.length || 0 }}</span><span>{{ result.usage_including_root_review ? 'Token（含审查）' : 'Token（研究阶段）' }}：{{ usageText }}</span>
                    <small v-if="result.delegation_mode === 'structured' && result.delegation_routing?.proposal">委派路由：{{ routingSummary(result.delegation_routing) }} · {{ result.delegation_routing.proposal.reason }}</small>
                    <span>引用检查：{{ result.citation_audit?.status || '未检查' }}</span>
                    <small v-if="result.parent_run_id">续写自 {{ result.parent_run_id }}；旧版本可在研究历史中查看。</small>
                    <small>引用位置检查不判断原文是否支持结论。</small>
                    <CitationEvidence :audit="result.citation_audit" :project-id="projectId" :run-id="result.run_id" />
                    <ClaimAudit :audit="result.claim_audit" />
                    <small v-if="result.report_error">报告文件保存失败；回答和事件记录仍已保留。</small>
                    <a v-if="result.report_artifact" :href="`/api/projects/${projectId}/artifacts/${result.report_artifact.id}/download`">下载研究报告</a>
                  </div>
                </template>
                <div v-else class="empty-report"><p>输入问题后，研究结果会显示在这里。</p></div>
              </section>
              <section class="panel activity-panel">
                <div class="panel-header"><div><strong>研究轨迹</strong></div><span class="panel-meta">LIVE</span></div>
                <div v-if="progress.length" class="timeline"><div v-for="(item, index) in progress" :key="index" class="timeline-item"><span class="timeline-dot"></span><span>{{ item }}</span></div></div>
                <div v-else class="timeline-empty">Agent 的研究步骤会显示在这里。</div>
                <details v-if="progressText" class="model-output"><summary>模型过程输出</summary><div>{{ progressText }}</div></details>
                <details class="mt-3"><summary>查看每轮输入输出</summary><ResearchTrace :project-id="projectId" :run-id="result?.run_id || ''" :busy="busy" :live="traceEvents" /></details>
                <div class="activity-note">运行事件与最终结论分开展示。</div>
              </section>
            </div>
          </template>
        </section>

        <section v-else-if="activeTab === 'sources'">
          <SourceLibrary :key="projectId" :project-id="projectId" :busy="busy" @changed="loadProjectData" />
        </section>

        <section v-else-if="activeTab === 'artifacts'"><Artifacts :key="projectId" :project-id="projectId" :busy="busy" /></section>
        <section v-else-if="activeTab === 'external'"><ExternalSources :key="projectId" :project-id="projectId" :busy="busy" @changed="loadProjectData" /></section>
        <section v-else>
          <h2>长期记忆</h2><p class="section-copy">查看和管理对话记忆、项目记忆。</p>
          <div class="panel collection-panel">
            <div class="panel-header"><div><strong>添加记忆</strong></div><span class="panel-meta">由你明确保存</span></div>
            <form @submit.prevent="addMemory"><div class="memory-form"><select v-model="memoryScope" class="form-select" :disabled="busy || !projectId"><option value="project">当前项目记忆</option><option value="conversation">跨项目对话记忆</option></select><input v-model="memoryText" class="form-control" placeholder="希望 Astrion 记住什么？" :disabled="busy || !projectId"><button class="btn primary-btn" :disabled="busy || !projectId || !memoryText.trim()">保存</button></div></form>
            <div class="list-title">当前可用记忆 <span>{{ memories.length }}</span></div>
            <div v-if="!memories.length" class="list-empty">还没有保存记忆。</div>
            <div v-for="memory in memories" :key="memory.id" class="list-row memory-row"><span class="memory-icon">◌</span><div><strong>{{ memory.content }}</strong><small>{{ memory.scope === 'conversation' ? '对话记忆 · 跨项目' : '项目记忆 · 仅当前项目' }}</small></div><button class="btn subtle-btn" :disabled="busy" @click="forgetMemory(memory.id)">标记失效</button></div>
          </div>
        </section>
        <footer>Astrion Research</footer>
      </div>
    </main>
  </div>
</template>
