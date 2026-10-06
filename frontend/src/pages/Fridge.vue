<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页 · 干跑只标记待下架，不改真实状态</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>{{ label[L] }}</h3>
        <span v-for="x in by(L)" :key="x.id" class="lot" :class="{ 'lot-pending': x.sweep_pending }">
          {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}<em v-if="x.sweep_pending" class="tag">待下架</em>
        </span>
      </section>
    </div>
    <div style="margin-top:12px; display:flex; gap:8px;">
      <button @click="preview" :disabled="loading">{{ previewLot ? '重新干跑' : '干跑预览' }}</button>
      <button v-if="previewLot" class="btn-ghost" @click="cancelPreview">取消预览</button>
    </div>
    <p v-if="error" class="sweep-error">{{ error }}</p>

    <div v-if="previewLot" class="sweep-card">
      <h3>干跑预览（仅列出，尚未下架）· 基准日 {{ previewLot.as_of }}</h3>
      <p v-if="!previewLot.lots.length" class="muted">没有到期日早于今天且仍在架的批次，未到期批不会被带走。</p>
      <div v-else>
        <p class="muted">以下 {{ previewLot.lots.length }} 批将在确认后下架（名单已钉死，提交只动这一列）：</p>
        <span v-for="x in previewLot.lots" :key="x.id" class="lot lot-pending">
          #{{ x.id }} {{ x.name }} ×{{ x.qty_remain }} · 到期 {{ x.expiry }} · {{ label[x.layer] }}
        </span>
      </div>
      <div style="margin-top:10px; display:flex; gap:8px;">
        <button @click="commit" :disabled="committing || !previewLot.lots.length">确认下架</button>
      </div>
    </div>

    <div v-if="commitResult" class="sweep-card">
      <h3>下架完成 · 与总表、紧急条同一世代 · 基准日 {{ commitResult.as_of }}</h3>
      <div class="sweep-diff">
        <span class="muted">本次下架 {{ commitResult.expired_ids.length }} 批<span v-if="commitResult.expired_ids.length">：#{{ commitResult.expired_ids.join('、#') }}</span>。</span><br/>
        <template v-if="commitResult.gone_after_preview.length">
          <span class="muted">期间已离架（未动）：#{{ commitResult.gone_after_preview.join('、#') }}</span><br/>
        </template>
        <template v-if="commitResult.left_unswept.length">
          <span class="muted">另有 {{ commitResult.left_unswept.length }} 批已到期但不在本次名单（干跑后入库），未带走，可重新干跑：#{{ commitResult.left_unswept.join('、#') }}</span><br/>
        </template>
      </div>
      <div style="margin-top:10px;"><button class="btn-ghost" @click="closeResult">关闭</button></div>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
const previewLot = ref(null)
const commitResult = ref(null)
const error = ref('')
const loading = ref(false)
const committing = ref(false)
function by(L) { return rows.value.filter(r => r.layer === L) }
async function load() { rows.value = await api('/fridge') }
async function refreshAll() {
  await load()
  window.dispatchEvent(new CustomEvent('pantry:generation'))
}
async function preview() {
  loading.value = true; error.value = ''; commitResult.value = null
  try { previewLot.value = await api('/expire-sweep/preview'); await refreshAll() }
  catch (e) { error.value = e.message }
  finally { loading.value = false }
}
async function cancelPreview() {
  error.value = ''
  try { await api('/expire-sweep/preview', { method: 'DELETE' }) } catch {}
  previewLot.value = null
  await refreshAll()
}
function closeResult() { commitResult.value = null }
async function commit() {
  committing.value = true; error.value = ''
  try {
    const ids = previewLot.value.lots.map(l => l.id)
    const r = await api('/expire-sweep', { method: 'POST', body: JSON.stringify({ ids }) })
    // the pinned generation is consumed; show what it actually wrote, do NOT re-preview
    previewLot.value = null
    commitResult.value = r
    await refreshAll()
  } catch (e) {
    if (/no_open_preview|generation_mismatch/.test(e.message)) {
      error.value = '名单已变化，本次未下架任何批，请重新干跑'
      previewLot.value = null
    } else {
      error.value = '提交失败，已全部回滚到提交前：' + e.message
    }
    await refreshAll()
  } finally { committing.value = false }
}
onMounted(load)
</script>
