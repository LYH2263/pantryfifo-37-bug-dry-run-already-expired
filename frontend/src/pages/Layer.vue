<template>
  <div>
    <h1>{{ props.layer }} 层 · 虚线为干跑名单（仍在架）</h1>
    <span v-for="x in rows" :key="x.id" class="lot" :class="{ 'lot-pending': x.will_sweep }">
      {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}<em v-if="x.will_sweep" class="tag">待下架</em>
    </span>
  </div>
</template>
<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
const props = defineProps({ layer: String })
const rows = ref([])
async function load() { rows.value = await api('/fridge?layer=' + props.layer) }
function onGeneration() { load() }
watch(() => props.layer, load)
onMounted(() => {
  load()
  window.addEventListener('pantry:generation', onGeneration)
})
onBeforeUnmount(() => window.removeEventListener('pantry:generation', onGeneration))
</script>
