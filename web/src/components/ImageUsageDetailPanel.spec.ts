import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchImageUsageDetail,
  fetchImageUsageReplies,
} from '../api'
import type {
  ImageUsageDetailResult,
  ImageUsageRepliesResult,
} from '../types'
import ImageUsageDetailPanel from './ImageUsageDetailPanel.vue'

vi.mock('../api', () => ({
  fetchImageUsageDetail: vi.fn(),
  fetchImageUsageReplies: vi.fn(),
}))

const relativePath = 'images_unique/sample.png'

const detail: ImageUsageDetailResult = {
  item: {
    relativePath,
    fileUrl: '/api/files/images_unique/sample.png',
    sourceUrl: 'https://img.example/sample.png',
    mappingCount: 1,
    usageCount: 2,
    replyCount: 2,
    threadCount: 1,
  },
  threads: [
    {
      tid: 123,
      title: '测试主题',
      usageCount: 2,
      replyCount: 2,
    },
  ],
}

function replies(offset: number, pid: number): ImageUsageRepliesResult {
  return {
    items: [
      {
        tid: 123,
        aidKey: '456',
        dirName: '123_456',
        pid,
        lou: offset + 1,
        floorLabel: `${offset + 1}楼`,
        authorName: '测试用户',
        postdate: 1_700_000_000,
        occurrenceCount: 1,
        html: `<p>回复 ${pid}</p>`,
        readerUrl: '/threads?tid=123&aid=456&page=1',
      },
    ],
    total: 2,
    offset,
    limit: 1,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchImageUsageDetail).mockResolvedValue(detail)
  vi.mocked(fetchImageUsageReplies)
    .mockResolvedValueOnce(replies(0, 1001))
    .mockResolvedValueOnce(replies(1, 1002))
})

describe('ImageUsageDetailPanel', () => {
  it('loads the selected image and emits toolbar actions', async () => {
    const wrapper = mount(ImageUsageDetailPanel, {
      props: {
        relativePath,
        backLabel: '← 返回当前聚类',
        showRefresh: true,
      },
    })
    await flushPromises()

    expect(fetchImageUsageDetail).toHaveBeenCalledWith(relativePath)
    expect(wrapper.get('.image-detail-path').text()).toBe(relativePath)
    expect(wrapper.text()).toContain('2 次出现')

    const toolbarButtons = wrapper.findAll('.image-detail-toolbar button')
    await toolbarButtons[0].trigger('click')
    await toolbarButtons[1].trigger('click')

    expect(wrapper.emitted('back')).toEqual([[]])
    expect(wrapper.emitted('refresh')).toEqual([[]])
  })

  it('expands a thread and paginates its replies', async () => {
    const wrapper = mount(ImageUsageDetailPanel, {
      props: {
        relativePath,
        backLabel: '← 返回',
      },
    })
    await flushPromises()

    await wrapper.get('.image-thread-summary').trigger('click')
    await flushPromises()

    expect(fetchImageUsageReplies).toHaveBeenNthCalledWith(
      1,
      relativePath,
      123,
      0,
      20,
    )
    expect(wrapper.text()).toContain('回复 1001')

    await wrapper.get('.image-replies-more').trigger('click')
    await flushPromises()

    expect(fetchImageUsageReplies).toHaveBeenNthCalledWith(
      2,
      relativePath,
      123,
      1,
      20,
    )
    expect(wrapper.text()).toContain('回复 1002')
  })

  it('keeps the back action available when detail loading fails', async () => {
    vi.mocked(fetchImageUsageDetail).mockRejectedValueOnce(new Error('未知图片。'))
    const wrapper = mount(ImageUsageDetailPanel, {
      props: {
        relativePath,
        backLabel: '← 返回当前聚类',
      },
    })
    await flushPromises()

    expect(wrapper.get('.error-box').text()).toBe('未知图片。')
    await wrapper.get('.image-detail-toolbar button').trigger('click')
    expect(wrapper.emitted('back')).toEqual([[]])
  })
})
