<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  Search,
  ShieldAlert,
  Tags,
  UserRound,
} from 'lucide-vue-next';
import type { AnchorData, AnchorEntry, AnchorItem, AnchorPost, AuthorInfo, IgnoredItem, TopicInfo, WarningDetail } from './types';

type TabKey = 'entries' | 'duplicates' | 'review' | 'ignored' | 'rule' | 'warnings';
type ExportTopicGroup = {
  topicId: string;
  topicTitle: string;
  topicLabel: string;
  entries: AnchorEntry[];
};

type ExportIgnoredItem = {
  item: IgnoredItem;
  reason: string;
};

type ReviewTopicHit = {
  key: string;
  entryId: number;
  topicId: string;
  topicLabel: string;
  chosenLou: number;
  needsManualReview: boolean;
  confidence?: number | null;
};

type ReviewIgnoreHit = {
  key: string;
  reason: string;
  stage?: string;
  topicLabel?: string | null;
  supersededByLou?: number | null;
};

type ReviewRow = {
  lou: number;
  pid?: number | string | null;
  url?: string | null;
  postdate?: string | null;
  author?: AuthorInfo;
  content?: string;
  topicHits: ReviewTopicHit[];
  ignoreHits: ReviewIgnoreHit[];
};

const DETAIL_SNIPPET_LIMIT = 320;
const BBCODE_ENTRY_SNIPPET_LIMIT = 120;

const defaultDataUrl = '/data/anchors_43877379.json';
const data = ref<AnchorData | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const query = ref('');
const activeTab = ref<TabKey>('entries');
const selectedTopic = ref('all');
const selectedEntryId = ref<number | null>(null);
const selectedReviewLou = ref<number | null>(null);
const copyState = ref<'idle' | 'success' | 'error'>('idle');
let copyStateResetTimer: number | null = null;

const topics = computed(() => data.value?.topics ?? topicsFromParsedRule(data.value?.parsed_rule));
const entries = computed(() => data.value?.entries ?? entriesFromAnchors(data.value?.anchors ?? []));
const ignored = computed(() => data.value?.ignored ?? []);
const visibleIgnored = computed(() => ignored.value.filter((item) => !isStartFloorIgnored(item)).slice().sort(compareIgnoredItems));
const reviewRows = computed(() => buildReviewRows(entries.value, ignored.value));
const warnings = computed(() => data.value?.warnings ?? []);
const warningDetails = computed(() => data.value?.warning_details ?? warningDetailsFromStrings(warnings.value));
const duplicateEntries = computed(() => entries.value.filter((entry) => entry.has_duplicate));
const canExportBbcode = computed(() => !!data.value && (activeTab.value === 'entries' || activeTab.value === 'duplicates') && filteredEntries.value.length > 0);

const topicOptions = computed(() => {
  const counts = new Map<string, number>();
  for (const entry of entries.value) {
    counts.set(entry.topic_id, (counts.get(entry.topic_id) ?? 0) + 1);
  }
  const options = topics.value.map((topic) => ({
    id: topic.id,
    label: topic.short_name || topic.name,
    count: counts.get(topic.id) ?? 0,
  }));
  const knownIds = new Set(options.map((option) => option.id));
  for (const [topicId, count] of counts) {
    if (!knownIds.has(topicId)) {
      options.push({ id: topicId, label: topicId, count });
    }
  }
  return [{ id: 'all', label: '全部主题', count: entries.value.length }, ...options];
});

const filteredEntries = computed(() => {
  const term = query.value.trim().toLowerCase();
  const source = activeTab.value === 'duplicates' ? duplicateEntries.value : entries.value;
  return source.filter((entry) => {
    const topicMatched = selectedTopic.value === 'all' || entry.topic_id === selectedTopic.value;
    const queryMatched = !term || entrySearchText(entry).includes(term);
    return topicMatched && queryMatched;
  });
});

const filteredIgnored = computed(() => {
  const term = query.value.trim().toLowerCase();
  return visibleIgnored.value.filter((item) => !term || ignoredSearchText(item).includes(term));
});

const filteredReviewRows = computed(() => {
  const term = query.value.trim().toLowerCase();
  return reviewRows.value.filter((row) => !term || reviewSearchText(row).includes(term));
});

const exportIgnored = computed<ExportIgnoredItem[]>(() => {
  if (activeTab.value !== 'entries' || selectedTopic.value !== 'all') {
    return [];
  }
  return filteredIgnored.value
    .map((item) => {
      const reason = sanitizeExportIgnoreReason(item.ignore_reason);
      return reason ? { item, reason } : null;
    })
    .filter((item): item is ExportIgnoredItem => item !== null);
});

const selectedEntry = computed(() => {
  if (selectedEntryId.value === null) {
    return filteredEntries.value[0] ?? null;
  }
  return filteredEntries.value.find((entry) => entry.id === selectedEntryId.value) ?? filteredEntries.value[0] ?? null;
});

const selectedReviewRow = computed(() => {
  if (selectedReviewLou.value === null) {
    return filteredReviewRows.value[0] ?? null;
  }
  return filteredReviewRows.value.find((row) => row.lou === selectedReviewLou.value) ?? filteredReviewRows.value[0] ?? null;
});

const topicEntryNumbers = computed(() => {
  const counters = new Map<string, number>();
  const numbers = new Map<number, number>();
  for (const entry of entries.value) {
    const topicId = entry.topic_id || 'unclassified';
    const nextNumber = (counters.get(topicId) ?? 0) + 1;
    counters.set(topicId, nextNumber);
    numbers.set(entry.id, nextNumber);
  }
  return numbers;
});

const tabs = computed(() => [
  { key: 'entries' as const, label: '主题安价', count: entries.value.length },
  { key: 'duplicates' as const, label: '重复复核', count: duplicateEntries.value.length },
  { key: 'review' as const, label: '逐楼核对', count: reviewRows.value.length },
  { key: 'ignored' as const, label: '忽略楼层', count: visibleIgnored.value.length },
  { key: 'rule' as const, label: '规则解析', count: data.value?.parsed_rule ? 1 : 0 },
  { key: 'warnings' as const, label: '告警', count: warningDetails.value.length },
]);

const bbcodeExportGeneratedAt = computed(() => formatGeneratedAtLabel(data.value?.meta.generated_at));

