import { api } from "./client";

export interface TagInfo {
  id: number;
  name: string;
  color: string | null;
}

export interface ChatListItem {
  id: number;
  phone: string;
  display_name: string | null;
  mode: string;
  paused_until: string | null;
  is_resolved: boolean;
  unread_count: number;
  needs_attention: boolean;
  attention_reason: string | null;
  last_message_at: string;
  last_actor: string | null;
  last_message_preview: string | null;
  total_cost_usd: number;
  tags: TagInfo[];
}

export interface ChatListResponse {
  items: ChatListItem[];
  next_cursor: string | null;
}

export interface ChatDetail {
  id: number;
  phone: string;
  display_name: string | null;
  wa_chat_id: string;
  mode: string;
  paused_until: string | null;
  is_resolved: boolean;
  needs_attention: boolean;
  attention_reason: string | null;
  last_message_at: string;
  total_cost_usd: number;
  created_at: string;
  tags: TagInfo[];
}

export interface ChatMessage {
  id: number;
  direction: string;
  source: string;
  body: string | null;
  media_type: string | null;
  has_media: boolean;
  transcript: string | null;
  state: string;
  delivery_status: string | null;
  error_code: string | null;
  created_at: string;
  sent_at: string | null;
}

export interface MessageListResponse {
  items: ChatMessage[];
  next_cursor: string | null;
}

export type ChatFilter = "all" | "attention" | "unread" | "unresolved";

export function listChats(filter: ChatFilter, q: string, cursor?: string) {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
  if (q) params.set("q", q);
  if (filter === "attention") params.set("needs_attention", "true");
  if (filter === "unread") params.set("unread_only", "true");
  if (filter === "unresolved") params.set("is_resolved", "false");
  const qs = params.toString();
  return api<ChatListResponse>("GET", `/api/chats${qs ? `?${qs}` : ""}`);
}

export const getChat = (id: number) => api<ChatDetail>("GET", `/api/chats/${id}`);

/** Marks the dialog read and clears «требует внимания» (owner decision):
 *  a POST, so a cross-site link to the GET cannot do it. */
export const markChatOpened = (id: number) =>
  api<ChatDetail>("POST", `/api/chats/${id}/open`);

export const listMessages = (id: number, beforeId?: number) =>
  api<MessageListResponse>(
    "GET",
    `/api/chats/${id}/messages${beforeId ? `?before_id=${beforeId}` : ""}`,
  );

export const sendMessage = (id: number, body: string, clientMessageId: string) =>
  api<{ message_id: number; created: boolean }>("POST", `/api/chats/${id}/messages`, {
    body,
    client_message_id: clientMessageId,
  });

export const resolveChat = (id: number, isResolved: boolean) =>
  api<ChatDetail>("POST", `/api/chats/${id}/resolve`, { is_resolved: isResolved });

export const listTags = () => api<{ items: TagInfo[] }>("GET", "/api/tags");

export const createTag = (name: string) =>
  api<TagInfo>("POST", "/api/tags", { name, color: null });

export const attachTag = (chatId: number, tagId: number) =>
  api<void>("POST", `/api/chats/${chatId}/tags`, { tag_id: tagId });

export const detachTag = (chatId: number, tagId: number) =>
  api<void>("DELETE", `/api/chats/${chatId}/tags/${tagId}`);

export interface Note {
  id: number;
  body: string;
  created_at: string;
}

export const listNotes = (chatId: number) =>
  api<{ items: Note[] }>("GET", `/api/chats/${chatId}/notes`);

export const createNote = (chatId: number, body: string) =>
  api<Note>("POST", `/api/chats/${chatId}/notes`, { body });

export type ChatModeValue = "AUTO_AI" | "HUMAN" | "PAUSED_UNTIL" | "DISABLED";

export const setChatMode = (
  chatId: number,
  mode: ChatModeValue,
  pauseMinutes?: number,
) =>
  api<ChatDetail>("POST", `/api/chats/${chatId}/mode`, {
    mode,
    pause_minutes: pauseMinutes ?? null,
  });

export interface ModeHistoryItem {
  from_mode: string;
  to_mode: string;
  by_admin: boolean;
  reason: string | null;
  paused_until: string | null;
  created_at: string;
}

export const getModeHistory = (chatId: number) =>
  api<{ items: ModeHistoryItem[] }>("GET", `/api/chats/${chatId}/mode-history`);

export interface AiCallDetail {
  model: string;
  call_type: string;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  cost_usd: string;
  cost_kzt: string;
  latency_ms: number | null;
  cache_hit: boolean;
  instruction_version: number | null;
  reasoning_effort: string | null;
  rag_chunks: { chunk_id: number; title: string | null; content: string }[];
  created_at: string;
}

export interface AiCallResponse {
  found: boolean;
  call: AiCallDetail | null;
}

export const getAiCall = (chatId: number, messageId: number) =>
  api<AiCallResponse>("GET", `/api/chats/${chatId}/messages/${messageId}/ai-call`);
