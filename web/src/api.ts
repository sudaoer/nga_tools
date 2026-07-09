import type {
  DatabaseSchema,
  DatabaseSummary,
  PostVersionGroup,
  PostVersionPreview,
  PostVersionSelectionResult,
  PostVersionThreadSummary,
  PostsResult,
  SortDirection,
  TableRowDetail,
  TableRows,
  ThreadSummary,
} from './types'

async function readJson<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const payload = (await response.json()) as { error?: string }
      if (payload.error) {
        message = payload.error
      }
    } catch {
      // Keep the HTTP status message.
    }
    throw new Error(message)
  }
  return (await response.json()) as T
}

export async function fetchThreads(): Promise<ThreadSummary[]> {
  const payload = await readJson<{ items: ThreadSummary[] }>('/api/threads')
  return payload.items
}

export async function fetchThread(
  tid: number,
  aidKey: string,
): Promise<ThreadSummary> {
  return readJson<ThreadSummary>(`/api/threads/${tid}/${encodeURIComponent(aidKey)}`)
}

export interface PostQuery {
  page: number
  q: string
  louFrom: string
  louTo: string
}

export async function fetchPosts(
  tid: number,
  aidKey: string,
  query: PostQuery,
): Promise<PostsResult> {
  const params = new URLSearchParams()
  params.set('page', String(query.page))
  if (query.q.trim()) {
    params.set('q', query.q.trim())
  }
  if (query.louFrom.trim()) {
    params.set('lou_from', query.louFrom.trim())
  }
  if (query.louTo.trim()) {
    params.set('lou_to', query.louTo.trim())
  }
  return readJson<PostsResult>(
    `/api/threads/${tid}/${encodeURIComponent(aidKey)}/posts?${params.toString()}`,
  )
}

export async function fetchPostVersionGroups(
  tid: number,
  aidKey: string,
): Promise<PostVersionGroup[]> {
  const payload = await readJson<{ items: PostVersionGroup[] }>(
    `/api/admin/threads/${tid}/${encodeURIComponent(aidKey)}/post-versions`,
  )
  return payload.items
}

export async function fetchPostVersionThreads(
  options: { multiVersionOnly: boolean } = { multiVersionOnly: false },
): Promise<PostVersionThreadSummary[]> {
  const params = new URLSearchParams()
  if (options.multiVersionOnly) {
    params.set('multi_version_only', 'true')
  }
  const query = params.toString()
  const payload = await readJson<{ items: PostVersionThreadSummary[] }>(
    `/api/admin/post-version-threads${query ? `?${query}` : ''}`,
  )
  return payload.items
}

export async function fetchPostVersionPreview(
  tid: number,
  aidKey: string,
  versionId: number,
): Promise<PostVersionPreview> {
  return readJson<PostVersionPreview>(
    `/api/admin/threads/${tid}/${encodeURIComponent(
      aidKey,
    )}/post-versions/${versionId}/preview`,
  )
}

export async function selectPostVersion(
  tid: number,
  aidKey: string,
  lou: number,
  versionId: number,
): Promise<PostVersionSelectionResult> {
  const response = await fetch(
    `/api/admin/threads/${tid}/${encodeURIComponent(
      aidKey,
    )}/post-version-selections/${lou}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ versionId }),
    },
  )
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const payload = (await response.json()) as { error?: string }
      if (payload.error) {
        message = payload.error
      }
    } catch {
      // Keep the HTTP status message.
    }
    throw new Error(message)
  }
  return (await response.json()) as PostVersionSelectionResult
}

export async function clearPostVersionSelection(
  tid: number,
  aidKey: string,
  lou: number,
): Promise<PostVersionSelectionResult> {
  const response = await fetch(
    `/api/admin/threads/${tid}/${encodeURIComponent(
      aidKey,
    )}/post-version-selections/${lou}`,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const payload = (await response.json()) as { error?: string }
      if (payload.error) {
        message = payload.error
      }
    } catch {
      // Keep the HTTP status message.
    }
    throw new Error(message)
  }
  return (await response.json()) as PostVersionSelectionResult
}

export async function fetchDatabases(): Promise<DatabaseSummary[]> {
  const payload = await readJson<{ items: DatabaseSummary[] }>('/api/databases')
  return payload.items
}

export async function fetchDatabaseSchema(dbId: string): Promise<DatabaseSchema> {
  return readJson<DatabaseSchema>(`/api/databases/${encodeURIComponent(dbId)}/schema`)
}

export interface TableRowsQuery {
  offset: number
  limit: number
  q: string
  sortBy: string | null
  sortDirection: SortDirection
}

export async function fetchTableRows(
  dbId: string,
  tableName: string,
  query: TableRowsQuery,
): Promise<TableRows> {
  const params = new URLSearchParams()
  params.set('offset', String(query.offset))
  params.set('limit', String(query.limit))
  if (query.q.trim()) {
    params.set('q', query.q.trim())
  }
  if (query.sortBy !== null) {
    params.set('sort_by', query.sortBy)
    params.set('sort_direction', query.sortDirection)
  }
  return readJson<TableRows>(
    `/api/databases/${encodeURIComponent(dbId)}/tables/${encodeURIComponent(
      tableName,
    )}/rows?${params.toString()}`,
  )
}

export async function fetchTableRowDetail(
  dbId: string,
  tableName: string,
  rowId: number,
): Promise<TableRowDetail> {
  return readJson<TableRowDetail>(
    `/api/databases/${encodeURIComponent(dbId)}/tables/${encodeURIComponent(
      tableName,
    )}/rows/${rowId}`,
  )
}
