<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { fetchImageProblems } from '../api'
import PaginationControls from '../components/PaginationControls.vue'
import type {
  ImageProblemFilter,
  ImageProblemKind,
  ImageProblemsResult,
} from '../types'

const PAGE_SIZE = 20
const FILTERS: ImageProblemFilter[] = [
  'all',
  'invalid_url',
  'unmapped',
  'missing_file',
]

const result = ref<ImageProblemsResult | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const offset = ref(0)
const kind = ref<ImageProblemFilter>('all')

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

const kindLabels: Record<ImageProblemKind, string> = {
  invalid_url: '链接无效',
  unmapped: '未建立本地映射',
  missing_file: '本地文件缺失',
}

function integerFromParam(value: string | null): number | null {
  if (value === null || !value.trim()) {
    return null
  }
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
}

function isProblemFilter(value: string | null): value is ImageProblemFilter {
  return value !== null && FILTERS.includes(value as ImageProblemFilter)
}

function hydrateStateFromUrl(): void {
  const params = new URLSearchParams(window.location.search)
  const requestedOffset = integerFromParam(params.get('offset'))
  offset.value = requestedOffset === null ? 0 : requestedOffset
  const requestedKind = params.get('kind')
  kind.value = isProblemFilter(requestedKind) ? requestedKind : 'all'
}

function syncUrl(): void {
  const url = new URL(window.location.href)
  if (offset.value > 0) {
    url.searchParams.set('offset', String(offset.value))
  } else {
    url.searchParams.delete('offset')
  }
  if (kind.value === 'all') {
    url.searchParams.delete('kind')
  } else {
    url.searchParams.set('kind', kind.value)
  }
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
}

function caughtMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught)
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

function filterLabel(filter: ImageProblemFilter): string {
  if (filter === 'all') {
    return '全部问题'
  }
  return kindLabels[filter]
}

function filterCount(filter: ImageProblemFilter): number {
  if (result.value === null) {
    return 0
  }
  if (filter === 'all') {
    return result.value.problemPostCount
  }
  return result.value.kindCounts[filter].postCount
}

async function load(refresh = false): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const payload = await fetchImageProblems({
      offset: offset.value,
      limit: PAGE_SIZE,
      kind: kind.value,
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
}

async function setKind(nextKind: ImageProblemFilter): Promise<void> {
  if (kind.value === nextKind) {
    return
  }
  kind.value = nextKind
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

onMounted(async () => {
  hydrateStateFromUrl()
  await load(false)
})
</script>

<template>
  <main class="image-problems-shell">
    <header class="image-problems-header">
      <div>
        <h1>图片问题帖子</h1>
        <p>检查当前生效正文，定位无法由本地图片存储完整支持的帖子。</p>
      </div>
      <button
        type="button"
        class="icon-button"
        title="重新扫描"
        :disabled="loading"
        @click="refresh"
      >
        ↻
      </button>
    </header>

    <div class="image-problem-filters" aria-label="图片问题类型">
      <button
        v-for="filter in FILTERS"
        :key="filter"
        type="button"
        :class="{ active: kind === filter }"
        :disabled="loading"
        @click="setKind(filter)"
      >
        {{ filterLabel(filter) }}
        <span v-if="result">{{ formatNumber(filterCount(filter)) }}</span>
      </button>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-else-if="loading && result === null" class="empty-state">
      正在扫描当前生效正文，首次计算可能需要较长时间...
    </div>

    <template v-if="result">
      <section class="image-problem-summary" aria-label="问题摘要">
        <span><strong>{{ formatNumber(result.problemPostCount) }}</strong> 个问题帖子</span>
        <span>{{ formatNumber(result.problemThreadCount) }} 个主题</span>
        <span>{{ formatNumber(result.problemOccurrenceCount) }} 次问题引用</span>
        <span>{{ formatNumber(result.archiveCount) }} 个归档</span>
        <span>{{ formatNumber(result.scannedPostCount) }} 条正文</span>
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
        当前筛选下没有图片问题帖子。
      </div>
      <section v-else class="image-problem-posts">
        <article
          v-for="item in result.items"
          :key="`${item.dirName}:${item.pid}`"
          class="image-problem-post"
        >
          <header>
            <div>
              <strong>{{ item.title }}</strong>
              <div class="image-problem-post-meta">
                <span>tid {{ item.tid }}</span>
                <span>{{ item.floorLabel }}</span>
                <span>{{ item.authorName || '未知用户' }}</span>
                <span>{{ formatTime(item.postdate) }}</span>
                <span>{{ formatNumber(item.issueCount) }} 次问题引用</span>
              </div>
            </div>
            <RouterLink :to="item.editUrl" class="overlay-edit-button">
              设置 overlay
            </RouterLink>
          </header>

          <ul class="image-problem-issues">
            <li
              v-for="issue in item.issues"
              :key="`${issue.kind}:${issue.url}:${issue.relativePath || ''}`"
            >
              <span class="image-problem-kind" :class="`kind-${issue.kind}`">
                {{ kindLabels[issue.kind] }}
              </span>
              <code>{{ issue.url }}</code>
              <span>出现 {{ formatNumber(issue.occurrenceCount) }} 次</span>
              <span v-if="issue.relativePath">映射：{{ issue.relativePath }}</span>
            </li>
          </ul>

          <div class="post-body image-problem-post-body" v-html="item.html"></div>
        </article>
      </section>

      <PaginationControls
        :current-page="currentPage"
        :total-pages="totalPages"
        :disabled="loading"
        @change="goToPage"
      />
      <div v-if="loading" class="image-usage-loading">正在更新问题列表...</div>
    </template>
  </main>
</template>
