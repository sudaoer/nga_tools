<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ImageUsageDetailPanel from '../components/ImageUsageDetailPanel.vue'
import PaginationControls from '../components/PaginationControls.vue'
import { fetchImageUsage } from '../api'
import type {
  ImageUsageItem,
  ImageUsageResult,
  ImageUsageSort,
} from '../types'

const PAGE_SIZE = 100

const result = ref<ImageUsageResult | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const offset = ref(0)
const sortMetric = ref<ImageUsageSort>('usage')
const selectedPath = ref<string | null>(null)
const detailReady = ref(false)
const detailReloadToken = ref(0)

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
  sortMetric.value = params.get('sort') === 'threads' ? 'threads' : 'usage'
  selectedPath.value = params.get('image') || null
}

function syncUrl(): void {
  const url = new URL(window.location.href)
  if (offset.value > 0) {
    url.searchParams.set('offset', String(offset.value))
  } else {
    url.searchParams.delete('offset')
  }
  if (sortMetric.value === 'threads') {
    url.searchParams.set('sort', 'threads')
  } else {
    url.searchParams.delete('sort')
  }
  if (selectedPath.value !== null) {
    url.searchParams.set('image', selectedPath.value)
  } else {
    url.searchParams.delete('image')
  }
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
}

function formatNumber(value: number): string {
  return value.toLocaleString()
}

function formatTime(value: string | number | null): string {
  if (value === null) {
    return '-'
  }
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function caughtMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught)
}

async function load(refresh = false): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const payload = await fetchImageUsage({
      offset: offset.value,
      limit: PAGE_SIZE,
      sort: sortMetric.value,
      refresh,
    })
    result.value = payload
    if (payload.total > 0 && payload.items.length === 0 && offset.value > 0) {
      offset.value = Math.floor((payload.total - 1) / PAGE_SIZE) * PAGE_SIZE
      syncUrl()
      await load(false)
    }
  } catch (caught) {
    error.value = caughtMessage(caught)
  } finally {
    loading.value = false
  }
}

async function refresh(): Promise<void> {
  offset.value = 0
  syncUrl()
  await load(true)
  if (selectedPath.value !== null) {
    detailReloadToken.value += 1
  }
}

async function setSort(metric: ImageUsageSort): Promise<void> {
  if (sortMetric.value === metric) {
    return
  }
  sortMetric.value = metric
  offset.value = 0
  syncUrl()
  await load(false)
}

async function goToPage(page: number): Promise<void> {
  if (result.value === null) {
    return
  }
  const nextPage = Math.min(Math.max(Math.floor(page), 1), totalPages.value)
  const nextOffset = (nextPage - 1) * PAGE_SIZE
  if (nextOffset === offset.value) {
    return
  }
  offset.value = nextOffset
  syncUrl()
  await load(false)
}

function openImage(item: ImageUsageItem): void {
  selectedPath.value = item.relativePath
  syncUrl()
}

function closeDetail(): void {
  selectedPath.value = null
  syncUrl()
}

onMounted(async () => {
  hydrateStateFromUrl()
  await load(false)
  detailReady.value = true
})
</script>

<template>
  <main class="image-usage-shell">
    <template v-if="selectedPath === null">
      <header class="image-usage-header">
        <div>
          <h1>图片使用次数</h1>
          <p>按本地物理图片合并，统计当前生效正文中的每次出现和引用主题数。</p>
        </div>
        <button type="button" class="icon-button" title="重新统计" :disabled="loading" @click="refresh">
          ↻
        </button>
      </header>

      <div class="image-usage-sort" aria-label="图片排序方式">
        <button
          type="button"
          :class="{ active: sortMetric === 'usage' }"
          :disabled="loading"
          @click="setSort('usage')"
        >
          按出现次数
        </button>
        <button
          type="button"
          :class="{ active: sortMetric === 'threads' }"
          :disabled="loading"
          @click="setSort('threads')"
        >
          按引用主题数
        </button>
      </div>

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

        <PaginationControls
          :current-page="currentPage"
          :total-pages="totalPages"
          :disabled="loading"
          top
          @change="goToPage"
        />

        <div v-if="result.items.length === 0" class="empty-state reader-empty">
          图片索引中没有可显示的图片。
        </div>
        <div v-else class="image-usage-grid">
          <button
            v-for="item in result.items"
            :key="item.relativePath"
            type="button"
            class="image-usage-card"
            :title="item.relativePath"
            @click="openImage(item)"
          >
            <span class="image-usage-card-preview">
              <img :src="item.fileUrl" :alt="item.relativePath" loading="lazy" />
            </span>
            <span class="image-usage-card-stats">
              <template v-if="sortMetric === 'usage'">
                <strong>{{ formatNumber(item.usageCount) }} 次出现</strong>
                <span>{{ formatNumber(item.threadCount) }} 个主题</span>
              </template>
              <template v-else>
                <strong>{{ formatNumber(item.threadCount) }} 个主题</strong>
                <span>{{ formatNumber(item.usageCount) }} 次出现</span>
              </template>
            </span>
          </button>
        </div>

        <PaginationControls
          :current-page="currentPage"
          :total-pages="totalPages"
          :disabled="loading"
          @change="goToPage"
        />
        <div v-if="loading" class="image-usage-loading">正在更新统计...</div>
      </template>
    </template>

    <template v-else>
      <template v-if="!detailReady">
        <header class="image-detail-toolbar">
          <button type="button" @click="closeDetail">← 返回图片网格</button>
          <button type="button" disabled>重新统计</button>
        </header>
        <div class="empty-state">正在读取图片引用详情...</div>
      </template>
      <ImageUsageDetailPanel
        v-else
        :relative-path="selectedPath"
        back-label="← 返回图片网格"
        show-refresh
        :refreshing="loading"
        :reload-token="detailReloadToken"
        @back="closeDetail"
        @refresh="refresh"
      />
    </template>
  </main>
</template>
