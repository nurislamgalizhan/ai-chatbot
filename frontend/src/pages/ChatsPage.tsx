import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  attachTag,
  ChatDetail,
  ChatFilter,
  ChatListItem,
  ChatMessage,
  ChatModeValue,
  setChatMode,
  createNote,
  createTag,
  detachTag,
  getAiCall,
  listChats,
  listMessages,
  listNotes,
  listTags,
  getChat,
  markChatOpened,
  resolveChat,
  sendMessage,
} from "../api/chats";
import Icon, { IconName } from "../components/icons";
import { messageReason } from "../components/reasons";

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

const PAUSE_PRESETS = [15, 30, 60, 120];

function modeLabel(mode: string, pausedUntil: string | null): string {
  switch (mode) {
    case "AUTO_AI":
      return "AI-агент активен";
    case "PAUSED_UNTIL":
      return pausedUntil ? `Пауза до ${timeShort(pausedUntil)}` : "Пауза";
    case "HUMAN":
      return "Оператор";
    default:
      return "AI отключён";
  }
}

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

/** "Почему так": usage, cost and the KB fragments behind one AI reply.
 *  Loaded on demand; viewing a chat NEVER calls OpenAI (invariant 16). */
function WhySoBlock({ chatId, messageId }: { chatId: number; messageId: number }) {
  const detail = useQuery({
    queryKey: ["ai-call", chatId, messageId],
    queryFn: () => getAiCall(chatId, messageId),
    staleTime: Infinity,
  });
  if (detail.isPending) return <div className="why-so muted">Загрузка…</div>;
  if (detail.isError || !detail.data)
    return <div className="why-so muted">Не удалось загрузить данные вызова</div>;
  if (!detail.data.found || !detail.data.call) {
    return (
      <div className="why-so muted">
        Нет данных вызова: ответ дан из FAQ, кэша или шаблона — модель не
        вызывалась, расходов нет.
      </div>
    );
  }
  const call = detail.data.call;
  return (
    <div className="why-so">
      <dl className="kv">
        <dt>Модель</dt>
        <dd>
          {call.model}
          {call.instruction_version != null && ` · инструкция v${call.instruction_version}`}
          {call.reasoning_effort && ` · effort ${call.reasoning_effort}`}
        </dd>
        <dt>Токены</dt>
        <dd>
          вход {call.input_tokens} (кэш {call.cached_input_tokens}), выход{" "}
          {call.output_tokens}, reasoning {call.reasoning_tokens}
        </dd>
        <dt>Стоимость</dt>
        <dd>
          {call.cost_kzt} ₸ (${call.cost_usd})
        </dd>
        {call.latency_ms != null && (
          <>
            <dt>Время</dt>
            <dd>{call.latency_ms} мс</dd>
          </>
        )}
      </dl>
      {call.rag_chunks.length > 0 && (
        <details>
          <summary>Использованные фрагменты ({call.rag_chunks.length})</summary>
          {call.rag_chunks.map((chunk) => (
            <div key={chunk.chunk_id} className="why-so-chunk">
              <strong>{chunk.title ?? `Фрагмент ${chunk.chunk_id}`}</strong>
              <div className="muted">{chunk.content}</div>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}

const MEDIA_LABELS: Record<string, string> = {
  voice: "голосовое сообщение",
  image: "фотография",
  video: "видео",
  gif: "GIF",
  document: "документ",
  sticker: "стикер",
  other: "файл",
};

/* Files are served by the API, never linked from a filesystem path: the
 * endpoint resolves chat+message ids itself and sends the bytes as an
 * attachment. Only voice notes and photos are ever downloaded by the
 * bot; documents, video and stickers stay on the manager's phone, and a
 * location or contact card is already described in the message text —
 * none of those may be labelled «удалён по сроку хранения». */
function mediaNote(message: ChatMessage): string | null {
  const processable = message.media_type === "voice" || message.media_type === "image";
  if (processable) {
    return message.transcript
      ? "файл удалён по сроку хранения"
      : "файл ботом не сохранён — есть только на телефоне";
  }
  return "бот такие файлы не скачивает — файл на телефоне менеджера";
}

function MediaBlock({ chatId, message }: { chatId: number; message: ChatMessage }) {
  if (message.media_type === "other") return null; // the body describes it
  const label = MEDIA_LABELS[message.media_type ?? "other"] ?? "файл";
  const href = `/api/chats/${chatId}/messages/${message.id}/media`;
  return (
    <div className="media-hint">
      <Icon name="paperclip" size={14} /> {label}
      {message.has_media ? (
        <>
          {" — "}
          <a href={href} target="_blank" rel="noreferrer">
            скачать
          </a>
        </>
      ) : (
        <span className="muted"> — {mediaNote(message)}</span>
      )}
    </div>
  );
}

function MessageBlock({
  message,
  peerName,
  chatId,
}: {
  message: ChatMessage;
  peerName: string;
  chatId: number;
}) {
  const isCustomer = message.direction === "in";
  const [showWhy, setShowWhy] = useState(false);
  const reason = messageReason(message.error_code, message.state);
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
        {message.source === "ai" && (
          <button
            type="button"
            className="why-so-toggle"
            title="Почему так: расход, токены, использованные фрагменты"
            onClick={() => setShowWhy((v) => !v)}
          >
            <Icon name="info" size={14} /> почему так
          </button>
        )}
      </div>
      {message.media_type && message.error_code !== "reaction" && (
        <MediaBlock chatId={chatId} message={message} />
      )}
      {message.body && <div>{message.body}</div>}
      {message.transcript && (
        <div className="muted">
          {message.media_type === "image" ? "🖼 " : "🎤 "}
          {message.transcript}
        </div>
      )}
      {message.delivery_status === "skipped_dry_run" && (
        <div className="plaque warn">
          <strong>DRY RUN:</strong> сообщение обработано, но не отправлено
        </div>
      )}
      {reason && <div className={`plaque ${reason.tone}`}>{reason.text}</div>}
      {showWhy && <WhySoBlock chatId={chatId} messageId={message.id} />}
    </div>
  );
}

function ModeControl({
  detail,
  onChanged,
}: {
  detail: ChatDetail;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [customMin, setCustomMin] = useState("");
  const mutate = useMutation({
    mutationFn: ({ mode, minutes }: { mode: ChatModeValue; minutes?: number }) =>
      setChatMode(detail.id, mode, minutes),
    onSuccess: () => {
      setOpen(false);
      setCustomMin("");
      onChanged();
    },
  });

  return (
    <div className="mode-control">
      <button
        className={`mode-pill mode-${detail.mode}`}
        onClick={() => setOpen((v) => !v)}
        title="Режим AI в этом чате"
      >
        <span className="dot" />
        {modeLabel(detail.mode, detail.paused_until)}
        <Icon name="chevron-down" size={13} />
      </button>
      {open && (
        <div className="menu">
          <button onClick={() => mutate.mutate({ mode: "AUTO_AI" })}>
            AI-агент активен
          </button>
          <div className="menu-sep">Пауза</div>
          {PAUSE_PRESETS.map((m) => (
            <button
              key={m}
              onClick={() => mutate.mutate({ mode: "PAUSED_UNTIL", minutes: m })}
            >
              {m < 60 ? `${m} минут` : `${m / 60} час${m === 120 ? "а" : ""}`}
            </button>
          ))}
          <form
            className="menu-custom"
            onSubmit={(e) => {
              e.preventDefault();
              const minutes = Number(customMin);
              if (minutes >= 1 && minutes <= 1440) {
                mutate.mutate({ mode: "PAUSED_UNTIL", minutes });
              }
            }}
          >
            <input
              type="number"
              min={1}
              max={1440}
              placeholder="своё, мин"
              value={customMin}
              onChange={(e) => setCustomMin(e.target.value)}
            />
          </form>
          <div className="menu-sep" />
          <button onClick={() => mutate.mutate({ mode: "HUMAN" })}>
            Оператор (AI молчит)
          </button>
          <button
            className="menu-danger"
            onClick={() => mutate.mutate({ mode: "DISABLED" })}
          >
            Отключить AI в этом чате
          </button>
        </div>
      )}
    </div>
  );
}

const FILTER_KEYS = new Set<string>(["all", "attention", "unread", "unresolved"]);

export default function ChatsPage() {
  const queryClient = useQueryClient();
  // Deep links from «Обзор» (/chats?filter=attention) and «Расходы»
  // (/chats?chat=ID) land here; the page state starts from the URL.
  const [params] = useSearchParams();
  const initialFilter = params.get("filter");
  const initialChat = Number(params.get("chat"));
  const [filter, setFilter] = useState<ChatFilter>(
    initialFilter && FILTER_KEYS.has(initialFilter) ? (initialFilter as ChatFilter) : "all",
  );
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(
    Number.isInteger(initialChat) && initialChat > 0 ? initialChat : null,
  );
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
    queryFn: () => getChat(selectedId!),
    enabled: selectedId !== null,
  });
  // Opening a dialog marks it read and clears «требует внимания» — an
  // explicit POST, so that a plain link to the GET cannot do it.
  useEffect(() => {
    if (selectedId === null) return;
    void markChatOpened(selectedId).then(() => {
      void queryClient.invalidateQueries({ queryKey: ["chat", selectedId] });
      void queryClient.invalidateQueries({ queryKey: ["chats"] });
    }).catch(() => undefined);
  }, [selectedId, queryClient]);

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
                    {chat.mode !== "AUTO_AI" && (
                      <span className={`tag mode-tag mode-${chat.mode}`}>
                        {modeLabel(chat.mode, chat.paused_until)}
                      </span>
                    )}
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
                <ModeControl
                  detail={detail}
                  onChanged={() => {
                    void queryClient.invalidateQueries({
                      queryKey: ["chat", selectedId],
                    });
                    void queryClient.invalidateQueries({ queryKey: ["chats"] });
                  }}
                />
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
                <MessageBlock key={m.id} message={m} peerName={peerName} chatId={selectedId!} />
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
