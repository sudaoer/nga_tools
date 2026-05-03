export interface AuthorInfo {
  uid: number | string | null;
  username: string;
}

export interface AnchorPost {
  lou: number;
  pid?: number | string | null;
  postdate?: string | null;
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
  first_lou: number;
  first_postdate?: string | null;
  has_duplicate: boolean;
  duplicate_lous: number[];
  confidence?: number | null;
  needs_manual_review?: boolean;
}

export interface IgnoredItem {
  lou?: number | null;
  pid?: number | string | null;
  postdate?: string | null;
  author?: AuthorInfo;
  content?: string;
  original_content?: string;
  ignore_reason?: string;
  stage?: string;
  confidence?: number | null;
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
  anchor_count?: number;
  ignored_count?: number;
  duplicate_author_count?: number;
  source_page_range?: {
    start: number;
    end: number;
  };
}

export interface RulePost {
  lou?: number | null;
  pid?: number | string | null;
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
  anchors: AnchorItem[];
  ignored: IgnoredItem[];
  warnings: string[];
  raw_stats: Record<string, unknown>;
}