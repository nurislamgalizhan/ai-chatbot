import { api } from "./client";

export type KbLang = "ru" | "kk" | "en";
export type DocStatus = "draft" | "active" | "archived";
export type AnswerStatus = "absent" | "machine" | "approved";

export interface KbDocument {
  id: number;
  title: string;
  category: string | null;
  language: KbLang;
  status: DocStatus;
  version: number;
  is_searchable: boolean;
  valid_until: string | null;
  index_state: string;
  index_error: string | null;
  chunk_count: number;
  updated_at: string;
}

export interface KbDocumentDetail extends KbDocument {
  body: string;
  created_at: string;
}

export interface FaqPattern {
  id: number;
  language: KbLang;
  pattern: string;
}

export interface FaqEntry {
  id: number;
  normalized_key: string;
  category: string | null;
  is_active: boolean;
  answer_ru: string | null;
  answer_ru_status: AnswerStatus;
  answer_kk: string | null;
  answer_kk_status: AnswerStatus;
  answer_en: string | null;
  answer_en_status: AnswerStatus;
  patterns: FaqPattern[];
  can_activate: boolean;
  blockers: string[];
  misses_kk: number;
  misses_en: number;
  updated_at: string;
}

export interface AiGenerationResult {
  item: FaqEntry;
  cost_kzt: string;
  added: number;
}

export interface TranslationRow {
  faq_id: number;
  normalized_key: string;
  category: string | null;
  source_ru: string | null;
  language: KbLang;
  draft: string | null;
  status: AnswerStatus;
  miss_count: number;
}

export interface LanguageMissRow {
  language: KbLang;
  faq_id: number | null;
  normalized_key: string | null;
  category: string | null;
  misses: number;
}

export const listDocuments = (status?: string) =>
  api<{ items: KbDocument[] }>(
    "GET",
    `/api/kb/documents${status ? `?status=${status}` : ""}`,
  );

export const getDocument = (id: number) =>
  api<KbDocumentDetail>("GET", `/api/kb/documents/${id}`);

export const createDocument = (payload: {
  title: string;
  body: string;
  category?: string | null;
}) => api<KbDocumentDetail>("POST", "/api/kb/documents", payload);

export const updateDocument = (
  id: number,
  payload: Partial<{
    title: string;
    body: string;
    category: string | null;
    status: DocStatus;
    is_searchable: boolean;
  }>,
) => api<KbDocumentDetail>("PATCH", `/api/kb/documents/${id}`, payload);

export const deleteDocument = (id: number) =>
  api<void>("DELETE", `/api/kb/documents/${id}`);

export const reindexDocument = (id: number) =>
  api<KbDocumentDetail>("POST", `/api/kb/documents/${id}/reindex`, {});

export const listChunks = (id: number) =>
  api<{
    items: {
      id: number;
      chunk_index: number;
      content: string;
      token_count: number;
      embedding_model: string;
    }[];
  }>("GET", `/api/kb/documents/${id}/chunks`);

export const documentLog = (id: number) =>
  api<{
    items: {
      step: string;
      status: string;
      detail: string | null;
      tokens_used: number;
      created_at: string;
    }[];
  }>("GET", `/api/kb/documents/${id}/log`);

export const listFaq = () => api<{ items: FaqEntry[] }>("GET", "/api/kb/faq");

export const createFaq = (payload: {
  normalized_key?: string;
  category: string | null;
  answer_ru: string;
  patterns_ru: string[];
  patterns_kk: string[];
}) => api<FaqEntry>("POST", "/api/kb/faq", payload);

export const updateFaqAnswer = (
  id: number,
  language: KbLang,
  answer: string | null,
  status: AnswerStatus,
) =>
  api<FaqEntry>("PATCH", `/api/kb/faq/${id}/answer`, {
    language,
    answer,
    status,
  });

export const activateFaq = (id: number, isActive: boolean) =>
  api<FaqEntry>("POST", `/api/kb/faq/${id}/activate`, { is_active: isActive });

export const addFaqPattern = (id: number, language: KbLang, pattern: string) =>
  api<FaqEntry>("POST", `/api/kb/faq/${id}/patterns`, { language, pattern });

export const listTranslations = (language: KbLang, status: string) =>
  api<{ items: TranslationRow[] }>(
    "GET",
    `/api/kb/faq/translations?language=${language}&status=${status}`,
  );

export const languageMisses = () =>
  api<{ items: LanguageMissRow[]; total: number }>(
    "GET",
    "/api/kb/faq/language-misses",
  );

export interface SearchHit {
  chunk_id: number;
  document_id: number;
  title: string;
  content: string;
  score: number;
}

export interface SearchResult {
  faq_hit: string | null;
  faq_language_miss: boolean;
  detected_language: KbLang;
  hits: SearchHit[];
}

export const searchPreview = (query: string) =>
  api<SearchResult>("POST", "/api/kb/search", { query });

export const updateFaq = (
  id: number,
  payload: { answer_ru?: string; category?: string | null },
) => api<FaqEntry>("PATCH", `/api/kb/faq/${id}`, payload);

export const deleteFaq = (id: number) => api<void>("DELETE", `/api/kb/faq/${id}`);

/** Paid: the model writes the translation; it is stored as a draft the
 *  client can never receive until the owner approves it. */
export const translateFaq = (id: number, language: "kk" | "en") =>
  api<AiGenerationResult>("POST", `/api/kb/faq/${id}/translate`, { language });

/** Paid: the model proposes how clients actually phrase this question. */
export const suggestPatterns = (id: number, language: KbLang) =>
  api<AiGenerationResult>("POST", `/api/kb/faq/${id}/suggest-patterns`, { language });

export const deleteFaqPattern = (faqId: number, patternId: number) =>
  api<FaqEntry>("DELETE", `/api/kb/faq/${faqId}/patterns/${patternId}`);
