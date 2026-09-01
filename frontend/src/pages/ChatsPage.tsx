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
import Icon, { IconName } from "../components/icons";

const SOURCE_LABEL: Record<string, string> = {
  customer: "Клиент",
  ai: "AI",
  operator_panel: "Панель",
  operator_phone: "Телефон",
  system: "Система",
};

const SOURCE_ICON: Record<string, IconName> = {
  ai: "bot",
  operator_panel: "monitor",
  operator_phone: "phone",
  system: "check",
};

const FILTERS: { key: ChatFilter; label: string }[] = [
  { key: "all", label: "Все" },
  { key: "attention", label: "Требуют внимания" },
  { key: "unread", label: "Непрочитанные" },
  { key: "unresolved", label: "Нерешённые" },
];

const AVATAR_COLORS = 8;

/** djb2 hash -> stable pastel pair per name (Savvi-style avatars). */
function avatarIndex(seed: string): number {
  let hash = 5381;
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 33) ^ seed.charCodeAt(i);
  }
  return Math.abs(hash) % AVATAR_COLORS;
}

function Avatar({ seed, label }: { seed: string; label: string }) {
  const idx = avatarIndex(seed);
  return (
    <span
      className="avatar"
      style={{
        background: `var(--avatar-bg-${idx})`,
        color: `var(--avatar-fg-${idx})`,
      }}
    >
      {label.trim()[0]?.toUpperCase() ?? "?"}
    </span>
  );
}

function timeShort(value: string): string {
  const date = new Date(value);
  const now = new Date();
  return date.toDateString() === now.toDateString()
    ? date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
}

function MessageBlock({
  message,
  peerName,
}: {
  message: ChatMessage;
  peerName: string;
}) {
  const isCustomer = message.direction === "in";
  const classes = ["msg", isCustomer ? "from-customer" : "", `src-${message.source}`]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={classes}>
      <div className="msg-head">
        {isCustomer ? (
          <Avatar seed={peerName} label={peerName} />
        ) : (
          <span className="src-icon">
            <Icon name={SOURCE_ICON[message.source] ?? "check"} size={14} />
          </span>
        )}
        <span className="src-name">
          {isCustomer ? peerName : (SOURCE_LABEL[message.source] ?? message.source)}
        </span>
        <span className="msg-time">
          {timeShort(message.sent_at ?? message.created_at)}
        </span>
      </div>
      {message.media_type && <div className="media-hint">[{message.media_type}]</div>}
      {message.body && <div>{message.body}</div>}
      {message.transcript && <div className="muted">🎤 {message.transcript}</div>}
      {message.delivery_status === "skipped_dry_run" && (
        <div className="plaque warn">
          <strong>DRY RUN:</strong> сообщение обработано, но не отправлено
        </div>
      )}
      {message.error_code && (
        <div className="plaque danger">Ошибка: {message.error_code}</div>
      )}
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
    mutationFn: (body: string) => sendMessage(selectedId!, body, crypto.randomUUID()),
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
  const peerName = detail?.display_name ?? detail?.phone ?? "";

  return (
    <div className="chats-layout">
      <aside className="chat-list-pane">
        <div className="search-row">
          <span className="search-icon">
            <Icon name="search" size={16} />
          </span>
          <input
            placeholder="Поиск по диалогам…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="filter-chips">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={filter === f.key ? "chip-btn active" : "chip-btn"}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="chat-list">
          {chats.map((chat) => {
            const name = chat.display_name ?? chat.phone;
            return (
              <button
                key={chat.id}
                className={`chat-item ${chat.id === selectedId ? "selected" : ""}`}
                onClick={() => setSelectedId(chat.id)}
              >
                <Avatar seed={name} label={name} />
                <span>
                  <span className="chat-item-top">
                    <span className="chat-name">{name}</span>
                    <span className="chat-time">
                      {timeShort(chat.last_message_at)}
                    </span>
                  </span>
                  <span className="chat-item-bottom">
                    <span className="preview">
                      {chat.last_message_preview ?? "—"}
                    </span>
                    {chat.unread_count > 0 && (
                      <span className="badge unread">{chat.unread_count}</span>
                    )}
                  </span>
                  <span className="chat-item-flags">
                    {chat.needs_attention && (
                      <span className="badge attention">требует внимания</span>
                    )}
                    {chat.is_resolved && <span className="tag ok">решено</span>}
                    {chat.tags.map((t) => (
                      <span key={t.id} className="tag">
                        {t.name}
                      </span>
                    ))}
                  </span>
                </span>
              </button>
            );
          })}
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
          <div className="muted empty" style={{ margin: "auto" }}>
            Выберите чат слева (↑/↓ и Enter)
          </div>
        ) : (
          <>
            <header className="chat-header">
              <div className="peer">
                <Avatar seed={peerName} label={peerName} />
                <div className="who">
                  <div className="name">{peerName}</div>
                  <div className="sub">{detail.phone}</div>
                </div>
              </div>
              <div className="chat-header-actions">
                <span className="muted">
                  {detail.total_cost_usd > 0
                    ? `$${detail.total_cost_usd.toFixed(4)}`
                    : ""}
                </span>
                <button onClick={() => setShowNotes((v) => !v)}>
                  <Icon name="note" size={15} /> Заметки
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
                  {t.name}
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
                <MessageBlock key={m.id} message={m} peerName={peerName} />
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
              <button
                className="btn-icon-round"
                type="submit"
                aria-label="Отправить"
                title="Отправить"
                disabled={send.isPending || !draft.trim()}
              >
                <Icon name="send" size={18} />
              </button>
            </form>
            {send.isError && (
              <div className="error" style={{ padding: "0 20px 12px" }}>
                Не удалось отправить сообщение
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
