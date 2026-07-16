import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PaginationControls from './PaginationControls.vue'

function mountPager(
  props: Partial<{
    currentPage: number
    totalPages: number
    disabled: boolean
    top: boolean
  }> = {},
) {
  return mount(PaginationControls, {
    props: {
      currentPage: 2,
      totalPages: 5,
      ...props,
    },
  })
}

describe('PaginationControls', () => {
  it('emits the selected page from a page button', async () => {
    const wrapper = mountPager()
    const pageButtons = wrapper.findAll('button.page-number')

    await pageButtons[2].trigger('click')

    expect(wrapper.emitted('change')).toEqual([[3]])
  })

  it('submits a valid page number', async () => {
    const wrapper = mountPager()
    const input = wrapper.get<HTMLInputElement>('input[aria-label="跳转页码"]')

    await input.setValue('4')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('change')).toEqual([[4]])
  })

  it('handles out-of-range values in the submit handler', async () => {
    const wrapper = mountPager()
    const form = wrapper.get('form')
    const input = wrapper.get<HTMLInputElement>('input[aria-label="跳转页码"]')

    expect(form.attributes('novalidate')).toBeDefined()

    await input.setValue('99')
    await form.trigger('submit')

    expect(wrapper.emitted('change')).toEqual([[5]])
    expect(input.element.value).toBe('5')
  })

  it('restores the current page for empty or non-integer input', async () => {
    const wrapper = mountPager()
    const input = wrapper.get<HTMLInputElement>('input[aria-label="跳转页码"]')
    const form = wrapper.get('form')

    await input.setValue('')
    await form.trigger('submit')
    expect(input.element.value).toBe('2')

    await input.setValue('2.5')
    await form.trigger('submit')
    expect(input.element.value).toBe('2')
    expect(wrapper.emitted('change')).toBeUndefined()
  })

  it('disables all pagination controls while loading', () => {
    const wrapper = mountPager({ disabled: true })

    expect(wrapper.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(
      true,
    )
    expect(wrapper.get('input').attributes('disabled')).toBeDefined()
  })
})