const bbcodeExportText = computed(() => buildBbcodeExport(filteredEntries.value, exportIgnored.value, bbcodeExportGeneratedAt.value));
const copyButtonLabel = computed(() => {
  if (copyState.value === 'success') {
    return '已复制';
  }
  if (copyState.value === 'error') {
    return '复制失败';
  }
  return '复制 BBCODE';
});

onMounted(() => {
  void loadDefaultData();
});

watch(filteredEntries, (nextEntries) => {
  if (activeTab.value !== 'entries' && activeTab.value !== 'duplicates') {
    return;
  }
  if (!nextEntries.some((entry) => entry.id === selectedEntryId.value)) {
    selectedEntryId.value = nextEntries[0]?.id ?? null;
  }
});

async function loadDefaultData() {
  loading.value = true;
  error.value = null;
  try {
    const response = await fetch(defaultDataUrl, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`统计数据未就绪：HTTP ${response.status}`);
    }
    const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
    if (!contentType.includes('application/json')) {
      throw new Error('统计数据未生成或不是 JSON。');
    }
    const payload = (await response.json()) as AnchorData;
    data.value = payload;
    activeTab.value = 'entries';
    selectedTopic.value = 'all';
    selectedEntryId.value = (payload.entries ?? [])[0]?.id ?? null;
  } catch (caughtError) {
    error.value = caughtError instanceof Error ? caughtError.message : String(caughtError);
  } finally {
    loading.value = false;
  }
}

async function copyBbcode() {
  if (!canExportBbcode.value || !bbcodeExportText.value) {
    return;
  }
  try {
    await copyTextToClipboard(bbcodeExportText.value);
    setCopyState('success');
  } catch {
    setCopyState('error');
  }
}

function selectEntry(entry: AnchorEntry) {
  selectedEntryId.value = entry.id;
}

function selectReviewRow(row: ReviewRow) {
  selectedReviewLou.value = row.lou;
}

function topicsFromParsedRule(parsedRule: Record<string, unknown> | null | undefined): TopicInfo[] {
  const rawTopics = parsedRule?.topics;
  return Array.isArray(rawTopics) ? (rawTopics as TopicInfo[]) : [];
}

function entriesFromAnchors(anchors: AnchorItem[]): AnchorEntry[] {
  const rebuilt: AnchorEntry[] = [];
  for (const anchor of anchors) {
    if (anchor.entries?.length) {
      rebuilt.push(...anchor.entries);
      continue;
    }
    for (const post of anchor.posts) {
      rebuilt.push({
        id: rebuilt.length + 1,
        topic_id: 'unclassified',
        topic_name: '未分类',
        topic_short_name: '未分类',
        author: anchor.author,
        lou: post.lou,
        postdate: post.postdate,
        url: post.url ?? postUrl(post.pid),
        content: post.content,
        raw_clean_content: post.raw_clean_content,
        original_content: post.original_content,
        confidence: post.confidence,
        needs_manual_review: post.needs_manual_review,
        classification_source: post.classification_source,
        classification_note: post.classification_note,
        has_duplicate: anchor.has_duplicate,
        duplicate_lous: anchor.duplicate_lous,
        source_lous: [post.lou],
        source_posts: [{ ...post, author: anchor.author, url: post.url ?? postUrl(post.pid) }],
      });
    }
  }
  return rebuilt;
}

function entrySearchText(entry: AnchorEntry) {
  return [
    entry.id,
    entry.topic_id,
    entry.topic_name,
    entry.topic_short_name,
    entry.author.uid,
    entry.author.username,
    entry.lou,
    entry.content,
    entry.raw_clean_content,
    entry.source_lous?.join(','),
    entry.source_posts?.map((post) => `${post.lou} ${post.content}`).join(' '),
    JSON.stringify(entry.fields ?? {}),
  ]
    .join(' ')
    .toLowerCase();
}

function ignoredSearchText(item: IgnoredItem) {
  return [item.lou, item.pid, item.url, item.source_lous?.join(','), item.topic_id, item.topic_name, item.author?.uid, item.author?.username, item.content, item.ignore_reason, item.stage]
    .join(' ')
    .toLowerCase();
}

function reviewSearchText(row: ReviewRow) {
  return [
    row.lou,
    row.pid,
    row.url,
    row.postdate,
    row.author?.uid,
    row.author?.username,
    row.content,
    row.topicHits.map((hit) => `${hit.topicId} ${hit.topicLabel} ${hit.chosenLou}`).join(' '),
    row.ignoreHits.map((hit) => `${hit.reason} ${hit.stage || ''} ${hit.topicLabel || ''} ${hit.supersededByLou ?? ''}`).join(' '),
  ]
    .join(' ')
    .toLowerCase();
}

function compareIgnoredItems(left: IgnoredItem, right: IgnoredItem) {
  const leftLou = typeof left.lou === 'number' ? left.lou : Number.POSITIVE_INFINITY;
  const rightLou = typeof right.lou === 'number' ? right.lou : Number.POSITIVE_INFINITY;
  if (leftLou !== rightLou) {
    return leftLou - rightLou;
  }

  const leftPid = left.pid === null || left.pid === undefined || String(left.pid).trim() === '' ? '' : String(left.pid);
  const rightPid = right.pid === null || right.pid === undefined || String(right.pid).trim() === '' ? '' : String(right.pid);
  if (leftPid !== rightPid) {
    return leftPid.localeCompare(rightPid, 'zh-CN', { numeric: true });
  }

  return (left.stage || '').localeCompare(right.stage || '', 'zh-CN', { numeric: true });
}

function compareReviewRows(left: ReviewRow, right: ReviewRow) {
  return left.lou - right.lou;
}

function compareReviewTopicHits(left: ReviewTopicHit, right: ReviewTopicHit) {
  if (left.chosenLou !== right.chosenLou) {
    return left.chosenLou - right.chosenLou;
  }
  return left.topicLabel.localeCompare(right.topicLabel, 'zh-CN', { numeric: true });
}

function compareReviewIgnoreHits(left: ReviewIgnoreHit, right: ReviewIgnoreHit) {
  return left.reason.localeCompare(right.reason, 'zh-CN', { numeric: true });
}

