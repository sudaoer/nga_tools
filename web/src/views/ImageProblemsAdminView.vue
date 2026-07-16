<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Pane, Splitpanes } from 'splitpanes'
import { fetchImageProblems } from '../api'
import PaginationControls from '../components/PaginationControls.vue'
import PaneRestoreRail from '../components/PaneRestoreRail.vue'
import { usePersistentPaneLayout } from '../composables/usePersistentPaneLayout'
import type {
  ImageProblemFilter,
  ImageProblemKind,
  ImageProblemPostItem,
  ImageProblemsResult,
} from '../types'

const PAGE_SIZE = 20
const PROBLEM_LIST_PANE_ID = 'image-problem-list'
const PROBLEM_KINDS: ImageProblemKind[] = [
  'invalid_url',
  'unmapped',
  'missing_file',
]
const FILTERS: ImageProblemFilter[] = ['all', ...PROBLEM_KINDS]

const {
  collapsedPanes: collapsedSidebarPanes,
  collapsePane: collapseSidebarPane,
  containerRef: paneContainerRef,
  isCollapsed: isSidebarCollapsed,
  isNarrow: isNarrowPaneLayout,
  mainMinSize: detailPaneMinSize,
  mainSize: detailPaneSize,
  onResized: onPaneResized,
  paneMaxSize: sidebarPaneMaxSize,
  paneMinSize: sidebarPaneMinSize,
  paneSize: sidebarPaneSize,
  restorePane: restoreSidebarPane,
} = usePersistentPaneLayout({
  storageKey: 'nga-tools:web-pane-layout:v1:image-problems',
  mainMinPixels: 360,
  mainMobileSize: 58,
  panes: [
    {
      id: PROBLEM_LIST_PANE_ID,
      label: '问题帖子',
      controlsId: 'image-problem-list-pane',
      defaultSize: 30,
      minPixels: 240,
      maxSize: 55,
      mobileSize: 42,
    },
  ],
})

