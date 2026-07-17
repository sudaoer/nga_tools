<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import PaginationControls from '../components/PaginationControls.vue'
import {
  fetchClusterDetail,
  fetchClusters,
  fetchClusterStats,
} from '../api'
import type {
  ClusterDetailResult,
  ClustersResult,
  ClusterStatsResult,
} from '../types'

const LIST_PAGE_SIZE = 50
const MEMBER_PAGE_SIZE = 60

const result = ref<ClustersResult | null>(null)
const stats = ref<ClusterStatsResult | null>(null)
const detail = ref<ClusterDetailResult | null>(null)
const loading = ref(false)
const loadingDetail = ref(false)
const error = ref<string | null>(null)
const detailError = ref<string | null>(null)
const offset = ref(0)
const memberOffset = ref(0)
const selectedClusterId = ref<number | null>(null)

const listCurrentPage = computed(
  () => Math.floor(offset.value / LIST_PAGE_SIZE) + 1,
)
const listTotalPages = computed(() => {
  if (result.value === null) {
    return 1
  }
  return Math.max(1, Math.ceil(result.value.total / LIST_PAGE_SIZE))
})

const detailMembers = computed(() =>
  detail.value?.cluster?.members ?? [],
)
const memberCurrentPage = computed(
  () => Math.floor(memberOffset.value / MEMBER_PAGE_SIZE) + 1,
)
const memberTotalPages = computed(() => {
  const total = detailMembers.value.length
  return Math.max(1, Math.ceil(total / MEMBER_PAGE_SIZE))
})
const memberPage = computed(() =>
  detailMembers.value.slice(
    memberOffset.value,
    memberOffset.value + MEMBER_PAGE_SIZE,
  ),
)

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
  const cluster = params.get('cluster')
  selectedClusterId.value = cluster ? Number(cluster) : null
  const memberOffsetParam = integerFromParam(params.get('moffset'))
  memberOffset.value = memberOffsetParam === null ? 0 : memberOffsetParam
}

function syncUrl(): void {
  const url = new URL(window.location.href)
  if (offset.value > 0) {
    url.searchParams.set('offset', String(offset.value))
  } else {
    url.searchParams.delete('offset')
  }
  if (selectedClusterId.value !== null) {
    url.searchParams.set('cluster', String(selectedClusterId.value))
  } else {
    url.searchParams.delete('cluster')
  }
  if (memberOffset.value > 0) {
    url.searchParams.set('moffset', String(memberOffset.value))
  } else {
    url.searchParams.delete('moffset')
  }
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
}

function formatNumber(value: number): string {
  return value.toLocaleString()
}

