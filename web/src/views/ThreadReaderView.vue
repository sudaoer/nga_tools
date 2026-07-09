<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  clearPostOverlay,
  fetchPostOverlay,
  fetchPosts,
  fetchThread,
  fetchThreads,
  previewPostOverlay,
  savePostOverlay,
  type PostQuery,
} from '../api'
import type { PostItem, PostsResult, ThreadStatus, ThreadSummary } from '../types'

type SortKey =
  | 'backupUpdated'
  | 'authorUpdated'
  | 'postCount'
  | 'bodyWordCount'
  | 'maxLou'
  | 'lastpost'
  | 'postdate'
  | 'title'
  | 'author'
type SortDirection = 'asc' | 'desc'
type PageToken =
  | { type: 'page'; page: number; key: string }
  | { type: 'ellipsis'; key: string }

const SORT_KEYS: SortKey[] = [
  'backupUpdated',
  'authorUpdated',
  'postCount',
  'bodyWordCount',
  'maxLou',
  'lastpost',
  'postdate',
  'title',
  'author',
]
const THREAD_STATUSES: ThreadStatus[] = ['ready', 'needs_migration', 'missing_html', 'invalid']

const threads = ref<ThreadSummary[]>([])
const selectedThread = ref<ThreadSummary | null>(null)
const posts = ref<PostsResult | null>(null)
const threadError = ref<string | null>(null)
const postError = ref<string | null>(null)
const loadingThreads = ref(false)
const loadingThreadStats = ref(false)
const loadingPosts = ref(false)
const requestedThread = ref<{ tid: number; aidKey: string } | null>(null)
const pageJumpInput = ref('')
let threadListRequestId = 0
let threadStatsRequestId = 0
let postRequestId = 0
const overlayEditor = reactive({
  lou: null as number | null,
  floorLabel: '',
  bbcode: '',
  previewHtml: null as string | null,
  error: null as string | null,
  loading: false,
  previewing: false,
  saving: false,
})

const listFilter = reactive({
  q: '',
  status: 'all' as ThreadStatus | 'all',
  sortBy: 'authorUpdated' as SortKey,
  sortDirection: 'desc' as SortDirection,
})

const postQuery = reactive<PostQuery>({
  page: 1,
  q: '',
  louFrom: '',
  louTo: '',
})

const sortLabels: Record<SortKey, string> = {
  backupUpdated: '备份更新',
  authorUpdated: '作者最后发言',
  postCount: '楼层数',
  bodyWordCount: '正文字数',
  maxLou: '最高楼',
  lastpost: '主题最新回复',
  postdate: '主题发布时间',
  title: '标题',
  author: '作者',
}

const visibleThreads = computed(() => {
  const keyword = listFilter.q.trim().toLowerCase()
  const filtered = threads.value.filter((thread) => {
    if (listFilter.status !== 'all' && thread.status !== listFilter.status) {
      return false
    }
    if (!keyword) {
      return true
    }
    return [
      thread.threadName,
      thread.subject,
      thread.author,
      thread.dirName,
      String(thread.tid),
    ].some((value) => value?.toLowerCase().includes(keyword))
  })
  return filtered.toSorted(compareThreads)
})

const pageStart = computed(() => (posts.value ? posts.value.pageStartLou : 0))
const pageEnd = computed(() => (posts.value ? posts.value.pageEndLou : 0))
const pagerTokens = computed<PageToken[]>(() => {
  if (!posts.value || posts.value.totalPages <= 1) {
    return []
  }
  const totalPages = posts.value.totalPages
  const currentPage = posts.value.page
  if (totalPages <= 9) {
    return Array.from({ length: totalPages }, (_item, index) => ({
      type: 'page',
      page: index + 1,
      key: `page-${index + 1}`,
    }))
  }

  const pages = new Set<number>([1, totalPages])
  for (let page = currentPage - 2; page <= currentPage + 2; page += 1) {
    if (page > 1 && page < totalPages) {
      pages.add(page)
    }
  }

  const tokens: PageToken[] = []
  let previousPage: number | null = null
  for (const page of [...pages].sort((left, right) => left - right)) {
    if (previousPage !== null && page > previousPage + 1) {
      tokens.push({ type: 'ellipsis', key: `ellipsis-${previousPage}-${page}` })
    }
    tokens.push({ type: 'page', page, key: `page-${page}` })
    previousPage = page
  }
  return tokens
})

