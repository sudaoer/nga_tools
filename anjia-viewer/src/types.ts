export interface AuthorInfo {
  uid: number | string | null;
  username: string;
}

export interface AnchorPost {
  lou: number;
  pid?: number | string | null;
  url?: string | null;
  postdate?: string | null;
  author?: AuthorInfo;
  content: string;
  raw_clean_content?: string;
  original_content?: string;
  confidence?: number | null;
  needs_manual_review?: boolean;
  classification_source?: string | null;
  classification_note?: string | null;
  attachments?: Array<Record<string, unknown>>;
}

export interface AnchorItem {
  id: number;
  author: AuthorInfo;
  posts: AnchorPost[];
  entries?: AnchorEntry[];
  first_lou: number;
  first_postdate?: string | null;
  has_duplicate: boolean;
  duplicate_lous: number[];
  topic_ids?: string[];
  confidence?: number | null;
  needs_manual_review?: boolean;
}

export interface TopicInfo {
  id: string;
  name: string;
  short_name?: string;
  allow_multiple_per_author?: boolean;
  end_time?: string | null;
  description?: string;
}

export interface AnchorEntry {
  id: number;
  topic_id: string;
  topic_name: string;
  topic_short_name?: string;
  subtopic_name?: string | null;
  author: AuthorInfo;
  lou: number;
  pid?: number | string | null;
  url?: string | null;
  postdate?: string | null;
  content: string;
  fields?: Record<string, unknown>;
  raw_clean_content?: string;
  original_content?: string;
  attachments?: Array<Record<string, unknown>>;
  source_lous?: number[];
  source_posts?: AnchorPost[];
  superseded_lous?: number[];
  confidence?: number | null;
  needs_manual_review?: boolean;
  classification_source?: string | null;
  classification_note?: string | null;
  has_duplicate?: boolean;
  duplicate_lous?: number[];
  duplicate_entry_ids?: number[];
}

export interface IgnoredItem {
  lou?: number | null;
  pid?: number | string | null;
  url?: string | null;
  postdate?: string | null;
  author?: AuthorInfo;
  content?: string;
  original_content?: string;
  ignore_reason?: string;
  stage?: string;
  topic_id?: string | null;
  topic_name?: string | null;
  source_lous?: number[];
  superseded_by_lou?: number | null;
  confidence?: number | null;
}

export interface WarningDetail {
  type: string;
  message: string;
  entry_id?: number | null;
  topic_id?: string | null;
  topic_name?: string | null;
  author?: AuthorInfo;
  lou?: number | null;
  pid?: number | string | null;
  url?: string | null;
  source_lous?: number[];
  sources?: AnchorPost[];
}

export interface AnchorMeta {
  status: string;
  tid: number;
  rule_lou: number;
  generated_at: string;
  model?: string | null;
  llm_used: boolean;
  manual_review_required: boolean;
  cache_dir?: string;
  total_pages?: number;
  candidate_count?: number;
  entry_count?: number;
  anchor_count?: number;
  author_count?: number;
  ignored_count?: number;
  duplicate_entry_count?: number;
  duplicate_author_count?: number;
  superseded_count?: number;
  topic_counts?: Record<string, number>;
  source_page_range?: {
    start: number;
    end: number;
  };
}

export interface RulePost {
  lou?: number | null;
  pid?: number | string | null;
  url?: string | null;
  postdate?: string | null;
  author?: AuthorInfo;
  content?: string;
  original_content?: string;
}

export interface AnchorData {
  schema_version: number;
  meta: AnchorMeta;
  rule_post: RulePost | null;
  parsed_rule: Record<string, unknown> | null;
  topics?: TopicInfo[];
  entries?: AnchorEntry[];
  anchors: AnchorItem[];
  ignored: IgnoredItem[];
  warnings: string[];
  warning_details?: WarningDetail[];
  raw_stats: Record<string, unknown>;
}
