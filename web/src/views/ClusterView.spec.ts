import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchClusterDetail,
  fetchClusters,
  fetchClusterStats,
  fetchImageUsageDetail,
  fetchImageUsageReplies,
} from '../api'
import type {
  ClusterDetailResult,
  ClustersResult,
  ClusterStatsResult,
  ImageUsageDetailResult,
} from '../types'
import ClusterView from './ClusterView.vue'

vi.mock('../api', () => ({
  fetchClusterDetail: vi.fn(),
  fetchClusters: vi.fn(),
  fetchClusterStats: vi.fn(),
  fetchImageUsageDetail: vi.fn(),
  fetchImageUsageReplies: vi.fn(),
}))

const selectedPath = 'images_unique/member-60.png'

const clusters: ClustersResult = {
  items: [
    {
      clusterId: 7,
      memberCount: 61,
      sourceRelativePath: 'images_unique/member-0.png',
      sourceFileUrl: '/api/files/images_unique/member-0.png',
    },
  ],
  total: 51,
  offset: 50,
  limit: 50,
  runId: 1,
}

const clusterDetail: ClusterDetailResult = {
  runId: 1,
  cluster: {
    clusterId: 7,
    members: Array.from({ length: 61 }, (_, index) => ({
      relativePath: `images_unique/member-${index}.png`,
      fileUrl: `/api/files/images_unique/member-${index}.png`,
      isSourceCandidate: index === 0,
    })),
  },
}

const clusterStats: ClusterStatsResult = {
  runId: 1,
  totalClusters: 1,
  totalImages: 61,
  maxClusterSize: 61,
}

const imageDetail: ImageUsageDetailResult = {
  item: {
    relativePath: selectedPath,
    fileUrl: '/api/files/images_unique/member-60.png',
    sourceUrl: 'https://img.example/member-60.png',
    mappingCount: 1,
    usageCount: 1,
    replyCount: 1,
    threadCount: 1,
  },
  threads: [],
}

async function mountView(search: string) {
  window.history.replaceState(null, '', `/admin/clusters${search}`)
  const wrapper = mount(ClusterView)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchClusters).mockResolvedValue(clusters)
  vi.mocked(fetchClusterDetail).mockResolvedValue(clusterDetail)
  vi.mocked(fetchClusterStats).mockResolvedValue(clusterStats)
  vi.mocked(fetchImageUsageDetail).mockResolvedValue(imageDetail)
  vi.mocked(fetchImageUsageReplies).mockRejectedValue(new Error('not used'))
})

afterEach(() => {
  window.history.replaceState(null, '', '/admin/clusters')
})

describe('ClusterView image usage detail', () => {
  it('opens image usage inline and returns to the same cluster member page', async () => {
    const wrapper = await mountView('?offset=50&cluster=7&moffset=60')

    expect(wrapper.get('.cluster-detail-path-link').text()).toBe('member-60.png')
    await wrapper.get('.cluster-detail-path-link').trigger('click')
    await flushPromises()

    const openedUrl = new URL(window.location.href)
    expect(openedUrl.pathname).toBe('/admin/clusters')
    expect(openedUrl.searchParams.get('offset')).toBe('50')
    expect(openedUrl.searchParams.get('cluster')).toBe('7')
    expect(openedUrl.searchParams.get('moffset')).toBe('60')
    expect(openedUrl.searchParams.get('image')).toBe(selectedPath)
    expect(wrapper.get('.image-detail-path').text()).toBe(selectedPath)

    await wrapper.get('.image-detail-toolbar button').trigger('click')
    await flushPromises()

    const returnedUrl = new URL(window.location.href)
    expect(returnedUrl.searchParams.get('image')).toBeNull()
    expect(returnedUrl.searchParams.get('cluster')).toBe('7')
    expect(returnedUrl.searchParams.get('moffset')).toBe('60')
    expect(wrapper.get('.cluster-detail-path-link').text()).toBe('member-60.png')
  })

  it('restores an inline image detail from the cluster URL', async () => {
    const wrapper = await mountView(
      `?offset=50&cluster=7&moffset=60&image=${encodeURIComponent(selectedPath)}`,
    )

    expect(fetchImageUsageDetail).toHaveBeenCalledWith(selectedPath)
    expect(wrapper.get('.image-detail-path').text()).toBe(selectedPath)

    await wrapper.get('.image-detail-toolbar button').trigger('click')
    await flushPromises()

    expect(wrapper.get('.cluster-detail-path-link').text()).toBe('member-60.png')
  })
})
