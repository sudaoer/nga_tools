<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  fetchDatabaseSchema,
  fetchDatabases,
  fetchTableRowDetail,
  fetchTableRows,
} from '../api'
import type {
  ColumnInfo,
  DatabaseSchema,
  DatabaseStatus,
  DatabaseSummary,
  DbCell,
  SortDirection,
  TableRow,
  TableRows,
  TableSummary,
} from '../types'

const databases = ref<DatabaseSummary[]>([])
const schema = ref<DatabaseSchema | null>(null)
const rows = ref<TableRows | null>(null)
const selectedDatabaseId = ref<string | null>(null)
const selectedTableName = ref<string | null>(null)
const selectedRow = ref<TableRow | null>(null)
const databaseError = ref<string | null>(null)
const schemaError = ref<string | null>(null)
const rowError = ref<string | null>(null)
const detailError = ref<string | null>(null)
const loadingDatabases = ref(false)
const loadingSchema = ref(false)
const loadingRows = ref(false)
const loadingDetail = ref(false)

const rowQuery = reactive({
  q: '',
  offset: 0,
  limit: 50,
  sortBy: null as string | null,
  sortDirection: 'asc' as SortDirection,
})

const selectedDatabase = computed(
  () => databases.value.find((database) => database.id === selectedDatabaseId.value) || null,
)

const selectedTable = computed(
  () => schema.value?.tables.find((table) => table.name === selectedTableName.value) || null,
)

const currentPage = computed(() => {
  if (!rows.value) {
    return 1
  }
  return Math.floor(rows.value.offset / rows.value.limit) + 1
})

const totalPages = computed(() => {
  if (!rows.value) {
    return 1
  }
  return Math.max(1, Math.ceil(rows.value.total / rows.value.limit))
})

const rowStart = computed(() => {
  if (!rows.value || rows.value.total === 0) {
    return 0
  }
  return rows.value.offset + 1
})

const rowEnd = computed(() => {
  if (!rows.value) {
    return 0
  }
  return Math.min(rows.value.total, rows.value.offset + rows.value.rows.length)
})

function statusLabel(status: DatabaseStatus): string {
  return status === 'ready' ? '可读取' : '无效'
}

function tableTypeLabel(type: string): string {
  return type === 'view' ? '视图' : '表'
}

function formatNumber(value: number | null): string {
  return value === null ? '-' : value.toLocaleString()
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value || '-'
  }
  return date.toLocaleString()
}

function columnLabel(column: ColumnInfo): string {
  const marks: string[] = []
  if (column.primaryKey) {
    marks.push('PK')
  }
  if (column.notNull) {
    marks.push('NN')
  }
  return marks.length ? `${column.name} ${marks.join('/')}` : column.name
}

function nullCell(): DbCell {
  return { kind: 'null', value: null, truncated: false }
}

function cellFor(row: TableRow, columnName: string): DbCell {
  return row.cells[columnName] || nullCell()
}

function cellText(cell: DbCell): string {
  if (cell.kind === 'null') {
    return 'NULL'
  }
  if (cell.kind === 'blob') {
    const value = cell.value === null ? '' : String(cell.value)
    return `0x${value}${cell.truncated ? '...' : ''}`
  }
  const value = cell.value === null ? '' : String(cell.value)
  return `${value}${cell.truncated ? '...' : ''}`
}

function cellTitle(cell: DbCell): string {
  if (cell.kind === 'blob') {
    return cell.truncated ? 'BLOB 十六进制预览，已截断' : 'BLOB 十六进制'
  }
  if (cell.truncated) {
    return '列表预览已截断，点击行查看详情'
  }
  return cellText(cell)
}

function sortMarker(columnName: string): string {
  if (rowQuery.sortBy !== columnName) {
    return ''
  }
  return rowQuery.sortDirection === 'asc' ? ' ↑' : ' ↓'
}

function resetRowQuery(): void {
  rowQuery.q = ''
  rowQuery.offset = 0
  rowQuery.limit = 50
  rowQuery.sortBy = null
  rowQuery.sortDirection = 'asc'
}

async function loadDatabases(): Promise<void> {
  loadingDatabases.value = true
  databaseError.value = null
  try {
    databases.value = await fetchDatabases()
    const current =
      selectedDatabaseId.value === null
        ? null
        : databases.value.find((database) => database.id === selectedDatabaseId.value) || null
    const target =
      current ||
      databases.value.find((database) => database.status === 'ready') ||
      databases.value[0] ||
      null
    if (target === null) {
      selectedDatabaseId.value = null
      schema.value = null
      rows.value = null
      return
    }
    await selectDatabase(target)
  } catch (error) {
    databaseError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingDatabases.value = false
  }
}