function titleFor(thread: ThreadSummary): string {
  return thread.subject || thread.threadName || thread.dirName
}

function statusLabel(status: ThreadStatus): string {
  const labels: Record<ThreadStatus, string> = {
    ready: '可阅读',
    needs_migration: '需迁移',
    missing_html: '缺HTML',
    invalid: '无效',
  }
  return labels[status]
}

function dateFromValue(value: string | number): Date | null {
  if (typeof value === 'number') {
    return new Date(value * 1000)
  }
  const normalizedValue = value.trim()
  const match = normalizedValue.match(
    /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/,
  )
  if (match) {
    const [, year, month, day, hour, minute, second] = match
    return new Date(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
      Number(second || '0'),
    )
  }
  const date = new Date(normalizedValue)
  if (Number.isNaN(date.getTime())) {
    return null
  }
  return date
}

function formatTime(value: string | number | null): string {
  if (value === null) {
    return '-'
  }
  const date = dateFromValue(value)
  if (date === null) {
    return typeof value === 'string' && value.trim() ? value : '-'
  }
  return date.toLocaleString()
}

function formatNumber(value: number | null): string {
  return value === null ? '-' : value.toLocaleString()
}

function timeSortValue(value: string | number | null): number | null {
  if (value === null) {
    return null
  }
  const date = dateFromValue(value)
  return date === null ? null : date.getTime()
}

function sortValue(thread: ThreadSummary, key: SortKey): string | number | null {
  if (key === 'backupUpdated') {
    return timeSortValue(thread.updatedAt)
  }
  if (key === 'authorUpdated') {
    return timeSortValue(thread.authorUpdatedAt) ?? (
      thread.statsLoaded ? null : timeSortValue(thread.updatedAt)
    )
  }
  if (key === 'postCount') {
    return thread.postCount
  }
  if (key === 'bodyWordCount') {
    return thread.bodyWordCount
  }
  if (key === 'maxLou') {
    return thread.maxLou
  }
  if (key === 'lastpost') {
    return thread.lastpost
  }
  if (key === 'postdate') {
    return thread.postdate
  }
  if (key === 'author') {
    return (thread.author || thread.threadName || thread.dirName).toLowerCase()
  }
  return titleFor(thread).toLowerCase()
}

function compareSortValues(
  leftValue: string | number | null,
  rightValue: string | number | null,
): number {
  if (leftValue === null && rightValue === null) {
    return 0
  }
  if (leftValue === null) {
    return 1
  }
  if (rightValue === null) {
    return -1
  }
  if (typeof leftValue === 'number' && typeof rightValue === 'number') {
    return leftValue - rightValue
  }
  return String(leftValue).localeCompare(String(rightValue), 'zh-Hans-CN')
}

function compareThreads(left: ThreadSummary, right: ThreadSummary): number {
  const leftValue = sortValue(left, listFilter.sortBy)
  const rightValue = sortValue(right, listFilter.sortBy)
  if (leftValue === null && rightValue !== null) {
    return 1
  }
  if (leftValue !== null && rightValue === null) {
    return -1
  }
  const result = compareSortValues(leftValue, rightValue)
  if (result !== 0) {
    return listFilter.sortDirection === 'desc' ? -result : result
  }
  return titleFor(left).localeCompare(titleFor(right), 'zh-Hans-CN')
}

function slotUserLabel(post: PostItem): string {
  if (post.authorName) {
    return post.authorName
  }
  if (post.emptyReason === 'missing') {
    return '内容缺失'
  }
  if (post.emptyReason === 'filtered') {
    return '未匹配'
  }
  return '未知用户'
}

function closeOverlayEditor(): void {
  overlayEditor.lou = null
  overlayEditor.floorLabel = ''
  overlayEditor.bbcode = ''
  overlayEditor.previewHtml = null
  overlayEditor.error = null
  overlayEditor.loading = false
  overlayEditor.previewing = false
  overlayEditor.saving = false
}

