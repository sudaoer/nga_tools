import { nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchDatabaseSchema,
  fetchDatabases,
  fetchTableRowDetail,
  fetchTableRows,
} from '../api'
import type { DatabaseSchema, DatabaseSummary, TableRows } from '../types'
import DatabaseView from './DatabaseView.vue'

vi.mock('../api', () => ({
  fetchDatabaseSchema: vi.fn(),
  fetchDatabases: vi.fn(),
  fetchTableRowDetail: vi.fn(),
  fetchTableRows: vi.fn(),
}))

const databases: DatabaseSummary[] = [
  {
    id: 'archive:z',
    kind: 'archive',
    label: 'z / archive.sqlite3',
    relativePath: 'z/archive.sqlite3',
    status: 'ready',
    message: null,
    sizeBytes: 100,
    updatedAt: '2026-07-03T00:00:00+00:00',
    tableCount: 2,
  },
  {
    id: 'archive:a',
    kind: 'archive',
    label: 'a / archive.sqlite3',
    relativePath: 'a/archive.sqlite3',
    status: 'ready',
    message: null,
    sizeBytes: 100,
    updatedAt: '2026-07-02T00:00:00+00:00',
    tableCount: 5,
  },
  {
    id: 'archive:m',
    kind: 'archive',
    label: 'm / archive.sqlite3',
    relativePath: 'm/archive.sqlite3',
    status: 'ready',
    message: null,
    sizeBytes: 20,
    updatedAt: '2026-07-04T00:00:00+00:00',
    tableCount: 1,
  },
]

const tableRows: TableRows = {
  columns: [],
  rows: [],
  total: 0,
  offset: 0,
  limit: 50,
  query: '',
  sortBy: null,
  sortDirection: 'asc',
}

let wrapper: ReturnType<typeof mount> | null = null

function databasePaths(): string[] {
  return wrapper!.findAll('button.database-item').map((item) => item.get('.database-path').text())
}

async function mountViewer(search = ''): Promise<void> {
  window.history.replaceState(null, '', `/admin/databases${search}`)
  wrapper = mount(DatabaseView)
  await flushPromises()
  await nextTick()
}

beforeEach(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  })
  vi.mocked(fetchDatabases).mockResolvedValue(databases)
  vi.mocked(fetchDatabaseSchema).mockImplementation(
    async (databaseId): Promise<DatabaseSchema> => ({
      database: databases.find((database) => database.id === databaseId) || databases[0],
      tables: [
        {
          name: 'sample',
          type: 'table',
          rowCount: 0,
          columns: [],
        },
      ],
    }),
  )
  vi.mocked(fetchTableRows).mockResolvedValue(tableRows)
  vi.mocked(fetchTableRowDetail).mockRejectedValue(new Error('not used'))
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/admin/databases')
})

describe('DatabaseView database sorting', () => {
  it('keeps the API order by default and sorts by path in both directions', async () => {
    await mountViewer()

    expect(databasePaths()).toEqual([
      'z/archive.sqlite3',
      'a/archive.sqlite3',
      'm/archive.sqlite3',
    ])

    const sortBy = wrapper!.get<HTMLSelectElement>('select[aria-label="数据库排序字段"]')
    const sortDirection = wrapper!.get<HTMLSelectElement>('select[aria-label="数据库排序方向"]')
    await sortBy.setValue('relativePath')

    expect(databasePaths()).toEqual([
      'a/archive.sqlite3',
      'm/archive.sqlite3',
      'z/archive.sqlite3',
    ])

    await sortDirection.setValue('desc')
    expect(databasePaths()).toEqual([
      'z/archive.sqlite3',
      'm/archive.sqlite3',
      'a/archive.sqlite3',
    ])
  })

  it('sorts numeric and timestamp fields with a stable path tie-breaker', async () => {
    await mountViewer()

    const sortBy = wrapper!.get<HTMLSelectElement>('select[aria-label="数据库排序字段"]')
    const sortDirection = wrapper!.get<HTMLSelectElement>('select[aria-label="数据库排序方向"]')

    await sortBy.setValue('sizeBytes')
    expect(databasePaths()).toEqual([
      'm/archive.sqlite3',
      'a/archive.sqlite3',
      'z/archive.sqlite3',
    ])

    await sortDirection.setValue('desc')
    expect(databasePaths()).toEqual([
      'a/archive.sqlite3',
      'z/archive.sqlite3',
      'm/archive.sqlite3',
    ])

    await sortBy.setValue('updatedAt')
    expect(databasePaths()).toEqual([
      'm/archive.sqlite3',
      'z/archive.sqlite3',
      'a/archive.sqlite3',
    ])

    await sortDirection.setValue('asc')
    await sortBy.setValue('tableCount')
    expect(databasePaths()).toEqual([
      'm/archive.sqlite3',
      'z/archive.sqlite3',
      'a/archive.sqlite3',
    ])
  })

  it('hydrates and writes database sorting in the URL', async () => {
    await mountViewer('?db_sort=sizeBytes&db_sort_direction=desc')

    const sortBy = wrapper!.get<HTMLSelectElement>('select[aria-label="数据库排序字段"]')
    const sortDirection = wrapper!.get<HTMLSelectElement>('select[aria-label="数据库排序方向"]')
    expect(sortBy.element.value).toBe('sizeBytes')
    expect(sortDirection.element.value).toBe('desc')
    expect(databasePaths()).toEqual([
      'a/archive.sqlite3',
      'z/archive.sqlite3',
      'm/archive.sqlite3',
    ])

    await sortBy.setValue('relativePath')
    await nextTick()
    const params = new URLSearchParams(window.location.search)
    expect(params.get('db_sort')).toBe('relativePath')
    expect(params.get('db_sort_direction')).toBe('desc')
  })
})
