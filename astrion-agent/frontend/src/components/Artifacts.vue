<script setup>
import { ref, watch } from "vue";
import { request } from "../api.js";
const props = defineProps({ projectId: String, busy: Boolean });
const entries = ref([]),
  next = ref(null),
  error = ref(""),
  loading = ref(false);
const preview = ref(null),
  status = ref(null),
  code = ref(""),
  selected = ref([]),
  execution = ref(null);
const imageId = ref(null);
let generation = 0;
async function act(fn) {
  if (loading.value) return;
  error.value = "";
  loading.value = true;
  try {
    await fn();
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function load(offset = 0) {
  if (!props.projectId) return;
  const id = props.projectId,
    current = generation;
  const data = await request(`/api/projects/${id}/artifacts?offset=${offset}`);
  if (id !== props.projectId || current !== generation) return;
  entries.value = offset
    ? [...entries.value, ...data.artifacts]
    : data.artifacts;
  next.value = data.next_offset;
}
watch(
  () => props.projectId,
  () => {
    generation++;
    entries.value = [];
    selected.value = [];
    preview.value = null;
    execution.value = null;
    status.value = null;
    imageId.value = null;
    act(async () => {
      await load();
      if (props.projectId) {
        const id = props.projectId;
        const value = await request(`/api/projects/${id}/execution-status`);
        if (id === props.projectId) status.value = value;
      }
    });
  },
  { immediate: true },
);
async function upload(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  await act(async () => {
    await request(`/api/projects/${props.projectId}/artifacts`, {
      method: "POST",
      body: form,
    });
    await load();
  });
  event.target.value = "";
}
async function read(item, offset = 0) {
  await act(async () => {
    preview.value = await request(
      `/api/projects/${props.projectId}/artifacts/${item.id}/read?offset=${offset}`,
    );
  });
}
async function execute() {
  await act(async () => {
    execution.value = await request(
      `/api/projects/${props.projectId}/execute`,
      {
        method: "POST",
        body: JSON.stringify({
          code: code.value,
          input_artifact_ids: selected.value,
          timeout: 20,
        }),
      },
    );
    await load();
  });
}
function download(item) {
  return `/api/projects/${props.projectId}/artifacts/${item.id}/download`;
}
</script>

<template>
  <section>
    <h2>数据与产物</h2>
    <p>
      上传数据，查看研究生成的脚本、图表和报告。文件按项目保存，每个版本都有独立
      ID。
    </p>
    <div v-if="error" class="alert alert-danger">{{ error }}</div>
    <div v-if="status && !status.available" class="alert alert-warning">
      计算环境不可用：{{ status.reason }}。仍可上传、阅读和下载文件。
    </div>
    <div class="panel collection-panel">
      <input
        aria-label="上传数据或产物"
        type="file"
        class="form-control"
        accept=".csv,.tsv,.json,.txt,.md,.py,.png,.jpg,.jpeg,.pdf"
        :disabled="!projectId || busy || loading"
        @change="upload"
      />
      <small
        >单个文件最多 8 MB。论文 PDF 请导入资料库，以获得页码和引用。</small
      >
      <div v-for="item in entries" :key="item.id" class="list-row">
        <input
          type="checkbox"
          v-model="selected"
          :value="item.id"
          :aria-label="`选择输入 ${item.name}`"
          :disabled="busy || loading"
        />
        <div>
          <strong>{{ item.name }}</strong
          ><small
            >{{ item.size }} bytes · {{ item.provenance.kind }} ·
            {{ item.created_at }}</small
          ><small>ID {{ item.id }}</small>
          <button
            v-if="/\.(csv|tsv|json|txt|md|py)$/i.test(item.name)"
            class="btn btn-link"
            @click="read(item)"
          >
            查看
          </button>
          <button
            v-if="/\.(png|jpe?g)$/i.test(item.name)"
            class="btn btn-link"
            @click="imageId = item.id"
          >
            查看图表
          </button>
          <a :href="download(item)" class="btn btn-link">下载</a>
        </div>
      </div>
      <p v-if="!entries.length" class="mt-3">暂无文件。</p>
      <button
        v-if="next !== null"
        class="btn btn-outline-secondary"
        @click="act(() => load(next))"
      >
        更多产物
      </button>
    </div>
    <div v-if="imageId" class="panel collection-panel mt-3">
      <img
        :src="`/api/projects/${projectId}/artifacts/${imageId}/image`"
        alt="研究生成的图表"
        style="max-width: 100%"
      />
    </div>
    <div v-if="preview" class="panel collection-panel mt-3">
      <strong>{{ preview.name }}</strong>
      <pre class="mt-2" style="white-space: pre-wrap">{{
        preview.content
      }}</pre>
      <button
        v-if="preview.next_offset !== null"
        class="btn btn-link"
        @click="read(preview, preview.next_offset)"
      >
        接续阅读
      </button>
    </div>
    <details class="panel collection-panel mt-3">
      <summary>执行 Python</summary>
      <p class="mt-2">
        勾选输入文件后，从 /inputs/文件名 读取；把产物写到 /work
        根目录。执行无网络，限时 20 秒。
      </p>
      <textarea
        v-model="code"
        rows="7"
        class="form-control font-monospace"
        placeholder="输入 Python 代码"
        :disabled="busy || loading"
      ></textarea>
      <button
        class="btn primary-btn mt-2"
        :disabled="
          !projectId || busy || loading || !code.trim() || !status?.available
        "
        @click="execute"
      >
        执行并保存
      </button>
      <div v-if="execution" class="mt-3">
        <strong
          >状态：{{ execution.status }} · 退出码：{{
            execution.exit_code ?? "无"
          }}</strong
        >
        <p v-if="execution.error">
          {{ execution.error.code }} {{ execution.error.message }}
        </p>
        <pre>{{ execution.stdout }}</pre>
        <pre>{{ execution.stderr }}</pre>
      </div>
    </details>
  </section>
</template>
