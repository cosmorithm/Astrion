<script setup>
import { ref, watch } from 'vue'
import { request } from '../api.js'
const props = defineProps({ audit: { type: Object, default: null }, projectId: { type: [String, Number], default: null }, runId: { type: String, default: '' } })
const replay = ref(null)
const replayError = ref('')
const replayLoading = ref(false)
let requestVersion = 0
watch(() => [props.projectId, props.runId], () => {
  requestVersion += 1
  replay.value = null
  replayError.value = ''
  replayLoading.value = false
})
async function loadVisibility() {
  if (!props.projectId || !props.runId) return
  const version = ++requestVersion
  replayLoading.value = true
  replayError.value = ''
  try {
    const data = await request(`/api/projects/${encodeURIComponent(props.projectId)}/runs/${encodeURIComponent(props.runId)}/citation-visibility`)
    if (version !== requestVersion) return
    const original = props.audit?.checks || []
    if (data.checks?.length !== original.length || data.checks.some((c, i) => c.citation !== original[i].citation)) {
      throw new Error('重新检查的引用与当前报告不匹配')
    }
    replay.value = data
  } catch (e) {
    if (version === requestVersion) replayError.value = e.message
  } finally {
    if (version === requestVersion) replayLoading.value = false
  }
}
function visibility(check, index) { return replay.value?.checks?.[index]?.model_visibility || check.model_visibility }

</script>

<template>
  <details v-if="audit" class="mt-2">
    <summary>查看引用与工具返回原文（{{ audit.checks?.length || 0 }}）</summary>
    <small>位置检查只核对工具返回的原文范围，不保证正文完整进入模型上下文，也不判断结论是否得到支持；未覆盖的引用可能是警示或反例，请结合报告逐条核查。</small>
    <div v-if="projectId && runId" class="mt-1">
      <button type="button" class="btn btn-sm btn-outline-secondary" :disabled="replayLoading" @click="loadVisibility">{{ replayLoading ? '检查中…' : '按需检查最终请求中的正文' }}</button>
      <small v-if="replay?.reason" class="ms-2">日志不可用或超出读取限制（{{ replay.reason }}），可见性未知。</small>
      <small v-if="replayError" class="ms-2">{{ replayError }}</small>
    </div>
    <p v-if="!audit.checks?.length" class="mt-2 mb-1">报告没有可识别的行号引用。</p>
    <div v-for="(check, index) in audit.checks || []" :key="index" class="border-top mt-2 pt-2">
      <strong>{{ check.citation }}</strong>
      <span class="ms-2">{{ check.in_retrieved_range ? '工具返回范围覆盖' : '工具返回范围未覆盖' }}</span>
      <div v-if="visibility(check, index)"><small>{{ visibility(check, index).status === 'visible' ? '最终报告请求中包含对应工具正文' : '最终报告请求中正文可见性未知' }}；不代表结论已获证据支持。</small></div>
      <div v-if="check.report_context" class="mt-1"><small>报告中的上下文：</small><div class="border rounded p-2" style="white-space: pre-wrap; overflow-wrap: anywhere">{{ check.report_context }}</div></div>
      <div v-if="check.inherited_range"><small>包含历史运行中记录的已读范围。</small></div>
      <div v-for="(item, n) in check.evidence || []" :key="n" class="mt-2">
        <small>{{ item.name || `文档 ${item.document_id}` }} · L{{ item.start_line }}–L{{ item.end_line }}<template v-if="item.pages?.length"> · 第 {{ item.pages.join('、') }} 页</template> · {{ item.role === 'worker' ? `子 Agent ${item.worker_id || ''}` : '主 Agent' }} · {{ item.tool }}</small>
        <div class="border rounded p-2 mt-1" style="white-space: pre-wrap; overflow-wrap: anywhere">{{ item.excerpt }}</div>
        <small v-if="item.display_truncated">展示片段过长，已截短；请到资料库阅读完整原文。</small>
      </div>
      <small v-if="!check.evidence?.length">{{ check.inherited_range ? '该范围来自历史运行，本轮未保存可直接展示的原文。' : '本轮没有可直接展示的返回原文；旧记录也可能缺少此字段。' }}</small>
      <small v-else-if="!check.in_retrieved_range">上方只展示工具返回的部分，尚未覆盖整条引用。</small>
    </div>
  </details>
</template>
