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

const PAGE_SIZE = 50

const result = ref<ClustersResult | null>(null)
const stats = ref<ClusterStatsResult | null>(null)
const detail = ref<ClusterDetailResult | null>(null)
const loading = ref(false)
const loadingDetail = ref(false)
const error = ref<string | null>(null)
const detailError = ref<string | null>(null)
const offset = ref(0)
const selectedClusterId = ref<number | null>(null)

const currentPage = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
const totalPages = computed(() => {
  if (result.value === null) {
    return 1
  }
  return Math.max(1, Math.ceil(result.value.total / PAGE_SIZE))
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
  const cluster = params.get('cluster')
  selectedClusterId.value = cluster ? Number(cluster) : null
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
      limit: PAGE_SIZE,
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
  } catch (err) {
    detailError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loadingDetail.value = false
  }
}

function goToPage(page: number): void {
  offset.value = (page - 1) * PAGE_SIZE
}

function openCluster(clusterId: number): void {
  selectedClusterId.value = clusterId
  detail.value = null
  syncUrl()
  void loadDetail()
}

function closeDetail(): void {
  selectedClusterId.value = null
  detail.value = null
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
  <div class="cluster-view">
    <header class="cluster-header">
      <h1>图片聚类</h1>
      <div v-if="stats" class="cluster-stats">
        <span>聚类数量：<strong>{{ formatNumber(stats.totalClusters) }}</strong></span>
        <span>聚类图片：<strong>{{ formatNumber(stats.totalImages) }}</strong></span>
        <span>最大簇：<strong>{{ formatNumber(stats.maxClusterSize) }}</strong></span>
      </div>
    </header>

    <div v-if="error" class="error-box">{{ error }}</div>

    <template v-if="selectedClusterId !== null">
      <header class="cluster-detail-toolbar">
        <button type="button" @click="closeDetail">← 返回聚类列表</button>
      </header>
      <div v-if="detailError" class="error-box">{{ detailError }}</div>
      <div v-else-if="loadingDetail || detail === null" class="empty-state">
        正在读取聚类详情...
      </div>
      <template v-else-if="detail.cluster !== null">
        <h2 class="cluster-detail-title">
          聚类 #{{ detail.cluster.clusterId }}（{{ detail.cluster.members.length }} 张）
        </h2>
        <div class="cluster-detail-grid">
          <div
            v-for="member in detail.cluster.members"
            :key="member.relativePath"
            class="cluster-detail-card"
            :class="{ 'is-source': member.isSourceCandidate }"
          >
            <a :href="member.fileUrl" target="_blank" rel="noreferrer">
              <img :src="member.fileUrl" :alt="member.relativePath" loading="lazy" />
            </a>
            <div class="cluster-detail-card-meta">
              <span
                v-if="member.isSourceCandidate"
                class="cluster-source-badge"
              >建议源图</span>
              <span class="cluster-detail-path" :title="member.relativePath">
                {{ member.relativePath }}
              </span>
            </div>
          </div>
        </div>
      </template>
      <div v-else class="empty-state">未找到该聚类。</div>
    </template>

    <template v-else>
      <PaginationControls
        :current-page="currentPage"
        :total-pages="totalPages"
        :disabled="loading"
        top
        @change="goToPage"
      />

      <div v-if="result === null && loading" class="empty-state">正在加载聚类...</div>
      <div v-else-if="result !== null && result.items.length === 0" class="empty-state">
        暂无聚类结果。请先运行 <code>python main.py cluster run</code>。
      </div>
      <div v-else-if="result !== null" class="cluster-list-grid">
        <button
          v-for="item in result.items"
          :key="item.clusterId"
          type="button"
          class="cluster-list-card"
          :title="item.sourceRelativePath"
          @click="openCluster(item.clusterId)"
        >
          <span class="cluster-list-card-preview">
            <img :src="item.sourceFileUrl" :alt="item.sourceRelativePath" loading="lazy" />
          </span>
          <span class="cluster-list-card-stats">
            <strong>聚类 #{{ item.clusterId }}</strong>
            <span>{{ formatNumber(item.memberCount) }} 张</span>
          </span>
        </button>
      </div>

      <PaginationControls
        :current-page="currentPage"
        :total-pages="totalPages"
        :disabled="loading"
        @change="goToPage"
      />
    </template>
  </div>
</template>