async function selectDatabase(database: DatabaseSummary): Promise<void> {
  selectedDatabaseId.value = database.id
  selectedTableName.value = null
  schema.value = null
  rows.value = null
  selectedRow.value = null
  schemaError.value = null
  rowError.value = null
  detailError.value = null
  resetRowQuery()
  if (database.status !== 'ready') {
    return
  }
  loadingSchema.value = true
  try {
    schema.value = await fetchDatabaseSchema(database.id)
    const firstTable = schema.value.tables[0] || null
    if (firstTable !== null) {
      await selectTable(firstTable)
    }
  } catch (error) {
    schemaError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingSchema.value = false
  }
}

async function selectTable(table: TableSummary): Promise<void> {
  selectedTableName.value = table.name
  rows.value = null
  selectedRow.value = null
  rowError.value = null
  detailError.value = null
  resetRowQuery()
  await loadRows()
}

async function loadRows(resetOffset = false): Promise<void> {
  if (selectedDatabaseId.value === null || selectedTableName.value === null) {
    return
  }
  if (resetOffset) {
    rowQuery.offset = 0
  }
  loadingRows.value = true
  rowError.value = null
  selectedRow.value = null
  detailError.value = null
  try {
    rows.value = await fetchTableRows(selectedDatabaseId.value, selectedTableName.value, rowQuery)
  } catch (error) {
    rowError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingRows.value = false
  }
}

function applyFilter(): void {
  void loadRows(true)
}

function setSort(column: ColumnInfo): void {
  if (rowQuery.sortBy === column.name) {
    rowQuery.sortDirection = rowQuery.sortDirection === 'asc' ? 'desc' : 'asc'
  } else {
    rowQuery.sortBy = column.name
    rowQuery.sortDirection = 'asc'
  }
  void loadRows(true)
}

function previousPage(): void {
  if (!rows.value || rows.value.offset <= 0) {
    return
  }
  rowQuery.offset = Math.max(0, rows.value.offset - rows.value.limit)
  void loadRows()
}

function nextPage(): void {
  if (!rows.value || rows.value.offset + rows.value.limit >= rows.value.total) {
    return
  }
  rowQuery.offset = rows.value.offset + rows.value.limit
  void loadRows()
}

function changeLimit(): void {
  void loadRows(true)
}

async function openRow(row: TableRow): Promise<void> {
  if (row.rowId === null || selectedDatabaseId.value === null || selectedTableName.value === null) {
    detailError.value = '此表不支持rowid详情。'
    selectedRow.value = null
    return
  }
  loadingDetail.value = true
  detailError.value = null
  try {
    selectedRow.value = (
      await fetchTableRowDetail(selectedDatabaseId.value, selectedTableName.value, row.rowId)
    ).row
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : String(error)
    selectedRow.value = null
  } finally {
    loadingDetail.value = false
  }
}

onMounted(() => {
  void loadDatabases()
})
</script>

