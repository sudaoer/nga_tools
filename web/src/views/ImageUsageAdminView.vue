<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchImageUsage } from '../api'
import type { ImageUsageResult } from '../types'

const PAGE_SIZE = 100

const result = ref<ImageUsageResult | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const offset = ref(0)

const currentPage = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
const totalPages = computed(() => {
  if (result.value === null) {
    return 1
  }
  return Math.max(1, Math.ceil(result.value.total / PAGE_SIZE))
})
const rowStart = computed(() => {
  if (result.value === null || result.value.total === 0) {
    return 0
  }
  return result.value.offset + 1
})
const rowEnd = computed(() => {
  if (result.value === null) {
    return 0
  }
  return Math.min(
    result.value.total,
    result.value.offset + result.value.items.length,
  )
})

function integerFromParam(value: string | null): number | null {
  if (value === null || !value.trim()) {
    return null
  }
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
}

function hydrateStateFromUrl(): void {
  const params = new URLSearchParams(window.location.search)
  const requestedOffset = integerFromParam(params.get('offset'))
  offset.value = requestedOffset === null ? 0 : requestedOffset
}

function syncUrl(): void {
  const url = new URL(window.location.href)
  if (offset.value > 0) {
    url.searchParams.set('offset', String(offset.value))
  } else {
    url.searchParams.delete('offset')
  }
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
}

function formatNumber(value: number): string {
  return value.toLocaleString()
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

async function load(refresh = false): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const payload = await fetchImageUsage({
      offset: offset.value,
      limit: PAGE_SIZE,
      refresh,
    })
    result.value = payload
    if (payload.total > 0 && payload.items.length === 0 && offset.value > 0) {
      offset.value = Math.floor((payload.total - 1) / PAGE_SIZE) * PAGE_SIZE
      syncUrl()
      await load(false)
    }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
  } finally {
    loading.value = false
  }
}

async function refresh(): Promise<void> {
  offset.value = 0
  syncUrl()
  await load(true)
}

async function previousPage(): Promise<void> {
  offset.value = Math.max(0, offset.value - PAGE_SIZE)
  syncUrl()
  await load(false)
}

async function nextPage(): Promise<void> {
  if (result.value === null || offset.value + PAGE_SIZE >= result.value.total) {
    return
  }
  offset.value += PAGE_SIZE
  syncUrl()
  await load(false)
}

onMounted(() => {
  hydrateStateFromUrl()
  void load(false)
})
</script>

<template>
  <main class="image-usage-shell">
    <header class="image-usage-header">
      <div>
        <h1>图片使用次数</h1>
        <p>按本地物理图片合并，统计当前生效正文中的每次出现。</p>
      </div>
      <button type="button" class="icon-button" title="重新统计" :disabled="loading" @click="refresh">
        ↻
      </button>
    </header>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-else-if="loading && result === null" class="empty-state">
      正在扫描当前正文并统计图片使用次数，首次计算可能需要较长时间...
    </div>

    <template v-if="result">
      <section class="image-usage-summary" aria-label="统计摘要">
        <span><strong>{{ formatNumber(result.total) }}</strong> 张物理图片</span>
        <span>{{ formatNumber(result.archiveCount) }} 个归档</span>
        <span>{{ formatNumber(result.postCount) }} 条正文</span>
        <span>{{ formatNumber(result.referenceCount) }} 次图片引用</span>
        <span v-if="result.unmappedReferenceCount > 0" class="warning-text">
          {{ formatNumber(result.unmappedReferenceCount) }} 次引用未映射
        </span>
        <span>统计于 {{ formatTime(result.computedAt) }}</span>
      </section>

      <details v-if="result.skippedArchives.length > 0" class="image-usage-warning">
        <summary>有 {{ result.skippedArchives.length }} 个归档无法读取，结果不完整</summary>
        <ul>
          <li v-for="item in result.skippedArchives" :key="item.dirName">
            <strong>{{ item.dirName }}</strong>：{{ item.message }}
          </li>
        </ul>
      </details>

      <div class="post-count">
        <span>{{ rowStart }}-{{ rowEnd }} / {{ formatNumber(result.total) }}</span>
        <span>第 {{ currentPage }} / {{ totalPages }} 页</span>
      </div>

      <div v-if="result.items.length === 0" class="empty-state reader-empty">
        图片索引中没有可显示的图片。
      </div>
      <div v-else class="image-usage-table-wrap">
        <table class="image-usage-table">
          <thead>
            <tr>
              <th scope="col">预览</th>
              <th scope="col">使用次数</th>
              <th scope="col">本地路径</th>
              <th scope="col">映射 URL</th>
              <th scope="col">代表 URL</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in result.items" :key="item.relativePath">
              <td>
                <a :href="item.fileUrl" target="_blank" rel="noreferrer">
                  <img :src="item.fileUrl" :alt="item.relativePath" loading="lazy" />
                </a>
              </td>
              <td class="usage-count">{{ formatNumber(item.usageCount) }}</td>
              <td class="path-cell" :title="item.relativePath">{{ item.relativePath }}</td>
              <td>{{ formatNumber(item.mappingCount) }}</td>
              <td class="url-cell" :title="item.sourceUrl">
                <a :href="item.sourceUrl" target="_blank" rel="noreferrer">{{ item.sourceUrl }}</a>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="result.total > PAGE_SIZE" class="pager">
        <button type="button" :disabled="loading || offset <= 0" @click="previousPage">上一页</button>
        <span>第 {{ currentPage }} / {{ totalPages }} 页</span>
        <button
          type="button"
          :disabled="loading || offset + PAGE_SIZE >= result.total"
          @click="nextPage"
        >
          下一页
        </button>
      </div>
      <div v-if="loading" class="image-usage-loading">正在更新统计...</div>
    </template>
  </main>
</template>
