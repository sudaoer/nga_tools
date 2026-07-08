<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { fetchPosts, fetchThread, fetchThreads, type PostQuery } from './api'
import type { PostItem, PostsResult, ThreadStatus, ThreadSummary } from './types'

const threads = ref<ThreadSummary[]>([])
const selectedThread = ref<ThreadSummary | null>(null)
const posts = ref<PostsResult | null>(null)
const threadError = ref<string | null>(null)
const postError = ref<string | null>(null)
const loadingThreads = ref(false)
const loadingPosts = ref(false)

const listFilter = reactive({
  q: '',
  status: 'all' as ThreadStatus | 'all',
})

const postQuery = reactive<PostQuery>({
  page: 1,
  q: '',
  louFrom: '',
  louTo: '',
})

const filteredThreads = computed(() => {
  const keyword = listFilter.q.trim().toLowerCase()
  return threads.value.filter((thread) => {
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
})

const pageStart = computed(() => (posts.value ? posts.value.pageStartLou : 0))
const pageEnd = computed(() => (posts.value ? posts.value.pageEndLou : 0))

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

async function loadThreads(): Promise<void> {
  loadingThreads.value = true
  threadError.value = null
  try {
    threads.value = await fetchThreads()
    const firstReady = threads.value.find((thread) => thread.status === 'ready')
    if (firstReady && selectedThread.value === null) {
      await selectThread(firstReady)
    }
  } catch (error) {
    threadError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingThreads.value = false
  }
}

async function selectThread(thread: ThreadSummary): Promise<void> {
  selectedThread.value = thread
  posts.value = null
  postError.value = null
  postQuery.page = 1
  if (thread.status !== 'ready') {
    return
  }
  try {
    selectedThread.value = await fetchThread(thread.tid, thread.aidKey)
  } catch (error) {
    postError.value = error instanceof Error ? error.message : String(error)
    return
  }
  await loadPosts()
}

async function loadPosts(): Promise<void> {
  if (!selectedThread.value || selectedThread.value.status !== 'ready') {
    return
  }
  loadingPosts.value = true
  postError.value = null
  try {
    posts.value = await fetchPosts(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      postQuery,
    )
  } catch (error) {
    postError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingPosts.value = false
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

onMounted(() => {
  void loadThreads()
})
</script>

<template>
  <main class="app-shell">
    <aside class="thread-pane">
      <div class="pane-header">
        <h1>NGA 备份查看器</h1>
        <button type="button" class="icon-button" title="刷新列表" @click="loadThreads">
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
      </div>

      <div v-if="threadError" class="error-box">{{ threadError }}</div>
      <div v-else-if="loadingThreads" class="empty-state">正在读取备份列表...</div>
      <div v-else-if="filteredThreads.length === 0" class="empty-state">没有匹配的备份。</div>

      <div class="thread-list">
        <button
          v-for="thread in filteredThreads"
          :key="thread.dirName"
          type="button"
          class="thread-item"
          :class="{ selected: selectedThread?.dirName === thread.dirName }"
          @click="selectThread(thread)"
        >
          <span class="thread-title">{{ titleFor(thread) }}</span>
          <span class="thread-meta">
            <span class="status" :class="thread.status">{{ statusLabel(thread.status) }}</span>
            <span>{{ thread.postCount }} 楼</span>
            <span>{{ thread.author || thread.threadName || thread.dirName }}</span>
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
            <span>{{ selectedThread.postCount }} 楼</span>
            <span>更新 {{ formatTime(selectedThread.updatedAt) }}</span>
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
          </header>
          <div class="post-body" v-html="post.html"></div>
        </article>

        <div v-if="posts && posts.totalPages > 1" class="pager">
          <button type="button" :disabled="posts.page <= 1" @click="previousPage">上一页</button>
          <span>{{ posts.page }} / {{ posts.totalPages }}</span>
          <button
            type="button"
            :disabled="posts.page >= posts.totalPages"
            @click="nextPage"
          >
            下一页
          </button>
        </div>
      </template>
    </section>
  </main>
</template>