async function openOverlayEditor(post: PostItem): Promise<void> {
  if (!selectedThread.value || post.pid === null) {
    return
  }
  overlayEditor.lou = post.lou
  overlayEditor.floorLabel = post.floorLabel
  overlayEditor.bbcode = ''
  overlayEditor.previewHtml = null
  overlayEditor.error = null
  overlayEditor.loading = true
  try {
    const detail = await fetchPostOverlay(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      post.lou,
    )
    overlayEditor.floorLabel = detail.floorLabel
    overlayEditor.bbcode = detail.bbcode
    overlayEditor.previewHtml = detail.html
  } catch (error) {
    overlayEditor.error = error instanceof Error ? error.message : String(error)
  } finally {
    overlayEditor.loading = false
  }
}

async function previewOverlay(): Promise<void> {
  if (!selectedThread.value || overlayEditor.lou === null) {
    return
  }
  overlayEditor.error = null
  overlayEditor.previewing = true
  try {
    const preview = await previewPostOverlay(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      overlayEditor.lou,
      overlayEditor.bbcode,
    )
    overlayEditor.previewHtml = preview.html
  } catch (error) {
    overlayEditor.error = error instanceof Error ? error.message : String(error)
  } finally {
    overlayEditor.previewing = false
  }
}

async function saveOverlay(): Promise<void> {
  if (!selectedThread.value || overlayEditor.lou === null) {
    return
  }
  overlayEditor.error = null
  overlayEditor.saving = true
  try {
    const detail = await savePostOverlay(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      overlayEditor.lou,
      overlayEditor.bbcode,
    )
    overlayEditor.previewHtml = detail.html
    await loadPosts()
  } catch (error) {
    overlayEditor.error = error instanceof Error ? error.message : String(error)
  } finally {
    overlayEditor.saving = false
  }
}

async function clearOverlay(): Promise<void> {
  if (!selectedThread.value || overlayEditor.lou === null) {
    return
  }
  overlayEditor.error = null
  overlayEditor.saving = true
  try {
    await clearPostOverlay(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      overlayEditor.lou,
    )
    closeOverlayEditor()
    await loadPosts()
  } catch (error) {
    overlayEditor.error = error instanceof Error ? error.message : String(error)
  } finally {
    overlayEditor.saving = false
  }
}

function pageFromLouInput(value: string): number | null {
  const trimmedValue = value.trim()
  if (!trimmedValue) {
    return null
  }
  const lou = Number(trimmedValue)
  if (!Number.isFinite(lou) || lou < 0) {
    return null
  }
  return Math.floor(lou / 20) + 1
}

function integerFromParam(value: string | null): number | null {
  if (value === null || !value.trim()) {
    return null
  }
  const numberValue = Number(value)
  if (!Number.isInteger(numberValue) || numberValue < 1) {
    return null
  }
  return numberValue
}

function isThreadStatus(value: string | null): value is ThreadStatus {
  return value !== null && THREAD_STATUSES.includes(value as ThreadStatus)
}

function isSortKey(value: string | null): value is SortKey {
  return value !== null && SORT_KEYS.includes(value as SortKey)
}

function isSortDirection(value: string | null): value is SortDirection {
  return value === 'asc' || value === 'desc'
}

function hydrateStateFromUrl(): void {
  const params = new URLSearchParams(window.location.search)
  const tid = integerFromParam(params.get('tid'))
  const aidKey = params.get('aid')
  if (tid !== null && aidKey !== null && aidKey.trim()) {
    requestedThread.value = { tid, aidKey }
  }

  const page = integerFromParam(params.get('page'))
  if (page !== null) {
    postQuery.page = page
  }
  postQuery.q = params.get('post_q') || ''
  postQuery.louFrom = params.get('lou_from') || ''
  postQuery.louTo = params.get('lou_to') || ''
  listFilter.q = params.get('list_q') || ''

  const status = params.get('status')
  if (status === 'all' || isThreadStatus(status)) {
    listFilter.status = status
  }
  const sortBy = params.get('sort')
  if (isSortKey(sortBy)) {
    listFilter.sortBy = sortBy
  }
  const sortDirection = params.get('dir')
  if (isSortDirection(sortDirection)) {
    listFilter.sortDirection = sortDirection
  }
}

