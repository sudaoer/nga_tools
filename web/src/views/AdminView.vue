<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <main class="admin-shell">
    <nav class="admin-tabs" aria-label="管理导航">
      <RouterLink to="/admin/post-versions">正文版本</RouterLink>
      <RouterLink to="/admin/databases">数据库</RouterLink>
    </nav>
    <RouterView />
  </main>
</template>
