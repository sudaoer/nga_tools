import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import type { SplitpanesResizedPayload } from 'splitpanes'

export interface SidebarPaneDefinition {
  id: string
  label: string
  controlsId: string
  defaultSize: number
  minPixels: number
  maxSize: number
  mobileSize: number
}

export interface CollapsedPaneItem {
  id: string
  label: string
  controlsId: string
}

interface PaneState {
  size: number
  collapsed: boolean
}

interface StoredPaneLayout {
  version: 1
  panes: Record<string, PaneState>
}

interface PersistentPaneLayoutOptions {
  storageKey: string
  panes: SidebarPaneDefinition[]
  mainMinPixels: number
  mainMobileSize: number
}

interface DesktopMetrics {
  mainMinSize: number
  minSizes: Record<string, number>
  maxSizes: Record<string, number>
}

const STORAGE_VERSION = 1
const NARROW_VIEWPORT_QUERY = '(max-width: 860px)'
const SIZE_EPSILON = 0.001

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function usePersistentPaneLayout(options: PersistentPaneLayoutOptions) {
  const paneStates = reactive<Record<string, PaneState>>(
    Object.fromEntries(
      options.panes.map((pane) => [
        pane.id,
        {
          size: pane.defaultSize,
          collapsed: false,
        },
      ]),
    ),
  )
  const containerRef = ref<HTMLElement | null>(null)
  const containerWidth = ref(0)
  const isNarrow = ref(
    typeof window !== 'undefined' && window.matchMedia(NARROW_VIEWPORT_QUERY).matches,
  )
  let resizeObserver: ResizeObserver | null = null
  let viewportQuery: MediaQueryList | null = null

  function readStoredLayout(): void {
    if (typeof window === 'undefined') {
      return
    }
    try {
      const rawValue = window.localStorage.getItem(options.storageKey)
      if (rawValue === null) {
        return
      }
      const parsed: unknown = JSON.parse(rawValue)
      if (!isRecord(parsed) || parsed.version !== STORAGE_VERSION || !isRecord(parsed.panes)) {
        return
      }
      for (const definition of options.panes) {
        const storedPane = parsed.panes[definition.id]
        if (!isRecord(storedPane)) {
          continue
        }
        const storedSize = finiteNumber(storedPane.size)
        if (storedSize !== null) {
          paneStates[definition.id].size = clamp(storedSize, 0, definition.maxSize)
        }
        if (typeof storedPane.collapsed === 'boolean') {
          paneStates[definition.id].collapsed = storedPane.collapsed
        }
      }
    } catch {
      // localStorage may be unavailable or contain malformed data. Defaults remain usable.
    }
  }

  function persistLayout(): void {
    if (typeof window === 'undefined') {
      return
    }
    const stored: StoredPaneLayout = {
      version: STORAGE_VERSION,
      panes: Object.fromEntries(
        options.panes.map((pane) => [
          pane.id,
          {
            size: paneStates[pane.id].size,
            collapsed: paneStates[pane.id].collapsed,
          },
        ]),
      ),
    }
    try {
      window.localStorage.setItem(options.storageKey, JSON.stringify(stored))
    } catch {
      // Keep the in-memory layout working when storage is blocked or full.
    }
  }

  readStoredLayout()

  const visiblePanes = computed(() =>
    options.panes.filter((pane) => !paneStates[pane.id].collapsed),
  )

  const collapsedPanes = computed<CollapsedPaneItem[]>(() =>
    options.panes
      .filter((pane) => paneStates[pane.id].collapsed)
      .map(({ id, label, controlsId }) => ({ id, label, controlsId })),
  )

  const desktopMetrics = computed<DesktopMetrics>(() => {
    const visible = visiblePanes.value
    const width = containerWidth.value
    const minSizes: Record<string, number> = {}
    const maxSizes: Record<string, number> = {}

    for (const pane of visible) {
      const pixelMinimum = width > 0 ? (pane.minPixels / width) * 100 : 0
      minSizes[pane.id] = clamp(pixelMinimum, 0, pane.maxSize)
      maxSizes[pane.id] = pane.maxSize
    }

    let mainMinSize = width > 0 ? (options.mainMinPixels / width) * 100 : 0
    mainMinSize = clamp(mainMinSize, 0, 100)

    const totalMinimum =
      mainMinSize + visible.reduce((total, pane) => total + minSizes[pane.id], 0)
    if (totalMinimum > 100) {
      const scale = 100 / totalMinimum
      mainMinSize *= scale
      for (const pane of visible) {
        minSizes[pane.id] *= scale
        maxSizes[pane.id] = Math.max(minSizes[pane.id], maxSizes[pane.id])
      }
    }

    return { mainMinSize, minSizes, maxSizes }
  })

  const desktopPaneSizes = computed<Record<string, number>>(() => {
    const visible = visiblePanes.value
    const metrics = desktopMetrics.value
    const sizes: Record<string, number> = {}

    for (const pane of visible) {
      sizes[pane.id] = clamp(
        paneStates[pane.id].size,
        metrics.minSizes[pane.id],
        metrics.maxSizes[pane.id],
      )
    }

    const budget = 100 - metrics.mainMinSize
    const total = visible.reduce((sum, pane) => sum + sizes[pane.id], 0)
    const overflow = total - budget
    if (overflow > SIZE_EPSILON) {
      const capacity = visible.reduce(
        (sum, pane) => sum + Math.max(0, sizes[pane.id] - metrics.minSizes[pane.id]),
        0,
      )
      if (capacity > SIZE_EPSILON) {
        for (const pane of visible) {
          const paneCapacity = Math.max(0, sizes[pane.id] - metrics.minSizes[pane.id])
          sizes[pane.id] -= overflow * (paneCapacity / capacity)
        }
      }
    }

    return sizes
  })

  const mobileTotalSize = computed(
    () =>
      options.mainMobileSize +
      visiblePanes.value.reduce((total, pane) => total + pane.mobileSize, 0),
  )

  function paneSize(id: string): number {
    const definition = options.panes.find((pane) => pane.id === id)
    if (definition === undefined) {
      return 0
    }
    if (isNarrow.value) {
      return (definition.mobileSize / mobileTotalSize.value) * 100
    }
    return desktopPaneSizes.value[id] ?? 0
  }

  function paneMinSize(id: string): number {
    return isNarrow.value ? 0 : (desktopMetrics.value.minSizes[id] ?? 0)
  }

  function paneMaxSize(id: string): number {
    return isNarrow.value ? 100 : (desktopMetrics.value.maxSizes[id] ?? 100)
  }

  const mainSize = computed(() => {
    if (isNarrow.value) {
      return (options.mainMobileSize / mobileTotalSize.value) * 100
    }
    const sidebarTotal = visiblePanes.value.reduce(
      (total, pane) => total + (desktopPaneSizes.value[pane.id] ?? 0),
      0,
    )
    return Math.max(0, 100 - sidebarTotal)
  })

  const mainMinSize = computed(() =>
    isNarrow.value ? 0 : desktopMetrics.value.mainMinSize,
  )

  function isCollapsed(id: string): boolean {
    return paneStates[id]?.collapsed ?? false
  }

  function collapsePane(id: string): void {
    if (paneStates[id] === undefined || paneStates[id].collapsed) {
      return
    }
    paneStates[id].collapsed = true
    persistLayout()
  }

  function restorePane(id: string): void {
    if (paneStates[id] === undefined || !paneStates[id].collapsed) {
      return
    }
    paneStates[id].collapsed = false
    persistLayout()
  }

  function onResized(payload: SplitpanesResizedPayload): void {
    if (isNarrow.value || payload.event === undefined) {
      return
    }
    const visible = visiblePanes.value
    if (payload.panes.length !== visible.length + 1) {
      return
    }
    visible.forEach((pane, index) => {
      paneStates[pane.id].size = clamp(payload.panes[index].size, 0, pane.maxSize)
    })
    persistLayout()
  }

  function updateContainerWidth(): void {
    containerWidth.value = containerRef.value?.clientWidth ?? 0
  }

  function updateNarrowViewport(event: MediaQueryListEvent): void {
    isNarrow.value = event.matches
  }

  onMounted(() => {
    updateContainerWidth()
    if (typeof ResizeObserver !== 'undefined' && containerRef.value !== null) {
      resizeObserver = new ResizeObserver(updateContainerWidth)
      resizeObserver.observe(containerRef.value)
    }
    viewportQuery = window.matchMedia(NARROW_VIEWPORT_QUERY)
    isNarrow.value = viewportQuery.matches
    viewportQuery.addEventListener('change', updateNarrowViewport)
  })

  onBeforeUnmount(() => {
    resizeObserver?.disconnect()
    viewportQuery?.removeEventListener('change', updateNarrowViewport)
  })

  return {
    collapsedPanes,
    collapsePane,
    containerRef,
    isCollapsed,
    isNarrow,
    mainMinSize,
    mainSize,
    onResized,
    paneMaxSize,
    paneMinSize,
    paneSize,
    restorePane,
  }
}
