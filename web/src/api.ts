import type { PostsResult, ThreadSummary } from './types'

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
  offset: number
  limit: number
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
  params.set('offset', String(query.offset))
  params.set('limit', String(query.limit))
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
