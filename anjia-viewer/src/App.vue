<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import {
  AlertTriangle,
  CheckCircle2,
  FileUp,
  RefreshCcw,
  Search,
  ShieldAlert,
  UserRound,
} from 'lucide-vue-next';
import type { AnchorData, AnchorItem, IgnoredItem } from './types';

type TabKey = 'anchors' | 'duplicates' | 'ignored' | 'rule' | 'warnings';

const defaultDataUrl = '/data/anchors_43877379.json';
const data = ref<AnchorData | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const query = ref('');
const activeTab = ref<TabKey>('anchors');
const selectedAnchorId = ref<number | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);

const anchors = computed(() => data.value?.anchors ?? []);
const ignored = computed(() => data.value?.ignored ?? []);
const warnings = computed(() => data.value?.warnings ?? []);
const duplicateAnchors = computed(() => anchors.value.filter((anchor) => anchor.has_duplicate));

const filteredAnchors = computed(() => {
  const term = query.value.trim().toLowerCase();
  const source = activeTab.value === 'duplicates' ? duplicateAnchors.value : anchors.value;
  if (!term) {
    return source;
  }
  return source.filter((anchor) => anchorSearchText(anchor).includes(term));
});

const filteredIgnored = computed(() => {
  const term = query.value.trim().toLowerCase();
  if (!term) {
    return ignored.value;
  }
  return ignored.value.filter((item) => ignoredSearchText(item).includes(term));
});

const selectedAnchor = computed(() => {
  if (selectedAnchorId.value === null) {
    return filteredAnchors.value[0] ?? null;
  }
  return filteredAnchors.value.find((anchor) => anchor.id === selectedAnchorId.value) ?? filteredAnchors.value[0] ?? null;
});

const tabs = computed(() => [
  { key: 'anchors' as const, label: '有效安价', count: anchors.value.length },
  { key: 'duplicates' as const, label: '重复作者', count: duplicateAnchors.value.length },
  { key: 'ignored' as const, label: '忽略楼层', count: ignored.value.length },
  { key: 'rule' as const, label: '规则解析', count: data.value?.parsed_rule ? 1 : 0 },
  { key: 'warnings' as const, label: '告警', count: warnings.value.length },
]);

onMounted(() => {
  void loadDefaultData();
});

watch(filteredAnchors, (nextAnchors) => {
  if (activeTab.value !== 'anchors' && activeTab.value !== 'duplicates') {
    return;
  }
  if (!nextAnchors.some((anchor) => anchor.id === selectedAnchorId.value)) {
    selectedAnchorId.value = nextAnchors[0]?.id ?? null;
  }
});

async function loadDefaultData() {
  loading.value = true;
  error.value = null;
  try {
    const response = await fetch(defaultDataUrl, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`默认数据未就绪：HTTP ${response.status}`);
    }
    const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
    if (!contentType.includes('application/json')) {
      throw new Error('默认数据未生成或不是 JSON。');
    }
    const payload = (await response.json()) as AnchorData;
    setData(payload);
  } catch (caughtError) {
    error.value = caughtError instanceof Error ? caughtError.message : String(caughtError);
  } finally {
    loading.value = false;
  }
}

function chooseFile() {
  fileInput.value?.click();
}

async function onFilePicked(event: Event) {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (!file) {
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    const payload = JSON.parse(await file.text()) as AnchorData;
    setData(payload);
  } catch (caughtError) {
    error.value = caughtError instanceof Error ? caughtError.message : String(caughtError);
  } finally {
    loading.value = false;
    target.value = '';
  }
}

function setData(payload: AnchorData) {
  data.value = payload;
  activeTab.value = 'anchors';
  selectedAnchorId.value = payload.anchors[0]?.id ?? null;
}

function selectAnchor(anchor: AnchorItem) {
  selectedAnchorId.value = anchor.id;
}

function anchorSearchText(anchor: AnchorItem) {
  return [
    anchor.id,
    anchor.author.uid,
    anchor.author.username,
    anchor.first_lou,
    anchor.posts.map((post) => `${post.lou} ${post.content} ${post.raw_clean_content ?? ''}`).join(' '),
  ]
    .join(' ')
    .toLowerCase();
}

function ignoredSearchText(item: IgnoredItem) {
  return [item.lou, item.author?.uid, item.author?.username, item.content, item.ignore_reason, item.stage]
    .join(' ')
    .toLowerCase();
}

function formatPercent(value?: number | null) {
  if (typeof value !== 'number') {
    return '待复核';
  }
  return `${Math.round(value * 100)}%`;
}

