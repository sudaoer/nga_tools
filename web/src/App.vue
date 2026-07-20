<script setup lang="ts">
import { provide, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import BgmToggle from './components/BgmToggle.vue'

const bgmEnabled = ref(false)
const bgmVolume = ref(0.5)
provide('bgmEnabled', bgmEnabled)
provide('bgmVolume', bgmVolume)
</script>

<template>
  <div class="web-shell">
    <header class="top-nav">
      <strong>NGA 本地查看器</strong>
      <nav aria-label="主导航">
        <RouterLink to="/threads">帖子</RouterLink>
        <RouterLink to="/admin">管理</RouterLink>
        <BgmToggle v-model:enabled="bgmEnabled" v-model:volume="bgmVolume" />
      </nav>
    </header>
    <RouterView />
  </div>
</template>