const result = ref<ImageProblemsResult | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const offset = ref(0)
const kind = ref<ImageProblemFilter>('all')
const selectedTid = ref<number | null>(null)
const selectedAidKey = ref<string | null>(null)
const selectedLou = ref<number | null>(null)

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
const selectedPost = computed<ImageProblemPostItem | null>(() => {
  if (
    result.value === null ||
    selectedTid.value === null ||
    selectedAidKey.value === null ||
    selectedLou.value === null
  ) {
    return null
  }
  return (
    result.value.items.find(
      (item) =>
        item.tid === selectedTid.value &&
        item.aidKey === selectedAidKey.value &&
        item.lou === selectedLou.value,
    ) || null
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

function setSelectedPost(item: ImageProblemPostItem | null): void {
  selectedTid.value = item?.tid ?? null
  selectedAidKey.value = item?.aidKey ?? null
  selectedLou.value = item?.lou ?? null
}

function hydrateStateFromUrl(): void {
  const params = new URLSearchParams(window.location.search)
  const requestedOffset = integerFromParam(params.get('offset'))
  offset.value = requestedOffset === null ? 0 : requestedOffset
  const requestedKind = params.get('kind')
  kind.value = isProblemFilter(requestedKind) ? requestedKind : 'all'

  const requestedTid = integerFromParam(params.get('tid'))
  const requestedAidKey = params.get('aid')?.trim() || null
  const requestedLou = integerFromParam(params.get('lou'))
  if (
    requestedTid !== null &&
    requestedAidKey !== null &&
    requestedLou !== null
  ) {
    selectedTid.value = requestedTid
    selectedAidKey.value = requestedAidKey
    selectedLou.value = requestedLou
  }
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
  if (
    selectedTid.value !== null &&
    selectedAidKey.value !== null &&
    selectedLou.value !== null
  ) {
    url.searchParams.set('tid', String(selectedTid.value))
    url.searchParams.set('aid', selectedAidKey.value)
    url.searchParams.set('lou', String(selectedLou.value))
  } else {
    url.searchParams.delete('tid')
    url.searchParams.delete('aid')
    url.searchParams.delete('lou')
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

function formatImageIndexes(imageIndexes: number[]): string {
  return `第 ${imageIndexes.join('、')} 张图片`
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

function issueCountForKind(
  item: ImageProblemPostItem,
  problemKind: ImageProblemKind,
): number {
  return item.issues
    .filter((issue) => issue.kind === problemKind)
    .reduce((total, issue) => total + issue.occurrenceCount, 0)
}

function reconcileSelection(payload: ImageProblemsResult): void {
  const requestedItem = payload.items.find(
    (item) =>
      item.tid === selectedTid.value &&
      item.aidKey === selectedAidKey.value &&
      item.lou === selectedLou.value,
  )
  setSelectedPost(requestedItem || payload.items[0] || null)
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
    if (payload.total > 0 && payload.items.length === 0 && offset.value > 0) {
      offset.value = Math.floor((payload.total - 1) / PAGE_SIZE) * PAGE_SIZE
      await load(false)
      return
    }
    result.value = payload
    reconcileSelection(payload)
    syncUrl()
  } catch (caught) {
    error.value = caughtMessage(caught)
  } finally {
    loading.value = false
  }
}

async function refresh(): Promise<void> {
  offset.value = 0
  await load(true)
}

async function setKind(nextKind: ImageProblemFilter): Promise<void> {
  if (kind.value === nextKind) {
    return
  }
  kind.value = nextKind
  offset.value = 0
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
  await load(false)
}

function selectPost(item: ImageProblemPostItem): void {
  setSelectedPost(item)
  syncUrl()
}

onMounted(async () => {
  hydrateStateFromUrl()
  await load(false)
})
</script>

<template>
  <main class="image-problems-shell pane-layout-shell">
    <PaneRestoreRail :items="collapsedSidebarPanes" @restore="restoreSidebarPane" />
    <div ref="paneContainerRef" class="pane-split-area">
      <Splitpanes
        class="nga-splitpanes"
        :horizontal="isNarrowPaneLayout"
        :maximize-panes="false"
        :keyboard-step="isNarrowPaneLayout ? 0 : 2"
        @resized="onPaneResized"
      >
        <Pane
          v-if="!isSidebarCollapsed(PROBLEM_LIST_PANE_ID)"
          :size="sidebarPaneSize(PROBLEM_LIST_PANE_ID)"
          :min-size="sidebarPaneMinSize(PROBLEM_LIST_PANE_ID)"
          :max-size="sidebarPaneMaxSize(PROBLEM_LIST_PANE_ID)"
        >
          <aside id="image-problem-list-pane" class="image-problem-list-pane">
            <div class="pane-header">
              <h1>图片问题</h1>
              <div class="pane-header-actions">
                <button
                  type="button"
                  class="icon-button"
                  title="重新扫描"
                  aria-label="重新扫描"
                  :disabled="loading"
                  @click="refresh"
                >
                  ↻
                </button>
                <button
                  type="button"
                  class="icon-button pane-collapse-button"
                  title="隐藏问题帖子侧栏"
                  aria-label="隐藏问题帖子侧栏"
                  aria-controls="image-problem-list-pane"
                  :aria-expanded="true"
                  @click="collapseSidebarPane(PROBLEM_LIST_PANE_ID)"
                >
                  «
                </button>
              </div>
            </div>

            <p class="image-problem-intro">
              检查当前生效正文，定位无法由本地图片存储完整支持的帖子。
            </p>

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
              <section
                class="image-problem-summary image-problem-sidebar-summary"
                aria-label="问题摘要"
              >
                <span>
                  <strong>{{ formatNumber(result.problemPostCount) }}</strong>
                  个问题帖子
                </span>
                <span>{{ formatNumber(result.problemThreadCount) }} 个主题</span>
                <span>{{ formatNumber(result.problemOccurrenceCount) }} 次问题引用</span>
                <span>{{ formatNumber(result.archiveCount) }} 个归档</span>
                <span>{{ formatNumber(result.scannedPostCount) }} 条正文</span>
                <span>统计于 {{ formatTime(result.computedAt) }}</span>
              </section>

              <details v-if="result.skippedArchives.length > 0" class="image-usage-warning">
                <summary>有 {{ result.skippedArchives.length }} 个归档无法读取</summary>
                <ul>
                  <li v-for="item in result.skippedArchives" :key="item.dirName">
                    <strong>{{ item.dirName }}</strong>：{{ item.message }}
                  </li>
                </ul>
              </details>

              <div class="post-count image-problem-list-count">
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

              <div v-if="result.items.length === 0" class="empty-state">
                当前筛选下没有图片问题帖子。
              </div>
              <div v-else class="image-problem-post-list">
                <button
                  v-for="item in result.items"
                  :key="`${item.dirName}:${item.pid}`"
                  type="button"
                  class="image-problem-post-item"
                  :class="{
                    selected:
                      item.tid === selectedTid &&
                      item.aidKey === selectedAidKey &&
                      item.lou === selectedLou,
                  }"
                  :aria-pressed="
                    item.tid === selectedTid &&
                    item.aidKey === selectedAidKey &&
                    item.lou === selectedLou
                  "
                  @click="selectPost(item)"
                >
                  <span class="thread-title">{{ item.title }}</span>
                  <span class="image-problem-post-meta">
                    <span>{{ item.floorLabel }}</span>
                    <span>{{ item.authorName || '未知用户' }}</span>
                    <span>{{ formatTime(item.postdate) }}</span>
                    <span>{{ formatNumber(item.issueCount) }} 次问题引用</span>
                  </span>
                  <span class="image-problem-item-kinds">
                    <template v-for="problemKind in PROBLEM_KINDS" :key="problemKind">
                      <span
                        v-if="issueCountForKind(item, problemKind) > 0"
                        class="image-problem-kind"
                        :class="`kind-${problemKind}`"
                      >
                        {{ kindLabels[problemKind] }}
                        {{ formatNumber(issueCountForKind(item, problemKind)) }}
                      </span>
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
              <div v-if="loading" class="image-usage-loading">正在更新问题列表...</div>
            </template>
          </aside>
        </Pane>

        <Pane :size="detailPaneSize" :min-size="detailPaneMinSize" :max-size="100">
          <section class="image-problem-detail-pane">
            <div v-if="error && result === null" class="error-box">{{ error }}</div>
            <div v-else-if="loading && result === null" class="empty-state reader-empty">
              正在读取图片问题帖子...
            </div>
            <div v-else-if="selectedPost === null" class="empty-state reader-empty">
              当前筛选下没有可查看的问题帖子。
            </div>

            <article v-else class="image-problem-post image-problem-selected-post">
              <header>
                <div>
                  <strong>{{ selectedPost.title }}</strong>
                  <div class="image-problem-post-meta">
                    <span>tid {{ selectedPost.tid }}</span>
                    <span>{{ selectedPost.floorLabel }}</span>
                    <span>{{ selectedPost.authorName || '未知用户' }}</span>
                    <span>{{ formatTime(selectedPost.postdate) }}</span>
                    <span>{{ formatNumber(selectedPost.issueCount) }} 次问题引用</span>
                  </div>
                </div>
                <RouterLink :to="selectedPost.editUrl" class="overlay-edit-button">
                  设置 overlay
                </RouterLink>
              </header>

              <ul class="image-problem-issues">
                <li
                  v-for="issue in selectedPost.issues"
                  :key="`${issue.kind}:${issue.url}:${issue.relativePath || ''}`"
                >
                  <span class="image-problem-kind" :class="`kind-${issue.kind}`">
                    {{ kindLabels[issue.kind] }}
                  </span>
                  <strong>{{ formatImageIndexes(issue.imageIndexes) }}</strong>
                  <code>{{ issue.url }}</code>
                  <span>出现 {{ formatNumber(issue.occurrenceCount) }} 次</span>
                  <span v-if="issue.relativePath">映射：{{ issue.relativePath }}</span>
                </li>
              </ul>

              <div
                class="post-body image-problem-post-body"
                v-html="selectedPost.html"
              ></div>
            </article>
            <div v-if="loading && result !== null" class="image-usage-loading">
              正在更新问题列表...
            </div>
          </section>
        </Pane>
      </Splitpanes>
    </div>
  </main>
</template>
