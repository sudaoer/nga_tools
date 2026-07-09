<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <div class="web-shell">
    <header class="top-nav">
      <strong>NGA 本地查看器</strong>
      <nav aria-label="主导航">
        <RouterLink to="/threads">帖子</RouterLink>
        <RouterLink to="/databases">数据库</RouterLink>
      </nav>
    </header>
    <RouterView />
  </div>
</template>