<template>
  <main class="database-shell">
    <aside class="database-sidebar">
      <div class="pane-header">
        <h1>数据库查看器</h1>
        <button type="button" class="icon-button" title="刷新数据库列表" @click="loadDatabases">
          ↻
        </button>
      </div>

      <div v-if="databaseError" class="error-box">{{ databaseError }}</div>
      <div v-else-if="loadingDatabases" class="empty-state">正在读取数据库列表...</div>
      <div v-else-if="databases.length === 0" class="empty-state">未找到项目内 SQLite 数据库。</div>

      <div class="database-list">
        <button
          v-for="database in databases"
          :key="database.id"
          type="button"
          class="database-item"
          :class="{ selected: selectedDatabaseId === database.id, invalid: database.status !== 'ready' }"
          @click="selectDatabase(database)"
        >
          <span class="database-title">{{ database.label }}</span>
          <span class="thread-meta">
            <span class="status" :class="database.status">{{ statusLabel(database.status) }}</span>
            <span>{{ database.tableCount }} 表</span>
            <span>{{ formatBytes(database.sizeBytes) }}</span>
          </span>
          <span class="database-path">{{ database.relativePath }}</span>
        </button>
      </div>
    </aside>

    <aside class="table-pane">
      <div class="pane-header table-header">
        <h1>表</h1>
      </div>

      <div v-if="selectedDatabase?.status === 'invalid'" class="error-box">
        {{ selectedDatabase.message || '数据库无法读取。' }}
      </div>
      <div v-else-if="schemaError" class="error-box">{{ schemaError }}</div>
      <div v-else-if="loadingSchema" class="empty-state">正在读取表结构...</div>
      <div v-else-if="schema && schema.tables.length === 0" class="empty-state">没有可浏览的表。</div>

      <div v-if="schema" class="table-list">
        <button
          v-for="table in schema.tables"
          :key="table.name"
          type="button"
          class="table-item"
          :class="{ selected: selectedTableName === table.name }"
          @click="selectTable(table)"
        >
          <span>{{ table.name }}</span>
          <span class="thread-meta">
            <span>{{ tableTypeLabel(table.type) }}</span>
            <span>{{ formatNumber(table.rowCount) }} 行</span>
            <span>{{ table.columns.length }} 列</span>
          </span>
        </button>
      </div>
    </aside>

    <section class="database-content">
      <div v-if="selectedDatabase" class="reader-header">
        <div>
          <h2>{{ selectedDatabase.label }}</h2>
          <div class="reader-meta">
            <span>{{ selectedDatabase.relativePath }}</span>
            <span>{{ formatBytes(selectedDatabase.sizeBytes) }}</span>
            <span>更新于 {{ formatTime(selectedDatabase.updatedAt) }}</span>
          </div>
        </div>
        <span class="status large" :class="selectedDatabase.status">
          {{ statusLabel(selectedDatabase.status) }}
        </span>
      </div>

      <div v-if="!selectedDatabase" class="empty-state reader-empty">请选择一个数据库。</div>

      <template v-else-if="selectedDatabase.status === 'ready'">
        <form class="db-toolbar" @submit.prevent="applyFilter">
          <input v-model="rowQuery.q" type="search" placeholder="搜索当前表" />
          <select v-model.number="rowQuery.limit" aria-label="每页行数" @change="changeLimit">
            <option :value="25">25 行</option>
            <option :value="50">50 行</option>
            <option :value="100">100 行</option>
            <option :value="200">200 行</option>
          </select>
          <button type="submit">筛选</button>
        </form>

        <div v-if="selectedTable" class="schema-strip">
          <strong>{{ selectedTable.name }}</strong>
          <span>{{ tableTypeLabel(selectedTable.type) }}</span>
          <span>{{ formatNumber(selectedTable.rowCount) }} 行</span>
          <span>{{ selectedTable.columns.length }} 列</span>
        </div>

        <div v-if="rowError" class="error-box">{{ rowError }}</div>
        <div v-else-if="loadingRows" class="empty-state">正在读取数据行...</div>
        <div v-else-if="rows && rows.rows.length === 0" class="empty-state reader-empty">
          当前筛选没有数据。
        </div>

        <div v-if="rows" class="post-count">
          <span>{{ rowStart }}-{{ rowEnd }} / {{ formatNumber(rows.total) }}</span>
          <span>第 {{ currentPage }} / {{ totalPages }} 页</span>
        </div>

        <div v-if="rows" class="database-table-wrap">
          <table class="database-table">
            <thead>
              <tr>
                <th scope="col">rowid</th>
                <th
                  v-for="column in rows.columns"
                  :key="column.name"
                  scope="col"
                  class="sortable"
                  @click="setSort(column)"
                >
                  {{ columnLabel(column) }}{{ sortMarker(column.name) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(row, index) in rows.rows"
                :key="row.rowId ?? `row-${index}`"
                :class="{ selectable: row.rowId !== null }"
                @click="openRow(row)"
              >
                <td>{{ row.rowId ?? '-' }}</td>
                <td
                  v-for="column in rows.columns"
                  :key="column.name"
                  :class="['cell', cellFor(row, column.name).kind]"
                  :title="cellTitle(cellFor(row, column.name))"
                >
                  {{ cellText(cellFor(row, column.name)) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="rows && rows.total > rows.limit" class="pager">
          <button type="button" :disabled="rows.offset <= 0" @click="previousPage">上一页</button>
          <span>第 {{ currentPage }} / {{ totalPages }} 页</span>
          <button
            type="button"
            :disabled="rows.offset + rows.limit >= rows.total"
            @click="nextPage"
          >
            下一页
          </button>
        </div>

        <div v-if="detailError" class="error-box">{{ detailError }}</div>
        <div v-else-if="loadingDetail" class="empty-state reader-empty">正在读取行详情...</div>
        <section v-if="selectedRow && rows" class="row-detail">
          <div class="row-detail-header">
            <h2>rowid {{ selectedRow.rowId }}</h2>
            <button type="button" class="icon-button" title="关闭详情" @click="selectedRow = null">
              ×
            </button>
          </div>
          <dl>
            <template v-for="column in rows.columns" :key="column.name">
              <dt>{{ column.name }}</dt>
              <dd :class="cellFor(selectedRow, column.name).kind">
                {{ cellText(cellFor(selectedRow, column.name)) }}
              </dd>
            </template>
          </dl>
        </section>
      </template>
    </section>
  </main>
</template>
