import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  attachTag,
  ChatFilter,
  ChatListItem,
  ChatMessage,
  createNote,
  createTag,
  detachTag,
  listChats,
  listMessages,
  listNotes,
  listTags,
  openChat,
  resolveChat,
  sendMessage,
} from "../api/chats";

const SOURCE_LABEL: Record<string, string> = {
  customer: "Клиент",
  ai: "AI",
  operator_panel: "Панель",
  operator_phone: "Телефон",
  system: "Система",
};

const FILTERS: { key: ChatFilter; label: string }[] = [
  { key: "all", label: "Все" },
  { key: "attention", label: "Требуют внимания" },
  { key: "unread", label: "Непрочитанные" },
  { key: "unresolved", label: "Нерешённые" },
];

function timeShort(value: string): string {
  const date = new Date(value);
  const now = new Date();
  return date.toDateString() === now.toDateString()
    ? date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isCustomer = message.direction === "in";
  const classes = ["bubble", isCustomer ? "from-customer" : "from-us", `src-${message.source}`];
  return (
    <div className={`bubble-row ${isCustomer ? "left" : "right"}`}>
      <div className={classes.join(" ")}>
        <div className="bubble-meta">
          <span>{SOURCE_LABEL[message.source] ?? message.source}</span>
          <span>{timeShort(message.sent_at ?? message.created_at)}</span>
        </div>
        {message.media_type && (
          <div className="media-hint">[{message.media_type}]</div>
        )}
        {message.body && <div>{message.body}</div>}
        {message.transcript && <div className="muted">🎤 {message.transcript}</div>}
        {message.delivery_status === "skipped_dry_run" && (
          <div className="dry-run-hint">DRY RUN — не отправлено</div>
        )}
        {message.error_code && (
          <div className="error">Ошибка: {message.error_code}</div>
        )}
      </div>
    </div>
  );
}

