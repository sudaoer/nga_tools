export type ThreadStatus = 'ready' | 'needs_migration' | 'invalid'

export interface ThreadSummary {
  tid: number
  aid: number | null
  aidKey: string
  dirName: string
  status: ThreadStatus
  message: string | null
  statsLoaded: boolean
  threadName: string | null
  subject: string | null
  author: string | null
  link: string | null
  replies: number | null
  postdate: number | null
  lastpost: number | null
  postCount: number | null
  bodyWordCount: number | null
  bodyChineseCharCount: number | null
  bodyWordPostCount: number | null
  minLou: number | null
  maxLou: number | null
  pageCount: number | null
  updatedAt: string | null
  authorUpdatedAt: number | string | null
  hasWarnings: boolean
}

export interface PostVersionThreadSummary extends ThreadSummary {
  multiVersionFloorCount: number
}

export interface PostItem {
  lou: number
  pid: number | null
  versionId: number | null
  manualVersion: boolean
  authorName: string | null
  authorUid: number | null
  postdate: number | string | null
  floorLabel: string
  html: string
  emptyReason: 'missing' | 'filtered' | null
  hasOverlay: boolean
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

export interface PostVersionOption {
  id: number
  sourceHash: string
  firstSeenAt: string
  lastSeenAt: string
  seenCount: number
  isLatest: boolean
  isSelected: boolean
  selectable: boolean
  content: string
  contentPreview: string
}

export interface PostVersionGroup {
  lou: number
  floorLabel: string
  latestVersionId: number
  selectedVersionId: number | null
  activeVersionId: number
  versions: PostVersionOption[]
}

export interface PostVersionPreview {
  item: PostItem
}

export interface PostVersionSelectionResult {
  lou: number
  selectedVersionId: number | null
  activeVersionId: number
}

export interface PostOverlayDetail {
  lou: number
  floorLabel: string
  hasOverlay: boolean
  bbcode: string
  html: string | null
}

export interface PostOverlayPreview {
  html: string
}

export interface ImageUsageItem {
  relativePath: string
  fileUrl: string
  sourceUrl: string
  mappingCount: number
  usageCount: number
  replyCount: number
  threadCount: number
}

export type ImageUsageSort = 'usage' | 'threads'

export interface SkippedImageUsageArchive {
  dirName: string
  message: string
}

export interface ImageUsageResult {
  items: ImageUsageItem[]
  total: number
  offset: number
  limit: number
  sort: ImageUsageSort
  computedAt: string
  archiveCount: number
  postCount: number
  referenceCount: number
  mappedReferenceCount: number
  unmappedReferenceCount: number
  skippedArchives: SkippedImageUsageArchive[]
}

export interface ImageUsageThreadGroup {
  tid: number
  title: string
  usageCount: number
  replyCount: number
}

export interface ImageUsageDetailResult {
  item: ImageUsageItem
  threads: ImageUsageThreadGroup[]
}

export interface ImageUsageReplyItem {
  tid: number
  aidKey: string
  dirName: string
  pid: number
  lou: number
  floorLabel: string
  authorName: string | null
  postdate: number | string | null
  occurrenceCount: number
  html: string
  readerUrl: string
}

export interface ImageUsageRepliesResult {
  items: ImageUsageReplyItem[]
  total: number
  offset: number
  limit: number
}

export type ImageProblemKind = 'invalid_url' | 'unmapped' | 'missing_file'
export type ImageProblemFilter = 'all' | ImageProblemKind

export interface ImageProblemIssueItem {
  kind: ImageProblemKind
  url: string
  occurrenceCount: number
  relativePath: string | null
}

export interface ImageProblemPostItem {
  tid: number
  aidKey: string
  dirName: string
  title: string
  pid: number
  lou: number
  floorLabel: string
  authorName: string | null
  postdate: number | string | null
  issueCount: number
  issues: ImageProblemIssueItem[]
  html: string
  editUrl: string
}

export interface ImageProblemKindCount {
  postCount: number
  occurrenceCount: number
}

export type ImageProblemKindCounts = Record<
  ImageProblemKind,
  ImageProblemKindCount
>

export interface ImageProblemsResult {
  items: ImageProblemPostItem[]
  total: number
  offset: number
  limit: number
  kind: ImageProblemFilter
  computedAt: string
  archiveCount: number
  scannedPostCount: number
  problemPostCount: number
  problemThreadCount: number
  problemOccurrenceCount: number
  kindCounts: ImageProblemKindCounts
  skippedArchives: SkippedImageUsageArchive[]
}

export type DatabaseKind =
  | 'forum_threads'
  | 'backup_state'
  | 'image_index'
  | 'image_cache'
  | 'audio_index'
  | 'archive'
  | 'archive_state'
  | 'archive_cache'
export type DatabaseStatus = 'ready' | 'invalid'
export type TableKind = 'table' | 'view'
export type SortDirection = 'asc' | 'desc'
export type DbCellKind = 'null' | 'integer' | 'real' | 'text' | 'blob' | 'other'

export interface DatabaseSummary {
  id: string
  kind: DatabaseKind
  label: string
  relativePath: string
  status: DatabaseStatus
  message: string | null
  sizeBytes: number
  updatedAt: string
  tableCount: number
}

export interface ColumnInfo {
  name: string
  type: string
  notNull: boolean
  primaryKey: boolean
  defaultValue: string | null
}

export interface TableSummary {
  name: string
  type: TableKind
  rowCount: number | null
  columns: ColumnInfo[]
}

export interface DatabaseSchema {
  database: DatabaseSummary
  tables: TableSummary[]
}

export interface DbCell {
  kind: DbCellKind
  value: string | number | null
  truncated: boolean
}

export interface TableRow {
  rowId: number | null
  cells: Record<string, DbCell>
}

export interface TableRows {
  columns: ColumnInfo[]
  rows: TableRow[]
  total: number
  offset: number
  limit: number
  query: string
  sortBy: string | null
  sortDirection: SortDirection
}

export interface TableRowDetail {
  row: TableRow
}