function setParam(params: URLSearchParams, key: string, value: string | number | null): void {
  if (value !== null && String(value).trim()) {
    params.set(key, String(value))
  }
}

function syncUrl(): void {
  const params = new URLSearchParams()
  if (selectedThread.value !== null) {
    params.set('tid', String(selectedThread.value.tid))
    params.set('aid', selectedThread.value.aidKey)
  }
  if (postQuery.page !== 1) {
    params.set('page', String(postQuery.page))
  }
  setParam(params, 'post_q', postQuery.q.trim())
  setParam(params, 'lou_from', postQuery.louFrom.trim())
  setParam(params, 'lou_to', postQuery.louTo.trim())
  setParam(params, 'list_q', listFilter.q.trim())
  if (listFilter.status !== 'all') {
    params.set('status', listFilter.status)
  }
  if (listFilter.sortBy !== 'authorUpdated') {
    params.set('sort', listFilter.sortBy)
  }
  if (listFilter.sortDirection !== 'desc') {
    params.set('dir', listFilter.sortDirection)
  }

  const query = params.toString()
  const nextUrl = `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`
  window.history.replaceState(null, '', nextUrl)
}

function threadKey(thread: ThreadSummary): string {
  return `${thread.tid}:${thread.aidKey}`
}

function mergeFullThreadStats(fullThreads: ThreadSummary[]): void {
  const fullByKey = new Map(fullThreads.map((thread) => [threadKey(thread), thread]))
  threads.value = threads.value.map((thread) => fullByKey.get(threadKey(thread)) || thread)
  if (selectedThread.value !== null) {
    selectedThread.value = fullByKey.get(threadKey(selectedThread.value)) || selectedThread.value
  }
}

async function loadFullThreadStats(refresh: boolean): Promise<void> {
  const requestId = ++threadStatsRequestId
  loadingThreadStats.value = true
  try {
    const fullThreads = await fetchThreads({ detail: 'full', refresh })
    if (requestId !== threadStatsRequestId) {
      return
    }
    mergeFullThreadStats(fullThreads)
  } catch (error) {
    if (threads.value.length === 0) {
      threadError.value = error instanceof Error ? error.message : String(error)
    }
  } finally {
    if (requestId === threadStatsRequestId) {
      loadingThreadStats.value = false
    }
  }
}

function findInitialThread(threadList: ThreadSummary[]): {
  target: ThreadSummary | null
  requestedMatched: boolean
} {
  const currentThread =
    selectedThread.value === null
      ? null
      : threadList.find(
          (thread) =>
            thread.tid === selectedThread.value?.tid &&
            thread.aidKey === selectedThread.value?.aidKey,
        ) || null
  const requested =
    requestedThread.value === null
      ? null
      : threadList.find(
          (thread) =>
            thread.tid === requestedThread.value?.tid &&
            thread.aidKey === requestedThread.value?.aidKey,
        ) || null
  return {
    target: currentThread || requested || threadList.find((thread) => thread.status === 'ready') || null,
    requestedMatched: requested !== null,
  }
}

async function loadThreads(refresh = false): Promise<void> {
  const requestId = ++threadListRequestId
  loadingThreads.value = true
  threadError.value = null
  try {
    const lightThreads = await fetchThreads({ detail: 'light', refresh })
    if (requestId !== threadListRequestId) {
      return
    }
    threads.value = lightThreads
    const { target, requestedMatched } = findInitialThread(lightThreads)
    requestedThread.value = null
    loadingThreads.value = false
    if (target !== null && selectedThread.value === null) {
      void selectThread(target, { resetPage: !requestedMatched })
    }
    void loadFullThreadStats(refresh)
  } catch (error) {
    if (requestId === threadListRequestId) {
      threadError.value = error instanceof Error ? error.message : String(error)
    }
  } finally {
    if (requestId === threadListRequestId) {
      loadingThreads.value = false
    }
  }
}

function refreshThreads(): void {
  void loadThreads(true)
}

