<script setup lang="ts">
import { computed, ref, watch } from 'vue'

type PageToken =
  | { type: 'page'; page: number; key: string }
  | { type: 'ellipsis'; key: string }

const props = withDefaults(
  defineProps<{
    currentPage: number
    totalPages: number
    disabled?: boolean
    top?: boolean
  }>(),
  {
    disabled: false,
    top: false,
  },
)

const emit = defineEmits<{
  change: [page: number]
}>()

const pageJumpInput = ref(String(props.currentPage))

const pagerTokens = computed<PageToken[]>(() => {
  if (props.totalPages <= 1) {
    return []
  }
  if (props.totalPages <= 9) {
    return Array.from({ length: props.totalPages }, (_item, index) => ({
      type: 'page',
      page: index + 1,
      key: `page-${index + 1}`,
    }))
  }

  const pages = new Set<number>([1, props.totalPages])
  for (let page = props.currentPage - 2; page <= props.currentPage + 2; page += 1) {
    if (page > 1 && page < props.totalPages) {
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

function goToPage(page: number): void {
  const nextPage = Math.min(Math.max(Math.floor(page), 1), props.totalPages)
  pageJumpInput.value = String(nextPage)
  if (nextPage !== props.currentPage) {
    emit('change', nextPage)
  }
}

function applyPageJump(): void {
  const value = pageJumpInput.value.trim()
  const page = Number(value)
  if (!value || !Number.isInteger(page)) {
    pageJumpInput.value = String(props.currentPage)
    return
  }
  goToPage(page)
}

watch(
  () => props.currentPage,
  (page) => {
    pageJumpInput.value = String(page)
  },
)
</script>

<template>
  <nav
    v-if="totalPages > 1"
    class="pager"
    :class="{ 'top-pager': top }"
    aria-label="分页"
  >
    <button type="button" :disabled="disabled || currentPage <= 1" @click="goToPage(1)">
      首页
    </button>
    <button
      type="button"
      :disabled="disabled || currentPage <= 1"
      @click="goToPage(currentPage - 1)"
    >
      上一页
    </button>
    <template v-for="token in pagerTokens" :key="token.key">
      <span v-if="token.type === 'ellipsis'" class="pager-ellipsis">...</span>
      <button
        v-else
        type="button"
        class="page-number"
        :class="{ active: token.page === currentPage }"
        :disabled="disabled || token.page === currentPage"
        @click="goToPage(token.page)"
      >
        {{ token.page }}
      </button>
    </template>
    <button
      type="button"
      :disabled="disabled || currentPage >= totalPages"
      @click="goToPage(currentPage + 1)"
    >
      下一页
    </button>
    <button
      type="button"
      :disabled="disabled || currentPage >= totalPages"
      @click="goToPage(totalPages)"
    >
      末页
    </button>
    <form class="page-jump" @submit.prevent="applyPageJump">
      <input
        v-model="pageJumpInput"
        type="number"
        min="1"
        :max="totalPages"
        inputmode="numeric"
        aria-label="跳转页码"
        :disabled="disabled"
      />
      <button type="submit" :disabled="disabled">跳转</button>
    </form>
  </nav>
</template>