async function loadStats(): Promise<void> {
  try {
    stats.value = await fetchClusterStats()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function loadClusters(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    result.value = await fetchClusters({
      offset: offset.value,
      limit: LIST_PAGE_SIZE,
      minSize: 2,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function loadDetail(): Promise<void> {
  if (selectedClusterId.value === null) {
    detail.value = null
    return
  }
  loadingDetail.value = true
  detailError.value = null
  try {
    detail.value = await fetchClusterDetail(selectedClusterId.value)
    memberOffset.value = 0
  } catch (err) {
    detailError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loadingDetail.value = false
  }
}

function goToListPage(page: number): void {
  offset.value = (page - 1) * LIST_PAGE_SIZE
}

function goToMemberPage(page: number): void {
  memberOffset.value = (page - 1) * MEMBER_PAGE_SIZE
  syncUrl()
  const el = document.querySelector('.cluster-view')
  if (el) {
    el.scrollTo({ top: 0, behavior: 'smooth' })
  }
}

function openCluster(clusterId: number): void {
  selectedClusterId.value = clusterId
  detail.value = null
  memberOffset.value = 0
  syncUrl()
  void loadDetail()
}

function closeDetail(): void {
  selectedClusterId.value = null
  detail.value = null
  memberOffset.value = 0
  syncUrl()
}

watch(offset, () => {
  syncUrl()
  void loadClusters()
})

onMounted(() => {
  hydrateStateFromUrl()
  void loadStats()
  void loadClusters()
  if (selectedClusterId.value !== null) {
    void loadDetail()
  }
})
</script>

<template>
  <main class="image-usage-shell cluster-shell">
    <header class="image-usage-header cluster-header">
      <div>
        <h1>图片聚类</h1>
        <div v-if="stats" class="cluster-stats">
          <span>聚类数量：<strong>{{ formatNumber(stats.totalClusters) }}</strong></span>
          <span>聚类图片：<strong>{{ formatNumber(stats.totalImages) }}</strong></span>
          <span>最大簇：<strong>{{ formatNumber(stats.maxClusterSize) }}</strong></span>
        </div>
      </div>
    </header>

    <div v-if="error" class="error-box">{{ error }}</div>

    <template v-if="selectedClusterId !== null">
      <header class="image-detail-toolbar">
        <button type="button" @click="closeDetail">← 返回聚类列表</button>
      </header>
      <div v-if="detailError" class="error-box">{{ detailError }}</div>
      <div v-else-if="loadingDetail || detail === null" class="empty-state">
        正在读取聚类详情...
      </div>
      <template v-else-if="detail.cluster !== null">
        <h2 class="cluster-detail-title">
          聚类 #{{ detail.cluster.clusterId }}（{{ formatNumber(detail.cluster.members.length) }} 张）
        </h2>

        <PaginationControls
          :current-page="memberCurrentPage"
          :total-pages="memberTotalPages"
          :disabled="loadingDetail"
          top
          @change="goToMemberPage"
        />
        <div class="image-usage-grid cluster-detail-grid">
          <div
            v-for="member in memberPage"
            :key="member.relativePath"
            class="image-usage-card cluster-detail-card"
            :class="{ 'is-source': member.isSourceCandidate }"
          >
            <a class="image-usage-card-preview" :href="member.fileUrl" target="_blank" rel="noreferrer">
              <img :src="member.fileUrl" :alt="member.relativePath" loading="lazy" />
            </a>
            <div class="image-usage-card-stats cluster-detail-card-meta">
              <span
                v-if="member.isSourceCandidate"
                class="cluster-source-badge"
              >源图</span>
              <strong class="cluster-detail-path" :title="member.relativePath">
                {{ member.relativePath }}
              </strong>
            </div>
          </div>
        </div>
        <PaginationControls
          :current-page="memberCurrentPage"
          :total-pages="memberTotalPages"
          :disabled="loadingDetail"
          @change="goToMemberPage"
        />
      </template>
      <div v-else class="empty-state">未找到该聚类。</div>
    </template>

    <template v-else>
      <PaginationControls
        :current-page="listCurrentPage"
        :total-pages="listTotalPages"
        :disabled="loading"
        top
        @change="goToListPage"
      />

      <div v-if="result === null && loading" class="empty-state">正在加载聚类...</div>
      <div v-else-if="result !== null && result.items.length === 0" class="empty-state">
        暂无聚类结果。请先运行 <code>python main.py cluster run</code>。
      </div>
      <div v-else-if="result !== null" class="image-usage-grid">
        <button
          v-for="item in result.items"
          :key="item.clusterId"
          type="button"
          class="image-usage-card"
          :title="item.sourceRelativePath"
          @click="openCluster(item.clusterId)"
        >
          <span class="image-usage-card-preview">
            <img :src="item.sourceFileUrl" :alt="item.sourceRelativePath" loading="lazy" />
          </span>
          <span class="image-usage-card-stats">
            <strong>聚类 #{{ item.clusterId }}</strong>
            <span>{{ formatNumber(item.memberCount) }} 张</span>
          </span>
        </button>
      </div>

      <PaginationControls
        :current-page="listCurrentPage"
        :total-pages="listTotalPages"
        :disabled="loading"
        @change="goToListPage"
      />
      <div v-if="loading" class="image-usage-loading">正在更新...</div>
    </template>
  </main>
</template>