async function selectThread(
  thread: ThreadSummary,
  options: { resetPage: boolean } = { resetPage: true },
): Promise<void> {
  const requestId = ++postRequestId
  selectedThread.value = thread
  posts.value = null
  postError.value = null
  closeOverlayEditor()
  if (options.resetPage) {
    postQuery.page = 1
  }
  if (thread.status !== 'ready') {
    return
  }
  loadingPosts.value = true
  try {
    const [threadDetail, postResult] = await Promise.all([
      fetchThread(thread.tid, thread.aidKey),
      fetchPosts(thread.tid, thread.aidKey, postQuery),
    ])
    if (requestId !== postRequestId) {
      return
    }
    selectedThread.value = threadDetail
    posts.value = postResult
    postQuery.page = postResult.page
  } catch (error) {
    if (requestId === postRequestId) {
      postError.value = error instanceof Error ? error.message : String(error)
    }
  } finally {
    if (requestId === postRequestId) {
      loadingPosts.value = false
    }
  }
}

async function loadPosts(): Promise<void> {
  if (!selectedThread.value || selectedThread.value.status !== 'ready') {
    return
  }
  const requestId = ++postRequestId
  loadingPosts.value = true
  postError.value = null
  try {
    const postResult = await fetchPosts(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      postQuery,
    )
    if (requestId !== postRequestId) {
      return
    }
    posts.value = postResult
    postQuery.page = postResult.page
  } catch (error) {
    if (requestId === postRequestId) {
      postError.value = error instanceof Error ? error.message : String(error)
    }
  } finally {
    if (requestId === postRequestId) {
      loadingPosts.value = false
    }
  }
}

function applyPostFilter(): void {
  postQuery.page = pageFromLouInput(postQuery.louFrom) || 1
  void loadPosts()
}

function nextPage(): void {
  if (!posts.value || posts.value.page >= posts.value.totalPages) {
    return
  }
  postQuery.page = posts.value.page + 1
  void loadPosts()
}

function previousPage(): void {
  if (!posts.value || posts.value.page <= 1) {
    return
  }
  postQuery.page = posts.value.page - 1
  void loadPosts()
}

function goToPage(page: number): void {
  if (!posts.value) {
    return
  }
  const nextPage = Math.min(Math.max(Math.floor(page), 1), posts.value.totalPages)
  if (nextPage === posts.value.page) {
    pageJumpInput.value = String(posts.value.page)
    return
  }
  postQuery.page = nextPage
  void loadPosts()
}

function applyPageJump(): void {
  const page = integerFromParam(pageJumpInput.value)
  if (page === null) {
    pageJumpInput.value = posts.value ? String(posts.value.page) : ''
    return
  }
  goToPage(page)
}

watch(
  () => [
    selectedThread.value?.tid,
    selectedThread.value?.aidKey,
    postQuery.page,
    postQuery.q,
    postQuery.louFrom,
    postQuery.louTo,
    listFilter.q,
    listFilter.status,
    listFilter.sortBy,
    listFilter.sortDirection,
  ],
  syncUrl,
)

watch(
  () => posts.value?.page,
  (page) => {
    pageJumpInput.value = page === undefined ? '' : String(page)
  },
)

onMounted(() => {
  hydrateStateFromUrl()
  void loadThreads()
})
</script>

