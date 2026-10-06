<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页 · 干跑只钉名单，虚线「待下架」批在总表仍为在架，确认后才真正过期</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>{{ label[L] }}</h3>
        <span v-for="x in by(L)" :key="x.id" class="lot" :class="{ 'lot-pending': x.will_sweep }">
          {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}<em v-if="x.will_sweep" class="tag">待下架</em>
        </span>
      </section>
    </div>
    <div style="margin-top:12px; display:flex; gap:8px;">
      <button @click="preview" :disabled="loading">{{ previewLot ? '重新干跑' : '干跑预览' }}</button>
      <button v-if="previewLot" class="btn-ghost" @click="cancelPreview">取消预览</button>
    </div>

    <div v-if="previewLot" class="sweep-card">
      <h3>干跑预览（仅列出，尚未下架）· 世代 #{{ previewLot.generation }} · 基准日 {{ previewLot.as_of }}</h3>
      <p v-if="!previewLot.lots.length" class="muted">没有到期日早于今天且仍在架的批次，未到期批不会被带走。</p>
      <div v-else>
        <p class="muted">以下 {{ previewLot.lots.length }} 批已钉死为本世代名单，确认后下架（干跑后新入库的过期批不在其中）：</p>
        <span v-for="x in previewLot.lots" :key="x.id" class="lot lot-pending">
          #{{ x.id }} {{ x.name }} ×{{ x.qty_remain }} · 到期 {{ x.expiry }} · {{ label[x.layer] }}<em class="tag">待下架</em>
        </span>
      </div>
      <p v-if="error" class="sweep-error">{{ error }}</p>
      <div style="margin-top:10px; display:flex; gap:8px;">
        <button @click="commit" :disabled="committing || !previewLot.lots.length">确认下架</button>
      </div>
    </div>

    <div v-if="commitResult" class="sweep-card">
      <h3>已提交 · 世代 #{{ commitResult.generation }}（干跑基准 {{ commitResult.as_of }}）</h3>
      <p class="muted" style="margin:0;">
        实际下架 {{ commitResult.expired_ids.length }} 批<span v-if="commitResult.expired_ids.length">：#{{ commitResult.expired_ids.join('、#') }}</span>；
        <template v-if="commitResult.skipped_not_on_shelf.length">
          钉名单后已被扣减打完/离架而跳过 {{ commitResult.skipped_not_on_shelf.length }} 批：#{{ commitResult.skipped_not_on_shelf.join('、#') }}；
        </template>
        总表、分层页与紧急条已同世代切换，再次提交不会重写已过期批。
      </p>
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
// tell the alert bar and layer pages to read the same generation
function bump() { window.dispatchEvent(new CustomEvent('pantry:generation')) }
async function load() { rows.value = await api('/fridge') }
async function preview() {
  loading.value = true; error.value = ''; commitResult.value = null
  try {
    previewLot.value = await api('/expire-sweep/preview')
    await load()
    bump()
  } catch (e) { error.value = e.message }
  finally { loading.value = false }
}
async function cancelPreview() {
  error.value = ''
  try { await api('/expire-sweep/preview', { method: 'DELETE' }) }
  catch (e) { error.value = e.message }
  previewLot.value = null; commitResult.value = null
  await load(); bump()
}
async function commit() {
  committing.value = true; error.value = ''
  try {
    const r = await api('/expire-sweep', {
      method: 'POST',
      body: JSON.stringify({ generation: previewLot.value.generation }),
    })
    commitResult.value = r
    // generation consumed: views now read the post-commit generation together
    previewLot.value = null
    await load(); bump()
  } catch (e) {
    // 中途失败：后端已整笔回滚（含名单世代），三视图仍属提交前世代
    if (e.message === 'generation_mismatch' || e.message === 'no_pinned_preview') {
      error.value = '钉死的名单世代已失效，已按当下重新干跑，请照新名单确认：' + e.message
      try { previewLot.value = await api('/expire-sweep/preview'); await load(); bump() }
      catch (e2) { error.value += '；重新干跑也失败：' + e2.message }
    } else {
      error.value = '提交失败，已全部回滚到提交前：' + e.message
      await load(); bump()
    }
  } finally { committing.value = false }
}
onMounted(load)
</script>
