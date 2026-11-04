<script setup>
defineProps({ audit: { type: Object, default: null } })
const labels = { supported: '模型初审：支持', partial: '模型初审：部分支持', unsupported: '模型初审：不支持', unknown: '无法判断／未复核' }
</script>

<template>
  <details v-if="audit" class="mt-2">
    <summary>查看结论与证据（{{ audit.claims?.length || 0 }}）</summary>
    <small>只显示已登记的结论，不能保证覆盖所有重要结论。模型初审可能出错，需人工核查。</small>
    <div v-if="audit.registration"><small>交付前补登记：{{ audit.registration.status }} · token：{{ audit.registration.usage?.totals?.total_tokens ?? '未提供' }}</small></div>
    <div v-if="audit.review"><small>初审状态：{{ audit.review.status }} · 初审 token：{{ audit.review.usage?.totals?.total_tokens ?? '未提供' }}</small></div>
    <p v-if="!audit.claims?.length" class="mt-2 mb-1">本次没有登记结论；不代表报告没有事实主张。</p>
    <div v-for="claim in audit.claims || []" :key="claim.claim_id" class="border-top mt-2 pt-2">
      <strong>{{ claim.statement }}</strong>
      <div><small>{{ claim.in_answer ? '结论在报告中逐字出现' : '报告未逐字包含此结论' }} · {{ claim.location_valid ? '引用位置已关联' : '引用位置待核对' }} · {{ labels[claim.semantic_support] || labels.unknown }}</small></div>
      <div v-if="claim.semantic_reason"><small>{{ claim.semantic_reason }}</small></div>
      <div v-for="source in claim.sources" :key="source.ref" class="mt-2">
        <small>{{ source.name || `文档 ${source.document_id}` }} · L{{ source.start_line }}–L{{ source.end_line }}<template v-if="source.pages?.length"> · 第 {{ source.pages.join('、') }} 页</template> · SHA-256 {{ source.sha256 }}</small>
        <div v-if="source.content_kind || source.warnings?.length"><small>来源类型：{{ source.content_kind || '未标注' }}<template v-if="source.warnings?.length"> · 提取提示：{{ source.warnings.join('；') }}</template></small></div>
        <div class="border rounded p-2 mt-1" style="white-space: pre-wrap; overflow-wrap: anywhere">{{ source.quote }}</div>
      </div>
      <div v-if="claim.occurrences?.some(item => item.binding === 'quote_block_candidate')"><small>相邻原文引用块可能与结论有关，仍需核对。</small></div>
      <details v-if="claim.cited_observations?.length" class="mt-2">
        <summary><small>查看报告引用的已读原文及上下文</small></summary>
        <div v-for="source in claim.cited_observations" :key="`${source.ref}-${source.start_line}-${source.end_line}`" class="mt-2">
          <small>文档 {{ source.document_id }} · 引用 L{{ source.start_line }}–L{{ source.end_line }}<template v-if="source.pages?.length"> · 第 {{ source.pages.join('、') }} 页</template><template v-if="source.context_start_line"> · 上下文 L{{ source.context_start_line }}–L{{ source.context_end_line }}</template><template v-if="source.context_truncated || source.context_window_limited"> · 上下文受限</template><template v-if="source.context_conflict"> · 相邻观察有冲突，须核对</template></small>
          <div class="border rounded p-2 mt-1" style="white-space: pre-wrap; overflow-wrap: anywhere">{{ source.observed_context }}</div>
        </div>
      </details>
    </div>
    <small v-if="audit.review?.errors?.length">部分模型初审未完成；未完成的结论保持无法判断。</small>
  </details>
</template>
