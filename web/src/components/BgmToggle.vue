<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

const ENABLED_KEY = 'nga-tools:web-bgm-enabled'
const VOLUME_KEY = 'nga-tools:web-bgm-volume'
const DEFAULT_VOLUME = 0.5

const enabledModel = defineModel<boolean>('enabled', { required: true })
const volumeModel = defineModel<number>('volume', { required: true })

const enabled = ref(enabledModel.value)
const volume = ref(volumeModel.value)

function clampVolume(value: number): number {
  if (Number.isNaN(value)) {
    return DEFAULT_VOLUME
  }
  return Math.min(1, Math.max(0, value))
}

onMounted(() => {
  try {
    const storedEnabled = localStorage.getItem(ENABLED_KEY)
    if (storedEnabled === '1') {
      enabled.value = true
    } else if (storedEnabled === '0') {
      enabled.value = false
    }
    const storedVolume = localStorage.getItem(VOLUME_KEY)
    if (storedVolume !== null) {
      volume.value = clampVolume(Number(storedVolume))
    }
  } catch {
    // 忽略读取失败
  }
  enabledModel.value = enabled.value
  volumeModel.value = volume.value
})

watch(enabled, (value) => {
  enabledModel.value = value
  try {
    localStorage.setItem(ENABLED_KEY, value ? '1' : '0')
  } catch {
    // 忽略写入失败
  }
})

watch(volume, (value) => {
  const clamped = clampVolume(value)
  volume.value = clamped
  volumeModel.value = clamped
  try {
    localStorage.setItem(VOLUME_KEY, String(clamped))
  } catch {
    // 忽略写入失败
  }
})

function toggle(): void {
  enabled.value = !enabled.value
}

function onVolumeInput(event: Event): void {
  const target = event.target as HTMLInputElement
  volume.value = clampVolume(Number(target.value) / 100)
}
</script>

<template>
  <div class="bgm-control">
    <button
      type="button"
      class="bgm-toggle"
      :class="{ 'bgm-toggle--on': enabled }"
      :aria-pressed="enabled"
      :title="enabled ? '关闭帖子 BGM 自动播放' : '开启帖子 BGM 自动播放'"
      @click="toggle"
    >
      <span aria-hidden="true">{{ enabled ? '♪' : '♪̸' }}</span>
      <span class="bgm-toggle-label">BGM</span>
    </button>
    <input
      class="bgm-volume"
      type="range"
      min="0"
      max="100"
      step="1"
      :value="Math.round(volume * 100)"
      :disabled="!enabled"
      aria-label="BGM 音量"
      :title="`BGM 音量 ${Math.round(volume * 100)}%`"
      @input="onVolumeInput"
    />
  </div>
</template>