<template>
  <main class="app-shell">
    <aside class="thread-pane">
      <div class="pane-header">
        <h1>NGA 备份查看器</h1>
        <button type="button" class="icon-button" title="刷新列表" @click="refreshThreads">
          ↻
        </button>
      </div>

      <div class="filters">
        <input v-model="listFilter.q" type="search" placeholder="搜索标题、名称、作者或tid" />
        <select v-model="listFilter.status">
          <option value="all">全部状态</option>
          <option value="ready">可阅读</option>
          <option value="needs_migration">需迁移</option>
          <option value="missing_html">缺HTML</option>
          <option value="invalid">无效</option>
        </select>
        <select v-model="listFilter.sortBy" aria-label="排序字段">
          <option v-for="key in SORT_KEYS" :key="key" :value="key">
            按{{ sortLabels[key] }}
          </option>
        </select>
        <select v-model="listFilter.sortDirection" aria-label="排序方向">
          <option value="desc">降序</option>
          <option value="asc">升序</option>
        </select>
      </div>

      <div v-if="threadError" class="error-box">{{ threadError }}</div>
      <div v-else-if="loadingThreads" class="empty-state">正在读取备份列表...</div>
      <div v-else-if="visibleThreads.length === 0" class="empty-state">没有匹配的备份。</div>

      <div class="thread-list">
        <button
          v-for="thread in visibleThreads"
          :key="thread.dirName"
          type="button"
          class="thread-item"
          :class="{ selected: selectedThread?.dirName === thread.dirName }"
          @click="selectThread(thread)"
        >
          <span class="thread-title">{{ titleFor(thread) }}</span>
          <span class="thread-meta">
            <span class="status" :class="thread.status">{{ statusLabel(thread.status) }}</span>
            <span>{{ formatNumber(thread.postCount) }} 楼</span>
            <span v-if="thread.bodyWordCount !== null">
              {{ formatNumber(thread.bodyWordCount) }} 字
            </span>
            <span>{{ thread.author || thread.threadName || thread.dirName }}</span>
            <span>备份 {{ formatTime(thread.updatedAt) }}</span>
            <span v-if="thread.authorUpdatedAt !== null">
              作者最后发言 {{ formatTime(thread.authorUpdatedAt) }}
            </span>
          </span>
        </button>
      </div>
    </aside>

    <section class="reader-pane">
      <div v-if="selectedThread" class="reader-header">
        <div>
          <h2>{{ titleFor(selectedThread) }}</h2>
          <div class="reader-meta">
            <span>{{ selectedThread.dirName }}</span>
            <span>{{ formatNumber(selectedThread.postCount) }} 楼</span>
            <span v-if="selectedThread.bodyWordCount !== null">
              正文字数 {{ formatNumber(selectedThread.bodyWordCount) }}
            </span>
            <span>备份更新于 {{ formatTime(selectedThread.updatedAt) }}</span>
            <span v-if="selectedThread.authorUpdatedAt !== null">
              作者最后发言 {{ formatTime(selectedThread.authorUpdatedAt) }}
            </span>
            <a v-if="selectedThread.link" :href="selectedThread.link" target="_blank" rel="noreferrer">
              原帖
            </a>
          </div>
        </div>
        <span class="status large" :class="selectedThread.status">
          {{ statusLabel(selectedThread.status) }}
        </span>
      </div>

      <div v-if="!selectedThread" class="empty-state reader-empty">请选择一个备份。</div>

      <div v-else-if="selectedThread.status !== 'ready'" class="empty-state reader-empty">
        {{ selectedThread.message || '此备份暂不可阅读。' }}
      </div>

      <template v-else>
        <form class="post-toolbar" @submit.prevent="applyPostFilter">
          <input v-model="postQuery.q" type="search" placeholder="搜索正文" />
          <input v-model="postQuery.louFrom" type="number" min="0" placeholder="起始楼" />
          <input v-model="postQuery.louTo" type="number" min="0" placeholder="结束楼" />
          <button type="submit">筛选</button>
        </form>

        <div v-if="postError" class="error-box">{{ postError }}</div>
        <div v-else-if="loadingPosts" class="empty-state">正在读取楼层...</div>

        <div v-if="posts" class="post-count">
          <span>{{ pageStart }}-{{ pageEnd }}</span>
          <span>第 {{ posts.page }} / {{ posts.totalPages }} 页</span>
          <span>{{ posts.matchingPostCount }} / {{ posts.postCount }} 楼</span>
        </div>

        <div v-if="posts && posts.totalPages > 1" class="pager top-pager">
          <button type="button" :disabled="posts.page <= 1" @click="goToPage(1)">首页</button>
          <button type="button" :disabled="posts.page <= 1" @click="previousPage">上一页</button>
          <template v-for="token in pagerTokens" :key="token.key">
            <span v-if="token.type === 'ellipsis'" class="pager-ellipsis">...</span>
            <button
              v-else
              type="button"
              class="page-number"
              :class="{ active: token.page === posts.page }"
              :disabled="token.page === posts.page"
              @click="goToPage(token.page)"
            >
              {{ token.page }}
            </button>
          </template>
          <button
            type="button"
            :disabled="posts.page >= posts.totalPages"
            @click="nextPage"
          >
            下一页
          </button>
          <button
            type="button"
            :disabled="posts.page >= posts.totalPages"
            @click="goToPage(posts.totalPages)"
          >
            末页
          </button>
          <form class="page-jump" @submit.prevent="applyPageJump">
            <input
              v-model="pageJumpInput"
              type="number"
              min="1"
              :max="posts.totalPages"
              aria-label="跳转页码"
            />
            <button type="submit">跳转</button>
          </form>
        </div>

        <article
          v-for="post in posts?.slots || []"
          :key="post.lou"
          class="post-card"
          :class="{ empty: post.emptyReason !== null }"
        >
          <header>
            <strong>{{ post.floorLabel }}</strong>
            <span>{{ slotUserLabel(post) }}</span>
            <span>{{ formatTime(post.postdate) }}</span>
            <span v-if="post.hasOverlay" class="overlay-badge">已覆盖</span>
            <button
              v-if="post.pid !== null"
              type="button"
              class="overlay-edit-button"
              @click="openOverlayEditor(post)"
            >
              {{ post.hasOverlay ? '编辑覆盖' : '添加覆盖' }}
            </button>
          </header>
          <div class="post-content">
            <form
              v-if="overlayEditor.lou === post.lou"
              class="overlay-editor"
              @submit.prevent="saveOverlay"
            >
              <div class="overlay-editor-header">
                <strong>{{ overlayEditor.floorLabel || post.floorLabel }} BBCode overlay</strong>
                <button type="button" class="icon-button" title="关闭" @click="closeOverlayEditor">
                  ×
                </button>
              </div>
              <textarea
                v-model="overlayEditor.bbcode"
                :disabled="overlayEditor.loading || overlayEditor.saving"
                rows="8"
                spellcheck="false"
              ></textarea>
              <div class="overlay-editor-actions">
                <button
                  type="button"
                  :disabled="overlayEditor.loading || overlayEditor.previewing"
                  @click="previewOverlay"
                >
                  预览
                </button>
                <button
                  type="submit"
                  :disabled="overlayEditor.loading || overlayEditor.saving"
                >
                  保存
                </button>
                <button
                  v-if="post.hasOverlay"
                  type="button"
                  :disabled="overlayEditor.loading || overlayEditor.saving"
                  @click="clearOverlay"
                >
                  清除
                </button>
              </div>
              <div v-if="overlayEditor.error" class="error-box">{{ overlayEditor.error }}</div>
              <div v-else-if="overlayEditor.loading" class="empty-state">正在读取overlay...</div>
              <div
                v-if="overlayEditor.previewHtml"
                class="post-body overlay-preview"
                v-html="overlayEditor.previewHtml"
              ></div>
            </form>
            <div class="post-body" v-html="post.html"></div>
          </div>
        </article>

        <div v-if="posts && posts.totalPages > 1" class="pager">
          <button type="button" :disabled="posts.page <= 1" @click="goToPage(1)">首页</button>
          <button type="button" :disabled="posts.page <= 1" @click="previousPage">上一页</button>
          <template v-for="token in pagerTokens" :key="token.key">
            <span v-if="token.type === 'ellipsis'" class="pager-ellipsis">...</span>
            <button
              v-else
              type="button"
              class="page-number"
              :class="{ active: token.page === posts.page }"
              :disabled="token.page === posts.page"
              @click="goToPage(token.page)"
            >
              {{ token.page }}
            </button>
          </template>
          <button
            type="button"
            :disabled="posts.page >= posts.totalPages"
            @click="nextPage"
          >
            下一页
          </button>
          <button
            type="button"
            :disabled="posts.page >= posts.totalPages"
            @click="goToPage(posts.totalPages)"
          >
            末页
          </button>
          <form class="page-jump" @submit.prevent="applyPageJump">
            <input
              v-model="pageJumpInput"
              type="number"
              min="1"
              :max="posts.totalPages"
              aria-label="跳转页码"
            />
            <button type="submit">跳转</button>
          </form>
        </div>
      </template>
    </section>
  </main>
</template>
