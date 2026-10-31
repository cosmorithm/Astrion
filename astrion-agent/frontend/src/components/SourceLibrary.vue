<script setup>
import { ref, watch } from 'vue'
import { request } from '../api.js'
const props=defineProps({projectId:String,busy:Boolean})
const emit=defineEmits(['changed'])
const docs=ref([]), error=ref(''), loading=ref(false), ocr=ref(false), query=ref(''), found=ref(null), outline=ref(null), current=ref(null), docId=ref(0), page=ref(1), selection=ref({})
async function act(fn){error.value='';loading.value=true;try{await fn()}catch(e){error.value=e.message}finally{loading.value=false}}
async function load(){docs.value=props.projectId?await request(`/api/projects/${props.projectId}/documents`):[]}
watch(()=>props.projectId,()=>{found.value=null;outline.value=null;current.value=null;act(load)}, {immediate:true})
async function upload(event){const file=event.target.files?.[0];if(!file)return;await act(async()=>{const form=new FormData();form.append('file',file);form.append('ocr',String(ocr.value));await request(`/api/projects/${props.projectId}/documents/upload`,{method:'POST',body:form});await load();emit('changed')});event.target.value=''}
async function search(offset=0){await act(async()=>{found.value=await request(`/api/projects/${props.projectId}/search?${new URLSearchParams({query:query.value,offset})}`)})}
async function open(doc){await act(async()=>{docId.value=doc;outline.value=await request(`/api/projects/${props.projectId}/documents/${doc}/outline`);current.value=null})}
async function read(options={},offset=0){await act(async()=>{selection.value=options;current.value=await request(`/api/projects/${props.projectId}/documents/${docId.value}/read?${new URLSearchParams({...options,offset})}`)})}
async function moreOutline(){await act(async()=>{const more=await request(`/api/projects/${props.projectId}/documents/${docId.value}/outline?offset=${outline.value.next_offset}`);outline.value={...more,items:[...outline.value.items,...more.items]}})}
</script>
<template>
<section>
<h2>研究资料</h2><p class="section-copy">TXT / Markdown（1 MB）或 PDF（20 MB）。按页、章节与行号核对原文。</p>
<div v-if="error" class="alert alert-danger">{{error}}</div>
<div class="panel collection-panel">
<label class="form-label">导入资料 <input type="file" accept=".txt,.md,.pdf" class="form-control" :disabled="!projectId||busy||loading" @change="upload"></label>
<label class="ms-3"><input type="checkbox" v-model="ocr"> 对扫描页尝试 OCR（需本机 Tesseract）</label>
<div v-for="doc in docs" :key="doc.id" class="list-row"><button class="btn btn-link" @click="open(doc.id)">D{{doc.id}} · {{doc.name}}</button><small>{{doc.metadata?.page_count||''}} {{doc.metadata?.warnings?.join(' · ')}}</small></div>
<form class="d-flex gap-2 my-3" @submit.prevent="search()"><input class="form-control" v-model="query" placeholder="中英文检索"><button class="btn primary-btn" :disabled="!query.trim()||loading||!projectId">检索</button></form>
<div v-if="found"><small>语义检索：{{found.retrieval.semantic}} · {{found.total}} 个结果</small><div v-for="item in found.results" :key="`${item.document_id}-${item.start_line}-${item.chunk_offset}`" class="my-3"><button class="btn btn-link" @click="open(item.document_id)">[D{{item.document_id}}:L{{item.start_line}}-L{{item.end_line}}]</button><pre style="white-space:pre-wrap">{{item.excerpt}}</pre></div><button v-if="found.next_offset!==null" class="btn btn-outline-secondary" @click="search(found.next_offset)">下一页检索结果</button></div>
</div>
<div v-if="outline" class="panel collection-panel mt-3"><strong>D{{docId}} · {{outline.name}}</strong><p>{{outline.total_lines}} 行 · {{outline.page_count}} 页</p><div v-if="outline.warnings.length" class="alert alert-warning">提取提示：{{outline.warnings.join(' · ')}}。表格、公式和版面未经过完整性验证。</div>
<button class="btn btn-outline-secondary me-2" @click="read()">从开头阅读</button><template v-if="outline.page_count"><input type="number" min="1" :max="outline.page_count" v-model="page" style="width:80px"><button class="btn btn-link" @click="read({page})">读取页码</button></template>
<div v-for="item in outline.items" :key="`${item.id}-${item.page}`"><button class="btn btn-link" @click="read(item.id?{section:item.id}:{page:item.page})">{{item.title||`第 ${item.page} 页`}} · L{{item.start_line}}–{{item.end_line}}</button></div><button v-if="outline.next_offset!==null" class="btn btn-link" @click="moreOutline">更多目录</button>
<div v-if="current" class="mt-3"><small>[D{{current.document_id}}:L{{current.start_line}}-L{{current.end_line}}] · {{current.truncated?'包含部分行，需接续':'当前行范围可定位'}}</small><pre style="white-space:pre-wrap">{{current.excerpt}}</pre><button v-if="current.next_offset!==null" class="btn btn-outline-secondary" @click="read(selection,current.next_offset)">继续阅读</button></div>
</div>
</section>
</template>