export default function ChatsPage() {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ChatFilter>("all");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [showNotes, setShowNotes] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  const [tagDraft, setTagDraft] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const chatsQuery = useInfiniteQuery({
    queryKey: ["chats", filter, search],
    queryFn: ({ pageParam }) => listChats(filter, search, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 5000,
  });
  const chats: ChatListItem[] = useMemo(
    () => chatsQuery.data?.pages.flatMap((p) => p.items) ?? [],
    [chatsQuery.data],
  );

  const chatDetail = useQuery({
    queryKey: ["chat", selectedId],
    queryFn: () => openChat(selectedId!),
    enabled: selectedId !== null,
  });

  const messagesQuery = useInfiniteQuery({
    queryKey: ["messages", selectedId],
    queryFn: ({ pageParam }) =>
      listMessages(selectedId!, pageParam ? Number(pageParam) : undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: selectedId !== null,
    refetchInterval: 3000,
  });
  const messages: ChatMessage[] = useMemo(() => {
    const pages = messagesQuery.data?.pages ?? [];
    return pages
      .flatMap((p) => p.items)
      .slice()
      .sort((a, b) => a.id - b.id);
  }, [messagesQuery.data]);

  const tagsQuery = useQuery({ queryKey: ["tags"], queryFn: listTags });
  const notesQuery = useQuery({
    queryKey: ["notes", selectedId],
    queryFn: () => listNotes(selectedId!),
    enabled: selectedId !== null && showNotes,
  });

  const send = useMutation({
    mutationFn: (body: string) =>
      sendMessage(selectedId!, body, crypto.randomUUID()),
    onSuccess: () => {
      setDraft("");
      void queryClient.invalidateQueries({ queryKey: ["messages", selectedId] });
      void queryClient.invalidateQueries({ queryKey: ["chats"] });
    },
  });

  const toggleResolve = useMutation({
    mutationFn: () => resolveChat(selectedId!, !(chatDetail.data?.is_resolved ?? false)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["chat", selectedId] });
      void queryClient.invalidateQueries({ queryKey: ["chats"] });
    },
  });

  const addNote = useMutation({
    mutationFn: () => createNote(selectedId!, noteDraft),
    onSuccess: () => {
      setNoteDraft("");
      void queryClient.invalidateQueries({ queryKey: ["notes", selectedId] });
    },
  });

  const addTag = useMutation({
    mutationFn: async () => {
      const name = tagDraft.trim();
      const existing = tagsQuery.data?.items.find(
        (t) => t.name.toLowerCase() === name.toLowerCase(),
      );
      const tag = existing ?? (await createTag(name));
      await attachTag(selectedId!, tag.id);
    },
    onSuccess: () => {
      setTagDraft("");
      void queryClient.invalidateQueries({ queryKey: ["tags"] });
      void queryClient.invalidateQueries({ queryKey: ["chat", selectedId] });
    },
  });

  const removeTag = useMutation({
    mutationFn: (tagId: number) => detachTag(selectedId!, tagId),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["chat", selectedId] }),
  });

  // Autoscroll to the newest message.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, selectedId]);

  // Keyboard navigation across the chat list (SPEC §12: обязательна).
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      if (chats.length === 0) return;
      const index = chats.findIndex((c) => c.id === selectedId);
      const nextIndex =
        event.key === "ArrowDown"
          ? Math.min(index + 1, chats.length - 1)
          : Math.max(index - 1, 0);
      setSelectedId(chats[nextIndex].id);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chats, selectedId]);

  function onDraftKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (draft.trim() && !send.isPending) send.mutate(draft.trim());
    }
  }

  const detail = chatDetail.data;

  return (
    <div className="chats-layout">
      <aside className="chat-list-pane">
        <input
          placeholder="Поиск: номер, имя, текст…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="filter-chips">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={filter === f.key ? "chip active" : "chip"}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="chat-list" ref={listRef}>
          {chats.map((chat) => (
            <button
              key={chat.id}
              className={`chat-item ${chat.id === selectedId ? "selected" : ""}`}
              onClick={() => setSelectedId(chat.id)}
            >
              <div className="chat-item-top">
                <span className="chat-name">
                  {chat.display_name ?? chat.phone}
                </span>
                <span className="muted">{timeShort(chat.last_message_at)}</span>
              </div>
              <div className="chat-item-bottom">
                <span className="preview muted">
                  {chat.last_message_preview ?? "—"}
                </span>
                {chat.unread_count > 0 && (
                  <span className="badge unread">{chat.unread_count}</span>
                )}
              </div>
              <div className="chat-item-flags">
                {chat.needs_attention && (
                  <span className="badge attention">требует внимания</span>
                )}
                {chat.is_resolved && <span className="tag ok">решено</span>}
                {chat.tags.map((t) => (
                  <span key={t.id} className="tag">
                    {t.name}
                  </span>
                ))}
              </div>
            </button>
          ))}
          {chats.length === 0 && !chatsQuery.isPending && (
            <div className="muted empty">Чатов нет</div>
          )}
          {chatsQuery.hasNextPage && (
            <button
              className="load-more"
              disabled={chatsQuery.isFetchingNextPage}
              onClick={() => void chatsQuery.fetchNextPage()}
            >
              Показать ещё
            </button>
          )}
        </div>
      </aside>

      <section className="chat-pane">
        {selectedId === null || !detail ? (
          <div className="muted empty">Выберите чат слева (↑/↓ и Enter)</div>
        ) : (
          <>
            <header className="chat-header">
              <div>
                <div className="chat-name">
                  {detail.display_name ?? detail.phone}
                </div>
                <div className="muted">{detail.phone}</div>
              </div>
              <div className="chat-header-actions">
                <span className="muted">
                  {detail.total_cost_usd > 0
                    ? `$${detail.total_cost_usd.toFixed(4)}`
                    : ""}
                </span>
                <button onClick={() => setShowNotes((v) => !v)}>
                  Заметки
                </button>
                <button
                  className={detail.is_resolved ? "" : "primary"}
                  disabled={toggleResolve.isPending}
                  onClick={() => toggleResolve.mutate()}
                >
                  {detail.is_resolved ? "Открыть заново" : "Решено"}
                </button>
              </div>
            </header>

            <div className="chat-tags-row">
              {detail.tags.map((t) => (
                <span key={t.id} className="tag">
                  {t.name}{" "}
                  <button
                    className="tag-x"
                    title="Убрать тег"
                    onClick={() => removeTag.mutate(t.id)}
                  >
                    ×
                  </button>
                </span>
              ))}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (tagDraft.trim() && !addTag.isPending) addTag.mutate();
                }}
              >
                <input
                  className="tag-input"
                  placeholder="+ тег"
                  value={tagDraft}
                  onChange={(e) => setTagDraft(e.target.value)}
                />
              </form>
            </div>

            {showNotes && (
              <div className="notes-panel">
                <div className="notes-list">
                  {(notesQuery.data?.items ?? []).map((n) => (
                    <div key={n.id} className="note">
                      <span className="muted">{timeShort(n.created_at)}</span>{" "}
                      {n.body}
                    </div>
                  ))}
                </div>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (noteDraft.trim() && !addNote.isPending) addNote.mutate();
                  }}
                >
                  <input
                    placeholder="Внутренняя заметка (клиент не видит)…"
                    value={noteDraft}
                    onChange={(e) => setNoteDraft(e.target.value)}
                  />
                </form>
              </div>
            )}

            <div className="messages-scroll">
              {messagesQuery.hasNextPage && (
                <button
                  className="load-more"
                  onClick={() => void messagesQuery.fetchNextPage()}
                >
                  Более ранние сообщения
                </button>
              )}
              {messages.map((m) => (
                <MessageBubble key={m.id} message={m} />
              ))}
              <div ref={bottomRef} />
            </div>

            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                if (draft.trim() && !send.isPending) send.mutate(draft.trim());
              }}
            >
              <textarea
                placeholder="Сообщение… (Ctrl+Enter — отправить)"
                value={draft}
                rows={2}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={onDraftKeyDown}
              />
              <button className="primary" disabled={send.isPending || !draft.trim()}>
                Отправить
              </button>
            </form>
            {send.isError && (
              <div className="error">Не удалось отправить сообщение</div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
