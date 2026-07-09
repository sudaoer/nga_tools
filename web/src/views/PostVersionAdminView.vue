<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  clearPostVersionSelection,
  fetchPostVersionGroups,
  fetchPostVersionPreview,
  fetchPostVersionThreads,
  selectPostVersion,
} from '../api'
import type {
  PostItem,
  PostVersionGroup,
  PostVersionOption,
  PostVersionThreadSummary,
} from '../types'

const threads = ref<PostVersionThreadSummary[]>([])
const selectedThread = ref<PostVersionThreadSummary | null>(null)
const groups = ref<PostVersionGroup[]>([])
const selectedLou = ref<number | null>(null)
const preview = ref<PostItem | null>(null)
const previewVersionId = ref<number | null>(null)
const threadError = ref<string | null>(null)
const versionError = ref<string | null>(null)
const actionError = ref<string | null>(null)
const loadingThreads = ref(false)
const loadingGroups = ref(false)
const loadingPreview = ref(false)
const savingVersionId = ref<number | null>(null)
const showOnlyMultiVersionThreads = ref(true)

const selectedGroup = computed(
  () => groups.value.find((group) => group.lou === selectedLou.value) || null,
)

const visibleThreads = computed(() =>
  threads.value.filter(
    (thread) => !showOnlyMultiVersionThreads.value || thread.multiVersionFloorCount > 0,
  ),
)

function titleFor(thread: PostVersionThreadSummary): string {
  return thread.subject || thread.threadName || thread.dirName
}