function ensureReviewRow(
  rowsByLou: Map<number, ReviewRow>,
  source: Omit<ReviewRow, 'topicHits' | 'ignoreHits'> & { lou: number },
) {
  const existing = rowsByLou.get(source.lou);
  if (existing) {
    if ((existing.pid === null || existing.pid === undefined || String(existing.pid).trim() === '') && source.pid !== undefined) {
      existing.pid = source.pid;
    }
    if (!existing.url && source.url) {
      existing.url = source.url;
    }
    if (!existing.postdate && source.postdate) {
      existing.postdate = source.postdate;
    }
    if ((!existing.author || !existing.author.username) && source.author) {
      existing.author = source.author;
    }
    if ((!existing.content || !existing.content.trim()) && source.content) {
      existing.content = source.content;
    }
    return existing;
  }

  const created: ReviewRow = {
    ...source,
    topicHits: [],
    ignoreHits: [],
  };
  rowsByLou.set(source.lou, created);
  return created;
}

function reviewLousFromIgnoredItem(item: IgnoredItem) {
  const lous = new Set<number>();
  if (typeof item.lou === 'number') {
    lous.add(item.lou);
  }
  for (const lou of item.source_lous ?? []) {
    if (typeof lou === 'number') {
      lous.add(lou);
    }
  }
  return Array.from(lous).sort((left, right) => left - right);
}

function buildReviewRows(entryItems: AnchorEntry[], ignoredItems: IgnoredItem[]) {
  const rowsByLou = new Map<number, ReviewRow>();

  for (const entry of entryItems) {
    for (const post of sourcePosts(entry)) {
      if (typeof post.lou !== 'number') {
        continue;
      }
      const row = ensureReviewRow(rowsByLou, {
        lou: post.lou,
        pid: post.pid,
        url: post.url ?? postUrl(post.pid),
        postdate: post.postdate ?? entry.postdate,
        author: post.author ?? entry.author,
        content: post.content || post.original_content || entry.content,
      });
      const topicKey = `entry:${entry.id}`;
      if (!row.topicHits.some((hit) => hit.key === topicKey)) {
        row.topicHits.push({
          key: topicKey,
          entryId: entry.id,
          topicId: entry.topic_id,
          topicLabel: entry.topic_short_name || entry.topic_name || entry.topic_id,
          chosenLou: entry.lou,
          needsManualReview: Boolean(entry.needs_manual_review),
          confidence: entry.confidence,
        });
      }
    }
  }

  for (const item of ignoredItems) {
    const reason = item.ignore_reason?.trim();
    if (!reason) {
      continue;
    }
    for (const lou of reviewLousFromIgnoredItem(item)) {
      const row = ensureReviewRow(rowsByLou, {
        lou,
        pid: item.pid,
        url: item.url ?? postUrl(item.pid),
        postdate: item.postdate,
        author: item.author,
        content: item.content || item.original_content,
      });
      const ignoreKey = `ignored:${lou}:${item.stage || ''}:${item.topic_id || ''}:${item.superseded_by_lou ?? ''}:${reason}`;
      if (!row.ignoreHits.some((hit) => hit.key === ignoreKey)) {
        row.ignoreHits.push({
          key: ignoreKey,
          reason,
          stage: item.stage,
          topicLabel: item.topic_name || item.topic_id,
          supersededByLou: item.superseded_by_lou,
        });
      }
    }
  }

  const rows = Array.from(rowsByLou.values()).sort(compareReviewRows);
  for (const row of rows) {
    row.topicHits.sort(compareReviewTopicHits);
    row.ignoreHits.sort(compareReviewIgnoreHits);
  }
  return rows;
}

function isStartFloorIgnored(item: IgnoredItem) {
  return /小于等于起始楼层/.test(item.ignore_reason ?? '');
}

function formatPercent(value?: number | null) {
  if (typeof value !== 'number') {
    return '待复核';
  }
  return `${Math.round(value * 100)}%`;
}