function snippet(text?: string, limit = 180) {
  if (!text) {
    return '';
  }
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

function prettyJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function tabClass(key: TabKey) {
  return ['tab-button', { active: activeTab.value === key }];
}
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="title-block">
        <p class="eyebrow">NGA Anchor Counter</p>
        <h1>安价统计查看器</h1>
      </div>
      <div class="toolbar">
        <button class="icon-button" type="button" title="重新载入默认 JSON" @click="loadDefaultData">
          <RefreshCcw :size="18" />
          <span>刷新</span>
        </button>
        <button class="icon-button strong" type="button" title="打开本地 JSON" @click="chooseFile">
          <FileUp :size="18" />
          <span>打开 JSON</span>
        </button>
        <input ref="fileInput" class="hidden-input" type="file" accept="application/json,.json" @change="onFilePicked" />
      </div>
    </header>

    <main>
      <section v-if="data" class="summary-band" aria-label="统计摘要">
        <div class="stat-cell">
          <span>有效安价</span>
          <strong>{{ data.meta.anchor_count ?? anchors.length }}</strong>
        </div>
        <div class="stat-cell">
          <span>重复作者</span>
          <strong>{{ data.meta.duplicate_author_count ?? duplicateAnchors.length }}</strong>
        </div>
        <div class="stat-cell">
          <span>候选楼层</span>
          <strong>{{ data.meta.candidate_count ?? 0 }}</strong>
        </div>
        <div class="stat-cell">
          <span>忽略楼层</span>
          <strong>{{ data.meta.ignored_count ?? ignored.length }}</strong>
        </div>
        <div class="stat-cell wide">
          <span>规则楼</span>
          <strong>#{{ data.meta.rule_lou }}</strong>
        </div>
        <div class="stat-cell wide">
          <span>生成时间</span>
          <strong>{{ data.meta.generated_at }}</strong>
        </div>
      </section>

      <section v-if="error" class="notice error-notice">
        <AlertTriangle :size="18" />
        <span>{{ error }}</span>
      </section>

      <section v-if="data?.meta.manual_review_required || warnings.length" class="notice review-notice">
        <ShieldAlert :size="18" />
        <span>存在需要复核的统计项或运行告警。</span>
      </section>

      <section class="control-row">
        <label class="search-box">
          <Search :size="18" />
          <input v-model="query" type="search" placeholder="搜索作者、楼层或内容" />
        </label>
        <nav class="tabs" aria-label="数据视图">
          <button v-for="tab in tabs" :key="tab.key" type="button" :class="tabClass(tab.key)" @click="activeTab = tab.key">
            <span>{{ tab.label }}</span>
            <b>{{ tab.count }}</b>
          </button>
        </nav>
      </section>

      <section v-if="loading" class="empty-panel">加载中...</section>

      <section v-else-if="!data" class="empty-panel">
        <p>尚未加载统计 JSON。</p>
      </section>

      <section v-else-if="activeTab === 'rule'" class="rule-layout">
        <article class="panel">
          <h2>规则楼</h2>
          <dl class="meta-list">
            <div><dt>楼层</dt><dd>#{{ data.rule_post?.lou ?? data.meta.rule_lou }}</dd></div>
            <div><dt>作者</dt><dd>{{ data.rule_post?.author?.username || '未知' }}</dd></div>
            <div><dt>时间</dt><dd>{{ data.rule_post?.postdate || '未知' }}</dd></div>
          </dl>
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
          <li v-for="warning in warnings" :key="warning">
            <AlertTriangle :size="16" />
            <span>{{ warning }}</span>
          </li>
        </ul>
        <p v-if="warnings.length === 0" class="muted-line">没有运行告警。</p>
      </section>

      <section v-else-if="activeTab === 'ignored'" class="panel single-panel">
        <h2>忽略楼层</h2>
        <div class="ignored-list">
          <article v-for="item in filteredIgnored" :key="`${item.lou}-${item.stage}-${item.ignore_reason}`" class="ignored-row">
            <div class="row-main">
              <strong>#{{ item.lou ?? '未知' }}</strong>
              <span>{{ item.author?.username || '未知作者' }}</span>
              <small>{{ item.stage || 'unknown' }}</small>
            </div>
            <p>{{ item.ignore_reason || '未提供忽略原因' }}</p>
            <p class="muted-line">{{ snippet(item.content, 220) }}</p>
          </article>
        </div>
        <p v-if="filteredIgnored.length === 0" class="muted-line">没有匹配的忽略项。</p>
      </section>

      <section v-else class="workspace">
        <div class="list-pane">
          <button
            v-for="anchor in filteredAnchors"
            :key="anchor.id"
            type="button"
            :class="['anchor-row', { selected: selectedAnchor?.id === anchor.id }]"
            @click="selectAnchor(anchor)"
          >
            <span class="anchor-index">{{ anchor.id }}</span>
            <span class="anchor-main">
              <b>{{ anchor.author.username || '未知作者' }}</b>
              <small>uid {{ anchor.author.uid ?? '未知' }} · #{{ anchor.first_lou }}</small>
              <em>{{ snippet(anchor.posts[0]?.content, 120) }}</em>
            </span>
            <span class="anchor-flags">
              <i v-if="anchor.has_duplicate">重复</i>
              <i v-if="anchor.needs_manual_review" class="review">复核</i>
              <b>{{ formatPercent(anchor.confidence) }}</b>
            </span>
          </button>
          <p v-if="filteredAnchors.length === 0" class="muted-line">没有匹配的安价。</p>
        </div>

        <aside v-if="selectedAnchor" class="detail-pane">
          <div class="detail-head">
            <div>
              <p class="eyebrow">#{{ selectedAnchor.first_lou }}</p>
              <h2>{{ selectedAnchor.author.username || '未知作者' }}</h2>
            </div>
            <div class="author-chip">
              <UserRound :size="16" />
              <span>{{ selectedAnchor.author.uid ?? '未知' }}</span>
            </div>
          </div>

          <div class="status-line">
            <span v-if="selectedAnchor.has_duplicate" class="badge duplicate">重复楼层 {{ selectedAnchor.duplicate_lous.join(', ') }}</span>
            <span v-if="selectedAnchor.needs_manual_review" class="badge review">需要复核</span>
            <span v-if="!selectedAnchor.has_duplicate && !selectedAnchor.needs_manual_review" class="badge ok">
              <CheckCircle2 :size="14" />
              已判定
            </span>
          </div>

          <article v-for="post in selectedAnchor.posts" :key="post.lou" class="post-detail">
            <header>
              <strong>#{{ post.lou }}</strong>
              <span>{{ post.postdate || '未知时间' }}</span>
              <small>{{ post.classification_source || 'source unknown' }}</small>
            </header>
            <p class="content-block">{{ post.content }}</p>
            <details v-if="post.raw_clean_content && post.raw_clean_content !== post.content">
              <summary>清洗原文</summary>
              <p class="content-block compact">{{ post.raw_clean_content }}</p>
            </details>
            <p v-if="post.classification_note" class="muted-line">{{ post.classification_note }}</p>
          </article>
        </aside>
      </section>
    </main>
  </div>
</template>

<style scoped>
:global(*) {
  box-sizing: border-box;
}

:global(body) {
  margin: 0;
  min-width: 320px;
  background: #f4f5f2;
  color: #202124;
  font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
}

button,
input {
  font: inherit;
}

.shell {
  min-height: 100vh;
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

.title-block h1,
.detail-head h2,
.panel h2 {
  margin: 0;
  line-height: 1.2;
}

.title-block h1 {
  font-size: 24px;
}

.eyebrow {
  margin: 0 0 6px;
  color: #64706a;
  font-size: 12px;
}

.toolbar,
.tabs,
.control-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.icon-button,
.tab-button {
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

.icon-button {
  padding: 0 14px;
}

.icon-button.strong {
  border-color: #167a73;
  background: #167a73;
  color: #ffffff;
}

.hidden-input {
  display: none;
}

main {
  width: min(1480px, 100%);
  margin: 0 auto;
  padding: 24px 32px 40px;
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

.stat-cell {
  min-height: 82px;
  padding: 14px 16px;
  background: #ffffff;
}

.stat-cell span {
  display: block;
  margin-bottom: 10px;
  color: #64706a;
  font-size: 13px;
}

.stat-cell strong {
  font-size: 24px;
}

.stat-cell.wide strong {
  display: block;
  overflow: hidden;
  font-size: 16px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.notice {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
  margin-top: 14px;
  padding: 10px 12px;
  border-radius: 8px;
}

.error-notice {
  border: 1px solid #e4b3a3;
  background: #fff3ee;
  color: #8f3218;
}

.review-notice {
  border: 1px solid #d8c56c;
  background: #fff9d8;
  color: #6d5600;
}

.control-row {
  justify-content: space-between;
  margin: 18px 0;
}

.search-box {
  display: flex;
  align-items: center;
  gap: 10px;
  width: min(420px, 100%);
  min-height: 42px;
  padding: 0 12px;
  border: 1px solid #c8d0ca;
  border-radius: 8px;
  background: #ffffff;
}

.search-box input {
  width: 100%;
  min-width: 0;
  border: 0;
  outline: 0;
}

.tabs {
  flex-wrap: wrap;
  justify-content: flex-end;
}

.tab-button {
  padding: 0 10px;
}

.tab-button b {
  min-width: 22px;
  padding: 2px 6px;
  border-radius: 999px;
  background: #eef1ee;
  font-size: 12px;
}

.tab-button.active {
  border-color: #167a73;
  color: #0f625c;
}

.workspace {
  display: grid;
  grid-template-columns: minmax(360px, 0.95fr) minmax(0, 1.35fr);
  gap: 16px;
}

.list-pane,
.detail-pane,
.panel,
.empty-panel {
  border: 1px solid #d9ddd7;
  border-radius: 8px;
  background: #ffffff;
}

.list-pane {
  min-height: 520px;
  overflow: hidden;
}

.anchor-row {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  gap: 12px;
  width: 100%;
  min-height: 92px;
  padding: 12px 14px;
  border: 0;
  border-bottom: 1px solid #edf0ec;
  background: #ffffff;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.anchor-row:hover,
.anchor-row.selected {
  background: #eef7f5;
}

.anchor-index {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 8px;
  background: #27312f;
  color: #ffffff;
  font-weight: 700;
}

.anchor-main,
.anchor-flags,
.row-main {
  display: flex;
  min-width: 0;
}

.anchor-main {
  flex-direction: column;
  gap: 4px;
}

.anchor-main small,
.muted-line,
.post-detail header span,
.post-detail header small {
  color: #64706a;
}

.anchor-main em {
  overflow: hidden;
  color: #333837;
  font-style: normal;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.anchor-flags {
  align-items: flex-start;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px;
  max-width: 118px;
}

.anchor-flags i,
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 24px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #f2efe7;
  color: #795c18;
  font-size: 12px;
  font-style: normal;
}

.anchor-flags .review,
.badge.review {
  background: #fff0e5;
  color: #9a3f16;
}

.badge.duplicate {
  background: #eef0fb;
  color: #394d8f;
}

.badge.ok {
  background: #e7f5ed;
  color: #14633a;
}

.detail-pane {
  min-height: 520px;
  padding: 18px;
}

.detail-head,
.status-line,
.post-detail header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.author-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid #d9ddd7;
  border-radius: 8px;
  color: #4f5b56;
}

.status-line {
  justify-content: flex-start;
  flex-wrap: wrap;
  margin: 14px 0;
}

.post-detail {
  padding: 14px 0;
  border-top: 1px solid #edf0ec;
}

.post-detail header {
  justify-content: flex-start;
  margin-bottom: 10px;
}

.content-block {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
}

.content-block.compact {
  margin-top: 8px;
  color: #4f5b56;
}

details {
  margin-top: 10px;
}

summary {
  cursor: pointer;
  color: #167a73;
}

.rule-layout {
  display: grid;
  grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr);
  gap: 16px;
}

.panel,
.empty-panel {
  padding: 18px;
}

.single-panel {
  min-height: 420px;
}

.meta-list {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 16px 0;
}

.meta-list div {
  padding: 10px;
  border: 1px solid #edf0ec;
  border-radius: 8px;
}

.meta-list dt {
  color: #64706a;
  font-size: 12px;
}

.meta-list dd {
  margin: 4px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

pre {
  max-height: 560px;
  overflow: auto;
  margin: 14px 0 0;
  padding: 14px;
  border-radius: 8px;
  background: #202124;
  color: #f2f5f0;
  line-height: 1.5;
}

.warning-list {
  display: grid;
  gap: 10px;
  padding: 0;
  list-style: none;
}

.warning-list li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px;
  border: 1px solid #e4d488;
  border-radius: 8px;
  background: #fff9df;
}

.ignored-list {
  display: grid;
  gap: 10px;
}

.ignored-row {
  padding: 12px;
  border: 1px solid #edf0ec;
  border-radius: 8px;
}

.ignored-row p {
  margin: 8px 0 0;
}

.row-main {
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.row-main small {
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef1ee;
  color: #4f5b56;
}

.empty-panel {
  display: grid;
  min-height: 260px;
  place-items: center;
  color: #64706a;
}

@media (max-width: 1100px) {
  .summary-band {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .workspace,
  .rule-layout {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .topbar,
  .control-row {
    align-items: stretch;
    flex-direction: column;
  }

  main,
  .topbar {
    padding-left: 16px;
    padding-right: 16px;
  }

  .toolbar,
  .tabs {
    justify-content: flex-start;
    overflow-x: auto;
  }

  .summary-band,
  .meta-list {
    grid-template-columns: 1fr;
  }

  .anchor-row {
    grid-template-columns: 38px minmax(0, 1fr);
  }

  .anchor-flags {
    grid-column: 2;
    justify-content: flex-start;
    max-width: none;
  }

  .detail-head,
  .post-detail header {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>