function dateFromValue(value: string | number): Date | null {
  if (typeof value === 'number') {
    return new Date(value * 1000)
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
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

function formatHash(value: string): string {
  return value.slice(0, 12)
}

function versionLabel(option: PostVersionOption): string {
  if (option.isLatest) {
    return '最新版'
  }
  if (option.isSelected) {
    return '已选'
  }
  return '历史版'
}

function clearThreadSelection(): void {
  selectedThread.value = null
  groups.value = []
  selectedLou.value = null
  preview.value = null
  previewVersionId.value = null
}

async function selectVisibleThread(): Promise<void> {
  const current =
    selectedThread.value === null
      ? null
      : visibleThreads.value.find(
          (thread) =>
            thread.tid === selectedThread.value?.tid &&
            thread.aidKey === selectedThread.value?.aidKey,
        ) || null
  const target = current || visibleThreads.value[0] || null
  if (target !== null) {
    await selectThread(target)
  } else {
    clearThreadSelection()
  }
}

async function loadThreads(): Promise<void> {
  loadingThreads.value = true
  threadError.value = null
  try {
    threads.value = await fetchPostVersionThreads({
      multiVersionOnly: showOnlyMultiVersionThreads.value,
    })
    await selectVisibleThread()
  } catch (error) {
    threadError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingThreads.value = false
  }
}

async function selectThread(thread: PostVersionThreadSummary): Promise<void> {
  selectedThread.value = thread
  groups.value = []
  selectedLou.value = null
  preview.value = null
  previewVersionId.value = null
  await loadGroups()
}

async function loadGroups(): Promise<void> {
  if (selectedThread.value === null) {
    return
  }
  loadingGroups.value = true
  versionError.value = null
  actionError.value = null
  try {
    groups.value = await fetchPostVersionGroups(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
    )
    const currentGroup =
      selectedLou.value === null
        ? null
        : groups.value.find((group) => group.lou === selectedLou.value) || null
    const target = currentGroup || groups.value[0] || null
    selectedLou.value = target?.lou ?? null
    if (target !== null) {
      await previewVersion(target.activeVersionId)
    } else {
      preview.value = null
      previewVersionId.value = null
    }
  } catch (error) {
    versionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingGroups.value = false
  }
}

async function selectGroup(group: PostVersionGroup): Promise<void> {
  selectedLou.value = group.lou
  await previewVersion(group.activeVersionId)
}

async function previewVersion(versionId: number): Promise<void> {
  if (selectedThread.value === null) {
    return
  }
  loadingPreview.value = true
  actionError.value = null
  try {
    preview.value = (
      await fetchPostVersionPreview(
        selectedThread.value.tid,
        selectedThread.value.aidKey,
        versionId,
      )
    ).item
    previewVersionId.value = versionId
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
    preview.value = null
  } finally {
    loadingPreview.value = false
  }
}

async function applyVersion(option: PostVersionOption): Promise<void> {
  if (selectedThread.value === null || selectedGroup.value === null || !option.selectable) {
    return
  }
  savingVersionId.value = option.id
  actionError.value = null
  try {
    await selectPostVersion(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      selectedGroup.value.lou,
      option.id,
    )
    await loadGroups()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    savingVersionId.value = null
  }
}

async function clearSelection(): Promise<void> {
  if (selectedThread.value === null || selectedGroup.value === null) {
    return
  }
  savingVersionId.value = selectedGroup.value.activeVersionId
  actionError.value = null
  try {
    await clearPostVersionSelection(
      selectedThread.value.tid,
      selectedThread.value.aidKey,
      selectedGroup.value.lou,
    )
    await loadGroups()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    savingVersionId.value = null
  }
}

watch(showOnlyMultiVersionThreads, () => {
  void loadThreads()
})

onMounted(() => {
  void loadThreads()
})
</script>

<template>
  <section class="version-admin-shell">
    <aside class="thread-pane">
      <div class="pane-header">
        <h1>备份</h1>
        <button type="button" class="icon-button" title="刷新列表" @click="loadThreads">
          ↻
        </button>
      </div>

      <label class="version-thread-filter">
        <input v-model="showOnlyMultiVersionThreads" type="checkbox" />
        <span>只看多版本楼层</span>
      </label>

      <div v-if="threadError" class="error-box">{{ threadError }}</div>
      <div v-else-if="loadingThreads" class="empty-state">正在读取备份列表...</div>
      <div v-else-if="visibleThreads.length === 0" class="empty-state">
        {{ showOnlyMultiVersionThreads ? '没有多版本楼层的备份。' : '没有可管理的备份。' }}
      </div>

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
            <span>{{ thread.dirName }}</span>
            <span>{{ thread.postCount }} 楼</span>
            <span>多版本 {{ thread.multiVersionFloorCount }} 楼</span>
            <span>备份 {{ formatTime(thread.updatedAt) }}</span>
          </span>
        </button>
      </div>
    </aside>

    <aside class="version-list-pane">
      <div class="pane-header">
        <h1>正文版本</h1>
      </div>

      <div v-if="versionError" class="error-box">{{ versionError }}</div>
      <div v-else-if="loadingGroups" class="empty-state">正在读取版本...</div>
      <div v-else-if="groups.length === 0" class="empty-state">没有多版本楼层。</div>

      <div class="version-floor-list">
        <button
          v-for="group in groups"
          :key="group.lou"
          type="button"
          class="version-floor-item"
          :class="{ selected: selectedLou === group.lou }"
          @click="selectGroup(group)"
        >
          <span>{{ group.floorLabel }}</span>
          <span class="thread-meta">
            <span>{{ group.versions.length }} 个版本</span>
            <span v-if="group.selectedVersionId !== null">已覆盖</span>
            <span v-else>自动最新</span>
          </span>
        </button>
      </div>
    </aside>

    <section class="version-detail-pane">
      <div v-if="selectedGroup" class="reader-header">
        <div>
          <h2>{{ selectedGroup.floorLabel }}</h2>
          <div class="reader-meta">
            <span>当前版本 {{ selectedGroup.activeVersionId }}</span>
            <span>最新版 {{ selectedGroup.latestVersionId }}</span>
          </div>
        </div>
        <button
          type="button"
          :disabled="selectedGroup.selectedVersionId === null || savingVersionId !== null"
          @click="clearSelection"
        >
          清除
        </button>
      </div>

      <div v-if="!selectedGroup" class="empty-state reader-empty">请选择一个楼层。</div>

      <template v-else>
        <div class="version-options">
          <article
            v-for="option in selectedGroup.versions"
            :key="option.id"
            class="version-option"
            :class="{
              selected: option.id === selectedGroup.activeVersionId,
              previewed: option.id === previewVersionId,
            }"
          >
            <header>
              <strong>{{ versionLabel(option) }}</strong>
              <span>#{{ option.id }}</span>
              <span>{{ formatTime(option.lastSeenAt) }}</span>
              <span>{{ option.seenCount }} 次</span>
              <span>{{ formatHash(option.sourceHash) }}</span>
            </header>
            <p>{{ option.contentPreview }}</p>
            <div class="version-actions">
              <button type="button" @click="previewVersion(option.id)">预览</button>
              <button
                type="button"
                :disabled="!option.selectable || option.isSelected || savingVersionId !== null"
                @click="applyVersion(option)"
              >
                选择
              </button>
            </div>
          </article>
        </div>

        <div v-if="actionError" class="error-box">{{ actionError }}</div>
        <div v-else-if="loadingPreview" class="empty-state reader-empty">正在读取预览...</div>

        <article v-if="preview" class="post-card version-preview-card">
          <header>
            <strong>{{ preview.floorLabel }}</strong>
            <span>{{ preview.authorName || '未知用户' }}</span>
            <span>{{ formatTime(preview.postdate) }}</span>
            <span v-if="preview.manualVersion">手动版本</span>
          </header>
          <div class="post-body" v-html="preview.html"></div>
        </article>
      </template>
    </section>
  </section>
</template>