function snippet(text?: string, limit = 160) {
  if (!text) {
    return '';
  }
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

function prettyJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function postUrl(pid?: number | string | null) {
  if (pid === null || pid === undefined || String(pid).trim() === '') {
    return null;
  }
  return `https://ngabbs.com/read.php?pid=${encodeURIComponent(String(pid))}&opt=128`;
}

function sourceUrl(post: { url?: string | null; pid?: number | string | null } | null | undefined) {
  return post?.url || postUrl(post?.pid);
}

function louList(lous?: number[]) {
  return lous?.length ? lous.map((lou) => `#${lou}`).join(', ') : '';
}

function warningDetailsFromStrings(items: string[]): WarningDetail[] {
  return items.map((message) => ({ type: 'runtime_warning', message, sources: [] }));
}

function buildBbcodeExport(exportEntries: AnchorEntry[], exportIgnoredItems: ExportIgnoredItem[], generatedAtLabel: string) {
  if (!exportEntries.length && !exportIgnoredItems.length) {
    return '';
  }
  const sections: string[] = [];
  if (exportEntries.length) {
    const groups = groupEntriesByTopic(exportEntries);
    sections.push(groups.map((group) => formatBbcodeTopicBlock(group)).join('\n\n'));
  }
  if (exportIgnoredItems.length) {
    sections.push(formatBbcodeIgnoredBlock(exportIgnoredItems));
  }
  sections.push(`项目地址：[url]${currentSiteUrl()}[/url]`);
  sections.push(`统计生成时间：${generatedAtLabel}`);
  return sections.join('\n\n');
}

function groupEntriesByTopic(exportEntries: AnchorEntry[]): ExportTopicGroup[] {
  const groups = new Map<string, ExportTopicGroup>();
  for (const entry of exportEntries) {
    const topicId = entry.topic_id;
    const topicTitle = sanitizeCollapseTopicTitle(entry.topic_short_name || entry.topic_name || entry.topic_id);
    const topicLabel = entry.topic_name || entry.topic_short_name || entry.topic_id;
    if (!groups.has(topicId)) {
      groups.set(topicId, { topicId, topicTitle, topicLabel, entries: [] });
    }
    groups.get(topicId)?.entries.push(entry);
  }
  return Array.from(groups.values());
}

function formatBbcodeTopicBlock(group: ExportTopicGroup) {
  const entryBlocks = group.entries.map((entry, entryIndex) => formatBbcodeEntryBlock(entry, entryIndex + 1)).join('\n\n');
  return `[collapse=${group.topicTitle}]\n共${group.entries.length}条\n\n${entryBlocks}\n[/collapse]`;
}

function formatBbcodeEntryBlock(entry: AnchorEntry, entryIndex: number) {
  const content = snippet((entry.content || entry.raw_clean_content || '').trim(), BBCODE_ENTRY_SNIPPET_LIMIT) || '（空）';
  const sourceLabel = sourceLouLabel(entry) || `#${entry.lou}`;
  const pidLabel = formatEntryPidLabel(entry);
  return `[quote]\n${entryIndex}. ${formatEntryAuthor(entry)}｜${sourceLabel}${pidLabel}\n${content}\n[/quote]`;
}

function formatBbcodeIgnoredBlock(exportIgnoredItems: ExportIgnoredItem[]) {
  const ignoredBlocks = exportIgnoredItems
    .map(({ item, reason }, itemIndex) => formatBbcodeIgnoredItemBlock(item, reason, itemIndex + 1))
    .join('\n\n');
  return `[collapse=忽略楼层]\n共${exportIgnoredItems.length}条\n\n${ignoredBlocks}\n[/collapse]`;
}

function formatBbcodeIgnoredItemBlock(item: IgnoredItem, reason: string, itemIndex: number) {
  const content = snippet((item.content || item.original_content || '').trim(), 50) || '（空）';
  const sourceLabel = formatIgnoredLouLabel(item);
  const pidLabel = formatIgnoredPidLabel(item);
  return `[quote]\n${itemIndex}. ${formatIgnoredAuthor(item)}｜${sourceLabel}${pidLabel}\n忽略原因：${reason}\n${content}\n[/quote]`;
}

function formatEntryAuthor(entry: AnchorEntry) {
  const username = entry.author.username || '未知作者';
  if (entry.author.uid === null || entry.author.uid === undefined || String(entry.author.uid).trim() === '') {
    return username;
  }
  return `${username} (uid ${entry.author.uid})`;
}

function formatIgnoredAuthor(item: IgnoredItem) {
  const username = item.author?.username || '未知作者';
  if (item.author?.uid === null || item.author?.uid === undefined || String(item.author.uid).trim() === '') {
    return username;
  }
  return `${username} (uid ${item.author.uid})`;
}

function formatIgnoredLouLabel(item: IgnoredItem) {
  if (typeof item.lou === 'number') {
    return `#${item.lou}`;
  }
  return louList(item.source_lous) || '未知';
}

function formatIgnoredPidLabel(item: IgnoredItem) {
  if (item.pid === null || item.pid === undefined || String(item.pid).trim() === '') {
    return '';
  }
  return `｜[pid]${String(item.pid).trim()}[/pid]`;
}

function sanitizeExportIgnoreReason(reason?: string) {
  if (!reason) {
    return '';
  }
  return reason
    .split(/[；;]/)
    .map((part) => part.trim())
    .filter((part) => part && !/^作者 uid=\d+ 在忽略名单中$/.test(part) && !/^楼层 \d+ 在忽略楼层名单中$/.test(part))
    .join('；');
}

function sanitizeCollapseTopicTitle(title: string) {
  const cleaned = title.replace(/[^A-Za-z0-9\u4e00-\u9fff]/g, '').trim();
  return cleaned || '主题';
}

function formatGeneratedAtLabel(value?: string | null) {
  if (!value) {
    return '未知';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  const seconds = String(date.getSeconds()).padStart(2, '0');
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function formatEntryPidLabel(entry: AnchorEntry) {
  const pid = entry.pid ?? entry.source_posts?.find((post) => post.pid !== null && post.pid !== undefined && String(post.pid).trim() !== '')?.pid;
  if (pid === null || pid === undefined || String(pid).trim() === '') {
    return '';
  }
  return `｜[pid]${String(pid).trim()}[/pid]`;
}

function currentSiteUrl() {
  if (typeof window === 'undefined') {
    return 'https://260502.anchor.gmgo.sudoer.cn/';
  }
  return new URL('/', window.location.href).toString();
}


function setCopyState(nextState: 'idle' | 'success' | 'error') {
  copyState.value = nextState;
  if (copyStateResetTimer !== null) {
    window.clearTimeout(copyStateResetTimer);
    copyStateResetTimer = null;
  }
  if (nextState !== 'idle') {
    copyStateResetTimer = window.setTimeout(() => {
      copyState.value = 'idle';
      copyStateResetTimer = null;
    }, 1800);
  }
}

async function copyTextToClipboard(content: string) {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(content);
    return;
  }

  const textArea = document.createElement('textarea');
  textArea.value = content;
  textArea.setAttribute('readonly', 'true');
  textArea.style.position = 'fixed';
  textArea.style.top = '0';
  textArea.style.left = '0';
  textArea.style.opacity = '0';
  document.body.append(textArea);
  textArea.focus();
  textArea.select();
  textArea.setSelectionRange(0, textArea.value.length);

  const copied = document.execCommand('copy');
  textArea.remove();
  if (!copied) {
    throw new Error('Copy failed');
  }
}

function sourceLouLabel(entry: AnchorEntry) {
  return louList(entry.source_lous?.length ? entry.source_lous : [entry.lou]);
}

function topicEntryNumber(entry: AnchorEntry) {
  return topicEntryNumbers.value.get(entry.id) ?? entry.id;
}

function sourcePosts(entry: AnchorEntry): AnchorPost[] {
  if (entry.source_posts?.length) {
    return entry.source_posts;
  }
  return [{
    lou: entry.lou,
    pid: entry.pid,
    postdate: entry.postdate,
    url: entry.url ?? postUrl(entry.pid),
    author: entry.author,
    content: entry.raw_clean_content || entry.content,
    original_content: entry.original_content,
    attachments: entry.attachments,
  }];
}

function warningSourceRows(warning: WarningDetail) {
  return warning.sources?.length ? warning.sources : [];
}

function fieldRows(entry: AnchorEntry) {
  const fields = entry.fields ?? {};
  return Object.entries(fields).filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== '');
}

function tabClass(key: TabKey) {
  return ['tab-button', { active: activeTab.value === key }];
}

function topicClass(topicId: string) {
  return ['topic-button', { active: selectedTopic.value === topicId }];
}
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="title-block">
        <p class="eyebrow">NGA Anchor Counter</p>
        <h1>安价核对页</h1>
      </div>
      <div class="header-actions">
        <button v-if="canExportBbcode" class="icon-button" type="button" title="复制当前筛选结果的 BBCODE 文本" @click="copyBbcode">
          <span>{{ copyButtonLabel }}</span>
        </button>
      </div>
    </header>

    <main>
      <section v-if="data" class="summary-band" aria-label="统计摘要">
        <div class="stat-cell"><span>主题安价</span><strong>{{ data.meta.entry_count ?? entries.length }}</strong></div>
        <div class="stat-cell"><span>参与作者</span><strong>{{ data.meta.author_count ?? data.anchors.length }}</strong></div>
        <div class="stat-cell"><span>重复复核</span><strong>{{ data.meta.duplicate_entry_count ?? duplicateEntries.length }}</strong></div>
        <div class="stat-cell"><span>忽略楼层</span><strong>{{ visibleIgnored.length }}</strong></div>
        <div class="stat-cell wide"><span>规则楼</span><strong>#{{ data.meta.rule_lou }}</strong></div>
        <div class="stat-cell wide"><span>生成时间</span><strong>{{ data.meta.generated_at }}</strong></div>
      </section>

      <section v-if="error" class="notice error-notice">
        <AlertTriangle :size="18" />
        <span>{{ error }}</span>
      </section>

      <section v-if="data?.meta.manual_review_required || warnings.length" class="notice review-notice">
        <ShieldAlert :size="18" />
        <span>存在需要复核的主题安价、重复项或运行告警。</span>
      </section>

      <section class="control-row">
        <label class="search-box">
          <Search :size="18" />
          <input v-model="query" type="search" placeholder="搜索主题、作者、楼层、内容、忽略原因" />
        </label>
        <nav class="tabs" aria-label="数据视图">
          <button v-for="tab in tabs" :key="tab.key" type="button" :class="tabClass(tab.key)" @click="activeTab = tab.key">
            <span>{{ tab.label }}</span>
            <b>{{ tab.count }}</b>
          </button>
        </nav>
      </section>

      <section v-if="data && (activeTab === 'entries' || activeTab === 'duplicates')" class="topic-strip" aria-label="主题筛选">
        <Tags :size="18" />
        <button v-for="topic in topicOptions" :key="topic.id" type="button" :class="topicClass(topic.id)" @click="selectedTopic = topic.id">
          <span>{{ topic.label }}</span>
          <b>{{ topic.count }}</b>
        </button>
      </section>

      <section v-if="loading" class="empty-panel">加载中...</section>

      <section v-else-if="!data" class="empty-panel">
        <p>尚未加载网站统计数据。</p>
      </section>

      <section v-else-if="activeTab === 'rule'" class="rule-layout">
        <article class="panel">
          <h2>规则楼</h2>
          <dl class="meta-list">
            <div><dt>楼层</dt><dd>#{{ data.rule_post?.lou ?? data.meta.rule_lou }}</dd></div>
            <div><dt>作者</dt><dd>{{ data.rule_post?.author?.username || '未知' }}</dd></div>
            <div><dt>时间</dt><dd>{{ data.rule_post?.postdate || '未知' }}</dd></div>
          </dl>
          <a v-if="sourceUrl(data.rule_post || {})" :href="sourceUrl(data.rule_post || {}) || '#'" target="_blank" rel="noopener noreferrer" class="single-source-link">
            打开规则楼 #{{ data.rule_post?.lou ?? data.meta.rule_lou }}
            <ExternalLink :size="13" />
          </a>
          <p class="content-block">{{ data.rule_post?.content || '无规则正文' }}</p>
        </article>
        <article class="panel">
          <h2>解析结果</h2>
          <pre>{{ prettyJson(data.parsed_rule) }}</pre>
        </article>
      </section>

      <section v-else-if="activeTab === 'warnings'" class="panel single-panel">
        <h2>运行告警</h2>
        <ul class="warning-list">
          <li v-for="warning in warningDetails" :key="`${warning.type}-${warning.entry_id ?? ''}-${warning.message}`">
            <AlertTriangle :size="16" />
            <div class="warning-body">
              <strong>{{ warning.message }}</strong>
              <p v-if="warning.entry_id || warning.topic_name || warning.author" class="muted-line">
                <span v-if="warning.entry_id">Entry {{ warning.entry_id }} · </span>
                <span v-if="warning.topic_name">{{ warning.topic_name }} · </span>
                <span v-if="warning.author">{{ warning.author.username || '未知作者' }}</span>
              </p>
              <div v-if="warningSourceRows(warning).length" class="link-row">
                <a v-for="post in warningSourceRows(warning)" :key="`${warning.message}-${post.pid}-${post.lou}`" :href="sourceUrl(post) || '#'" target="_blank" rel="noopener noreferrer">
                  #{{ post.lou }}
                  <ExternalLink :size="13" />
                </a>
              </div>
              <a v-else-if="sourceUrl(warning)" :href="sourceUrl(warning) || '#'" target="_blank" rel="noopener noreferrer" class="single-source-link">
                #{{ warning.lou }}
                <ExternalLink :size="13" />
              </a>
            </div>
          </li>
        </ul>
        <p v-if="warningDetails.length === 0" class="muted-line">没有运行告警。</p>
      </section>

      <section v-else-if="activeTab === 'review'" class="workspace">
        <div class="list-pane">
          <button
            v-for="row in filteredReviewRows"
            :key="row.lou"
            type="button"
            :class="['entry-row', 'review-list-row', { selected: selectedReviewRow?.lou === row.lou }]"
            @click="selectReviewRow(row)"
          >
            <span class="review-list-index">#{{ row.lou }}</span>
            <span class="entry-main">
              <b>{{ row.author?.username || '未知作者' }}</b>
              <small>{{ row.postdate || '未知时间' }}</small>
              <em>{{ snippet(row.content) || '（无正文）' }}</em>
            </span>
            <span class="entry-flags">
              <i v-if="row.topicHits.length">{{ row.topicHits.length }} 安价</i>
              <i v-if="row.ignoreHits.length" class="review">{{ row.ignoreHits.length }} 忽略</i>
            </span>
          </button>
          <p v-if="filteredReviewRows.length === 0" class="muted-line list-empty">没有匹配的楼层核对结果。</p>
        </div>

        <aside v-if="selectedReviewRow" class="detail-pane">
          <div class="detail-head">
            <div>
              <p class="eyebrow">逐楼核对 · #{{ selectedReviewRow.lou }}</p>
              <h2>{{ selectedReviewRow.author?.username || '未知作者' }}</h2>
            </div>
            <div class="author-chip">
              <UserRound :size="16" />
              <span>{{ selectedReviewRow.author?.uid ?? '未知' }}</span>
            </div>
          </div>

          <div class="status-line">
            <span v-if="selectedReviewRow.topicHits.length" class="badge ok">{{ selectedReviewRow.topicHits.length }} 条安价</span>
            <span v-if="selectedReviewRow.ignoreHits.length" class="badge review">{{ selectedReviewRow.ignoreHits.length }} 条忽略</span>
          </div>

          <article class="post-detail review-detail-block">
            <header>
              <a v-if="sourceUrl(selectedReviewRow)" :href="sourceUrl(selectedReviewRow) || '#'" target="_blank" rel="noopener noreferrer" class="source-link">
                #{{ selectedReviewRow.lou }}
                <ExternalLink :size="13" />
              </a>
              <strong v-else>#{{ selectedReviewRow.lou }}</strong>
              <span>{{ selectedReviewRow.postdate || '未知时间' }}</span>
              <small>逐楼汇总</small>
            </header>
            <p class="content-block">{{ snippet(selectedReviewRow.content, DETAIL_SNIPPET_LIMIT) || '（无正文）' }}</p>
            <details v-if="selectedReviewRow.content && selectedReviewRow.content.length > DETAIL_SNIPPET_LIMIT">
              <summary>完整楼层正文</summary>
              <p class="content-block compact">{{ selectedReviewRow.content }}</p>
            </details>
          </article>

          <section v-if="selectedReviewRow.topicHits.length" class="source-list review-detail-block">
            <h3>安价结果</h3>
            <article v-for="hit in selectedReviewRow.topicHits" :key="hit.key" class="source-row">
              <header>
                <strong>{{ hit.topicLabel }}</strong>
                <span>Entry {{ hit.entryId }}</span>
                <small>#{{ hit.chosenLou }}</small>
              </header>
              <p class="content-block compact">
                <template v-if="typeof hit.confidence === 'number'">置信度 {{ formatPercent(hit.confidence) }}</template>
                <template v-if="hit.needsManualReview">
                  <template v-if="typeof hit.confidence === 'number'"> · </template>需要复核
                </template>
              </p>
            </article>
          </section>

          <section v-if="selectedReviewRow.ignoreHits.length" class="source-list review-detail-block">
            <h3>忽略原因</h3>
            <article v-for="hit in selectedReviewRow.ignoreHits" :key="hit.key" class="source-row">
              <header>
                <strong>{{ hit.reason }}</strong>
                <span>{{ hit.topicLabel || '未限定主题' }}</span>
                <small>{{ hit.stage || 'unknown' }}</small>
              </header>
              <p class="content-block compact">
                <template v-if="hit.supersededByLou">被 #{{ hit.supersededByLou }} 覆盖</template>
                <template v-else>无附加覆盖信息</template>
              </p>
            </article>
          </section>
        </aside>
      </section>

      <section v-else-if="activeTab === 'ignored'" class="panel single-panel">
        <h2>忽略楼层</h2>
        <div class="ignored-list">
          <article v-for="item in filteredIgnored" :key="`${item.lou}-${item.stage}-${item.ignore_reason}`" class="ignored-row">
            <div class="row-main">
              <strong>#{{ item.lou ?? '未知' }}</strong>
              <span>{{ item.author?.username || '未知作者' }}</span>
              <small>{{ item.stage || 'unknown' }}</small>
              <small v-if="item.topic_id">{{ item.topic_name || item.topic_id }}</small>
            </div>
            <p>{{ item.ignore_reason || '未提供忽略原因' }}</p>
            <p v-if="item.source_lous?.length || item.superseded_by_lou" class="muted-line">
              <span v-if="item.source_lous?.length">来源 {{ louList(item.source_lous) }}</span>
              <span v-if="item.superseded_by_lou"> · 被 #{{ item.superseded_by_lou }} 覆盖</span>
            </p>
            <a v-if="sourceUrl(item)" :href="sourceUrl(item) || '#'" target="_blank" rel="noopener noreferrer" class="single-source-link">
              打开 #{{ item.lou ?? '未知' }}
              <ExternalLink :size="13" />
            </a>
            <p class="muted-line">{{ snippet(item.content, 220) }}</p>
          </article>
        </div>
        <p v-if="filteredIgnored.length === 0" class="muted-line">没有匹配的忽略项。</p>
      </section>

      <section v-else class="workspace">
        <div class="list-pane">
          <button
            v-for="entry in filteredEntries"
            :key="entry.id"
            type="button"
            :class="['entry-row', { selected: selectedEntry?.id === entry.id }]"
            @click="selectEntry(entry)"
          >
            <span class="entry-index">{{ topicEntryNumber(entry) }}</span>
            <span class="entry-main">
              <b>{{ entry.topic_short_name || entry.topic_name }}</b>
              <small>{{ entry.author.username || '未知作者' }} · uid {{ entry.author.uid ?? '未知' }} · #{{ entry.lou }}</small>
              <em>{{ snippet(entry.content) }}</em>
            </span>
            <span class="entry-flags">
              <i v-if="entry.has_duplicate">重复</i>
              <i v-if="entry.needs_manual_review" class="review">复核</i>
              <b>{{ formatPercent(entry.confidence) }}</b>
            </span>
          </button>
          <p v-if="filteredEntries.length === 0" class="muted-line list-empty">没有匹配的主题安价。</p>
        </div>

        <aside v-if="selectedEntry" class="detail-pane">
          <div class="detail-head">
            <div>
              <p class="eyebrow">主题内编号 {{ topicEntryNumber(selectedEntry) }} · #{{ selectedEntry.lou }} · {{ selectedEntry.topic_id }}</p>
              <h2>{{ selectedEntry.topic_name }}</h2>
            </div>
            <div class="author-chip">
              <UserRound :size="16" />
              <span>{{ selectedEntry.author.username || '未知作者' }} / {{ selectedEntry.author.uid ?? '未知' }}</span>
            </div>
          </div>

          <div class="status-line">
            <span v-if="selectedEntry.has_duplicate" class="badge duplicate">重复楼层 {{ selectedEntry.duplicate_lous?.join(', ') }}</span>
            <span v-if="selectedEntry.needs_manual_review" class="badge review">需要复核</span>
            <span v-if="!selectedEntry.has_duplicate && !selectedEntry.needs_manual_review" class="badge ok">
              <CheckCircle2 :size="14" />
              已判定
            </span>
          </div>

          <dl v-if="fieldRows(selectedEntry).length" class="field-grid">
            <div v-for="[key, value] in fieldRows(selectedEntry)" :key="key">
              <dt>{{ key }}</dt>
              <dd>{{ value }}</dd>
            </div>
          </dl>

          <div class="source-strip">
            <span>来源楼层</span>
            <strong>{{ sourceLouLabel(selectedEntry) }}</strong>
            <em v-if="selectedEntry.superseded_lous?.length">已覆盖 {{ louList(selectedEntry.superseded_lous) }}</em>
          </div>

          <article class="post-detail">
            <header>
              <a v-if="sourceUrl(selectedEntry)" :href="sourceUrl(selectedEntry) || '#'" target="_blank" rel="noopener noreferrer" class="source-link">
                #{{ selectedEntry.lou }}
                <ExternalLink :size="13" />
              </a>
              <strong v-else>#{{ selectedEntry.lou }}</strong>
              <span>{{ selectedEntry.postdate || '未知时间' }}</span>
              <small>{{ selectedEntry.classification_source || 'source unknown' }}</small>
            </header>
            <p class="content-block">{{ selectedEntry.content }}</p>
            <details v-if="selectedEntry.raw_clean_content && selectedEntry.raw_clean_content !== selectedEntry.content">
              <summary>完整清洗原文</summary>
              <p class="content-block compact">{{ selectedEntry.raw_clean_content }}</p>
            </details>
            <p v-if="selectedEntry.classification_note" class="muted-line">{{ selectedEntry.classification_note }}</p>
          </article>

          <section class="source-list">
            <h3>来源原文</h3>
            <article v-for="(post, index) in sourcePosts(selectedEntry)" :key="`${post.lou}-${post.pid ?? index}`" class="source-row">
              <header>
                <a v-if="sourceUrl(post)" :href="sourceUrl(post) || '#'" target="_blank" rel="noopener noreferrer" class="source-link">
                  #{{ post.lou }}
                  <ExternalLink :size="13" />
                </a>
                <strong v-else>#{{ post.lou }}</strong>
                <span>{{ post.postdate || '未知时间' }}</span>
              </header>
              <p class="content-block compact">{{ post.content }}</p>
            </article>
          </section>
        </aside>
      </section>
    </main>
  </div>
</template>

<style scoped>
:global(*) { box-sizing: border-box; }
:global(body) {
  margin: 0;
  min-width: 320px;
  background: #f4f5f2;
  color: #202124;
  font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
}
button, input { font: inherit; }
.shell { min-height: 100vh; }
.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 22px 32px;
  border-bottom: 1px solid #d9ddd7;
  background: #fbfbfa;
}
.title-block h1, .detail-head h2, .panel h2, .source-list h3 { margin: 0; line-height: 1.2; }
.title-block h1 { font-size: 24px; }
.eyebrow { margin: 0 0 6px; color: #64706a; font-size: 12px; }
.icon-button, .tab-button, .topic-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 38px;
  border: 1px solid #c8d0ca;
  border-radius: 8px;
  background: #ffffff;
  color: #202124;
  cursor: pointer;
}
.icon-button { padding: 0 14px; }
.icon-button.strong { border-color: #167a73; background: #167a73; color: #ffffff; }
main {
  width: min(1480px, 100%);
  margin: 0 auto;
  padding: 24px 32px 40px;
  --workspace-pane-max-height: clamp(520px, calc(100vh - 320px), 960px);
}
.summary-band {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)) repeat(2, minmax(180px, 1.35fr));
  gap: 1px;
  overflow: hidden;
  border: 1px solid #d9ddd7;
  border-radius: 8px;
  background: #d9ddd7;
}
.stat-cell { min-height: 82px; padding: 14px 16px; background: #ffffff; }
.stat-cell span { display: block; margin-bottom: 10px; color: #64706a; font-size: 13px; }
.stat-cell strong { font-size: 24px; }
.stat-cell.wide strong { display: block; overflow: hidden; font-size: 16px; text-overflow: ellipsis; white-space: nowrap; }
.notice { display: flex; align-items: center; gap: 10px; min-height: 40px; margin-top: 14px; padding: 10px 12px; border-radius: 8px; }
.error-notice { border: 1px solid #e4b3a3; background: #fff3ee; color: #8f3218; }
.review-notice { border: 1px solid #d8c56c; background: #fff9d8; color: #6d5600; }
.control-row, .tabs, .topic-strip { display: flex; align-items: center; gap: 10px; }
.control-row { justify-content: space-between; margin: 18px 0; }
.search-box {
  display: flex;
  align-items: center;
  gap: 10px;
  width: min(440px, 100%);
  min-height: 42px;
  padding: 0 12px;
  border: 1px solid #c8d0ca;
  border-radius: 8px;
  background: #ffffff;
}
.search-box input { width: 100%; min-width: 0; border: 0; outline: 0; }
.tabs, .topic-strip { flex-wrap: wrap; justify-content: flex-end; }
.tab-button, .topic-button { padding: 0 10px; }
.tab-button b, .topic-button b { min-width: 22px; padding: 2px 6px; border-radius: 999px; background: #eef1ee; font-size: 12px; }
.tab-button.active, .topic-button.active { border-color: #167a73; color: #0f625c; }
.topic-strip { justify-content: flex-start; margin: -4px 0 18px; padding: 10px; border: 1px solid #d9ddd7; border-radius: 8px; background: #ffffff; }
.workspace { display: grid; grid-template-columns: minmax(390px, 0.95fr) minmax(0, 1.35fr); gap: 16px; align-items: start; }
.list-pane, .detail-pane, .panel, .empty-panel { border: 1px solid #d9ddd7; border-radius: 8px; background: #ffffff; }
.list-pane,
.detail-pane {
  min-height: 520px;
  max-height: var(--workspace-pane-max-height);
  overflow-y: auto;
  scrollbar-gutter: stable;
  overscroll-behavior: contain;
}
.entry-row {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  gap: 12px;
  width: 100%;
  min-height: 96px;
  padding: 12px 14px;
  border: 0;
  border-bottom: 1px solid #edf0ec;
  background: #ffffff;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.entry-row:hover, .entry-row.selected { background: #eef7f5; }
.entry-index { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 8px; background: #27312f; color: #ffffff; font-weight: 700; }
.entry-main, .entry-flags, .row-main { display: flex; min-width: 0; }
.entry-main { flex-direction: column; gap: 4px; }
.entry-main small, .muted-line, .post-detail header span, .post-detail header small { color: #64706a; }
.entry-main em { overflow: hidden; color: #333837; font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
.entry-flags { align-items: flex-start; justify-content: flex-end; flex-wrap: wrap; gap: 6px; max-width: 118px; }
.entry-flags i, .badge { display: inline-flex; align-items: center; gap: 5px; min-height: 24px; padding: 2px 8px; border-radius: 999px; background: #f2efe7; color: #795c18; font-size: 12px; font-style: normal; }
.entry-flags .review, .badge.review { background: #fff0e5; color: #9a3f16; }
.badge.duplicate { background: #eef0fb; color: #394d8f; }
.badge.ok { background: #e7f5ed; color: #14633a; }
.detail-pane { padding: 18px; }
.detail-head, .status-line, .post-detail header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.author-chip { display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 10px; border: 1px solid #d9ddd7; border-radius: 8px; color: #4f5b56; }
.status-line { justify-content: flex-start; flex-wrap: wrap; margin: 14px 0; }
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 12px 0; }
.field-grid div, .meta-list div { padding: 10px; border: 1px solid #edf0ec; border-radius: 8px; }
.field-grid dt, .meta-list dt { color: #64706a; font-size: 12px; }
.field-grid dd, .meta-list dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.source-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
  padding: 10px 12px;
  border: 1px solid #d9ddd7;
  border-radius: 8px;
  background: #f8faf8;
}
.source-strip span { color: #64706a; font-size: 12px; }
.source-strip strong { color: #202124; }
.source-strip em { color: #795c18; font-size: 12px; font-style: normal; }
.post-detail { padding: 14px 0; border-top: 1px solid #edf0ec; }
.post-detail header { justify-content: flex-start; margin-bottom: 10px; }
.content-block { margin: 0; white-space: pre-wrap; word-break: break-word; line-height: 1.7; }
.content-block.compact { margin-top: 8px; color: #4f5b56; }
details { margin-top: 10px; }
summary { cursor: pointer; color: #167a73; }
.source-list { display: grid; gap: 10px; padding-top: 12px; border-top: 1px solid #edf0ec; }
.source-list h3 { font-size: 15px; }
.source-row { padding: 12px; border: 1px solid #edf0ec; border-radius: 8px; background: #ffffff; }
.source-row header { display: flex; align-items: center; gap: 10px; color: #64706a; font-size: 13px; }
.source-row header strong { color: #202124; }
.rule-layout { display: grid; grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr); gap: 16px; }
.panel, .empty-panel { padding: 18px; }
.single-panel { min-height: 420px; }
.meta-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
pre { max-height: 560px; overflow: auto; margin: 14px 0 0; padding: 14px; border-radius: 8px; background: #202124; color: #f2f5f0; line-height: 1.5; }
.warning-list { display: grid; gap: 10px; padding: 0; list-style: none; }
.warning-list li { display: flex; align-items: flex-start; gap: 8px; padding: 10px; border: 1px solid #e4d488; border-radius: 8px; background: #fff9df; }
.warning-body { display: grid; gap: 6px; min-width: 0; }
.warning-body strong { font-weight: 650; }
.link-row { display: flex; flex-wrap: wrap; gap: 8px; }
.link-row a, .single-source-link, .source-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #0f625c;
  font-weight: 650;
  text-decoration: none;
}
.link-row a:hover, .single-source-link:hover, .source-link:hover { text-decoration: underline; }
.review-list-row { grid-template-columns: 72px minmax(0, 1fr) auto; }
.review-list-index {
  display: grid;
  place-items: center;
  min-width: 58px;
  height: 34px;
  padding: 0 8px;
  border-radius: 8px;
  background: #27312f;
  color: #ffffff;
  font-size: 12px;
  font-weight: 700;
}
.review-detail-block:first-of-type { border-top: 0; padding-top: 0; }
.ignored-list { display: grid; gap: 10px; }
.ignored-row { padding: 12px; border: 1px solid #edf0ec; border-radius: 8px; }
.ignored-row p { margin: 8px 0 0; }
.row-main { align-items: center; gap: 10px; flex-wrap: wrap; }
.row-main small { padding: 2px 8px; border-radius: 999px; background: #eef1ee; color: #4f5b56; }
.empty-panel { display: grid; min-height: 260px; place-items: center; color: #64706a; }
.list-empty { padding: 16px; }
@media (max-width: 1100px) {
  .summary-band { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .workspace, .rule-layout { grid-template-columns: 1fr; }
  .list-pane, .detail-pane {
    max-height: none;
    overflow: visible;
  }
}
@media (max-width: 760px) {
  .topbar, .control-row { align-items: stretch; flex-direction: column; }
  main, .topbar { padding-left: 16px; padding-right: 16px; }
  .tabs, .topic-strip { justify-content: flex-start; overflow-x: auto; }
  .summary-band, .meta-list, .field-grid { grid-template-columns: 1fr; }
  .entry-row { grid-template-columns: 38px minmax(0, 1fr); }
  .entry-flags { grid-column: 2; justify-content: flex-start; max-width: none; }
  .review-list-row { grid-template-columns: minmax(0, 1fr); }
  .review-list-index { width: fit-content; }
  .detail-head, .post-detail header { align-items: flex-start; flex-direction: column; }
}
</style>
