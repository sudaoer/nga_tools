<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  fetchImageUsageDetail,
  fetchImageUsageReplies,
} from '../api'
import type {
  ImageUsageDetailResult,
  ImageUsageRepliesResult,
  ImageUsageThreadGroup,
} from '../types'

const REPLY_PAGE_SIZE = 20

interface ThreadReplyState {
  open: boolean
  loading: boolean
  error: string | null
  result: ImageUsageRepliesResult | null
  nextOffset: number
}

const props = withDefaults(
  defineProps<{
    relativePath: string
    backLabel: string
    showRefresh?: boolean
    refreshing?: boolean
    reloadToken?: number
  }>(),
  {
    showRefresh: false,
    refreshing: false,
    reloadToken: 0,
  },
)

defineEmits<{
  back: []
  refresh: []
}>()

const detail = ref<ImageUsageDetailResult | null>(null)
const loadingDetail = ref(false)
const detailError = ref<string | null>(null)
const threadStates = ref<Record<string, ThreadReplyState>>({})

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

async function loadDetail(): Promise<void> {
  loadingDetail.value = true
  detail.value = null
  detailError.value = null
  threadStates.value = {}
  try {
    detail.value = await fetchImageUsageDetail(props.relativePath)
    threadStates.value = Object.fromEntries(
      detail.value.threads.map((group) => [
        String(group.tid),
        {
          open: false,
          loading: false,
          error: null,
          result: null,
          nextOffset: 0,
        },
      ]),
    )
  } catch (caught) {
    detailError.value = caughtMessage(caught)
  } finally {
    loadingDetail.value = false
  }
}

function stateFor(tid: number): ThreadReplyState | null {
  return threadStates.value[String(tid)] || null
}

async function toggleThread(group: ImageUsageThreadGroup): Promise<void> {
  const state = stateFor(group.tid)
  if (state === null) {
    return
  }
  state.open = !state.open
  if (state.open && state.result === null) {
    await loadMoreReplies(group.tid)
  }
}

async function loadMoreReplies(tid: number): Promise<void> {
  const state = stateFor(tid)
  if (state === null || state.loading) {
    return
  }
  state.loading = true
  state.error = null
  try {
    const payload = await fetchImageUsageReplies(
      props.relativePath,
      tid,
      state.nextOffset,
      REPLY_PAGE_SIZE,
    )
    const previousItems = state.result?.items || []
    state.result = {
      ...payload,
      items: [...previousItems, ...payload.items],
    }
    state.nextOffset = payload.offset + payload.limit
  } catch (caught) {
    state.error = caughtMessage(caught)
  } finally {
    state.loading = false
  }
}

function hasMoreReplies(tid: number): boolean {
  const state = stateFor(tid)
  return (
    state !== null &&
    state.result !== null &&
    state.nextOffset < state.result.total
  )
}

watch(
  () => [props.relativePath, props.reloadToken] as const,
  () => {
    void loadDetail()
  },
  { immediate: true },
)
</script>

<template>
  <section class="image-usage-detail-panel">
    <header class="image-detail-toolbar">
      <button type="button" @click="$emit('back')">{{ backLabel }}</button>
      <button
        v-if="showRefresh"
        type="button"
        :disabled="refreshing || loadingDetail"
        @click="$emit('refresh')"
      >
        重新统计
      </button>
    </header>
    <div v-if="detailError" class="error-box">{{ detailError }}</div>
    <div v-else-if="loadingDetail || detail === null" class="empty-state">
      正在读取图片引用详情...
    </div>
    <template v-else>
      <section class="image-detail-hero">
        <a :href="detail.item.fileUrl" target="_blank" rel="noreferrer">
          <img :src="detail.item.fileUrl" :alt="detail.item.relativePath" />
        </a>
        <div>
          <h1>图片引用详情</h1>
          <p class="image-detail-path">{{ detail.item.relativePath }}</p>
          <div class="image-detail-stats">
            <strong>{{ formatNumber(detail.item.usageCount) }} 次出现</strong>
            <span>{{ formatNumber(detail.item.threadCount) }} 个主题</span>
            <span>{{ formatNumber(detail.item.replyCount) }} 条回复</span>
          </div>
        </div>
      </section>

      <div v-if="detail.threads.length === 0" class="empty-state reader-empty">
        当前生效正文中没有回复引用这张图片。
      </div>
      <section v-else class="image-thread-groups">
        <article v-for="group in detail.threads" :key="group.tid" class="image-thread-group">
          <button type="button" class="image-thread-summary" @click="toggleThread(group)">
            <span class="image-thread-chevron">{{ stateFor(group.tid)?.open ? '▼' : '▶' }}</span>
            <strong>{{ group.title }}</strong>
            <span>tid {{ group.tid }}</span>
            <span>{{ formatNumber(group.replyCount) }} 条回复</span>
            <span>{{ formatNumber(group.usageCount) }} 次出现</span>
          </button>
          <div v-if="stateFor(group.tid)?.open" class="image-thread-replies">
            <div v-if="stateFor(group.tid)?.error" class="error-box">
              {{ stateFor(group.tid)?.error }}
            </div>
            <article
              v-for="reply in stateFor(group.tid)?.result?.items || []"
              :key="`${reply.dirName}:${reply.pid}`"
              class="image-reference-reply"
            >
              <header>
                <strong>{{ reply.floorLabel }}</strong>
                <span>{{ reply.authorName || '未知用户' }}</span>
                <span>{{ formatTime(reply.postdate) }}</span>
                <span class="image-reference-count">本回复出现 {{ reply.occurrenceCount }} 次</span>
                <a :href="reply.readerUrl">在阅读器查看</a>
              </header>
              <div class="post-body" v-html="reply.html"></div>
            </article>
            <div v-if="stateFor(group.tid)?.loading" class="empty-state">
              正在读取回复...
            </div>
            <button
              v-else-if="hasMoreReplies(group.tid)"
              type="button"
              class="image-replies-more"
              @click="loadMoreReplies(group.tid)"
            >
              加载更多回复
            </button>
          </div>
        </article>
      </section>
    </template>
  </section>
</template>
