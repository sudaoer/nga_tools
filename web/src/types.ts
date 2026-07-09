export type ThreadStatus = 'ready' | 'needs_migration' | 'missing_html' | 'invalid'

export interface ThreadSummary {
  tid: number
  aid: number | null
  aidKey: string
  dirName: string
  status: ThreadStatus
  message: string | null
  threadName: string | null
  subject: string | null
  author: string | null
  link: string | null
  replies: number | null
  postdate: number | null
  lastpost: number | null
  postCount: number
  bodyWordCount: number | null
  bodyChineseCharCount: number | null
  bodyWordPostCount: number | null
  minLou: number | null
  maxLou: number | null
  pageCount: number
  updatedAt: string | null
  authorUpdatedAt: number | string | null
  hasHtmlModified: boolean
  hasFloorMap: boolean
  hasWarnings: boolean
}

export interface PostItem {
  lou: number
  pid: number | null
  authorName: string | null
  authorUid: number | null
  postdate: number | string | null
  floorLabel: string
  html: string
  emptyReason: 'missing' | 'filtered' | null
}

export interface PostsResult {
  slots: PostItem[]
  items: PostItem[]
  total: number
  offset: number
  limit: number
  page: number
  pageSize: number
  pageStartLou: number
  pageEndLou: number
  totalPages: number
  postCount: number
  matchingPostCount: number
  maxLou: number | null
}
