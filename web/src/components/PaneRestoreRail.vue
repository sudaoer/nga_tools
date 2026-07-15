<script setup lang="ts">
import type { CollapsedPaneItem } from '../composables/usePersistentPaneLayout'

defineProps<{
  items: CollapsedPaneItem[]
}>()

const emit = defineEmits<{
  restore: [id: string]
}>()
</script>

<template>
  <nav v-if="items.length > 0" class="pane-restore-rail" aria-label="恢复已隐藏的侧栏">
    <button
      v-for="item in items"
      :key="item.id"
      type="button"
      class="pane-restore-button"
      :title="`显示${item.label}侧栏`"
      :aria-label="`显示${item.label}侧栏`"
      :aria-controls="item.controlsId"
      :aria-expanded="false"
      @click="emit('restore', item.id)"
    >
      <span aria-hidden="true">›</span>
      <span class="pane-restore-label">{{ item.label }}</span>
    </button>
  </nav>
</template>
