import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  activateFaq,
  addFaqPattern,
  AnswerStatus,
  createDocument,
  createFaq,
  deleteDocument,
  documentLog,
  FaqEntry,
  getDocument,
  KbLang,
  languageMisses,
  listChunks,
  listDocuments,
  listFaq,
  listTranslations,
  reindexDocument,
  searchPreview,
  TranslationRow,
  updateDocument,
  updateFaqAnswer,
} from "../api/kb";
import { ApiError } from "../api/client";

type Tab = "documents" | "faq" | "translations" | "search";

const TABS: { key: Tab; label: string }[] = [
  { key: "documents", label: "Документы" },
  { key: "faq", label: "Прямые ответы (FAQ)" },
  { key: "translations", label: "Проверка переводов" },
  { key: "search", label: "Проверка поиска" },
];

const INDEX_LABEL: Record<string, string> = {
  not_indexed: "не индексируется",
  pending: "в очереди",
  indexed: "проиндексирован",
  failed: "ошибка",
};

const STATUS_LABEL: Record<AnswerStatus, string> = {
  absent: "нет",
  machine: "черновик",
  approved: "утверждён",
};

function statusChip(status: AnswerStatus) {
  const cls =
    status === "approved" ? "tag ok" : status === "machine" ? "tag warn" : "tag";
  return <span className={cls}>{STATUS_LABEL[status]}</span>;
}

/* ------------------------------ Documents ------------------------------ */

function DocumentsTab() {
  const queryClient = useQueryClient();
  const documents = useQuery({ queryKey: ["kb-docs"], queryFn: () => listDocuments() });
  const [openId, setOpenId] = useState<number | null>(null);
  const [draft, setDraft] = useState({ title: "", body: "", category: "" });
  const [error, setError] = useState("");

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["kb-docs"] });
    void queryClient.invalidateQueries({ queryKey: ["kb-doc", openId] });
  };

  const create = useMutation({
    mutationFn: () =>
      createDocument({
        title: draft.title,
        body: draft.body,
        category: draft.category || null,
      }),
    onSuccess: () => {
      setDraft({ title: "", body: "", category: "" });
      setError("");
      invalidate();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Ошибка"),
  });

  const patch = useMutation({
    mutationFn: (args: { id: number; payload: Parameters<typeof updateDocument>[1] }) =>
      updateDocument(args.id, args.payload),
    onSuccess: invalidate,
  });
  const reindex = useMutation({ mutationFn: reindexDocument, onSuccess: invalidate });
  const remove = useMutation({
    mutationFn: deleteDocument,
    onSuccess: () => {
      setOpenId(null);
      invalidate();
    },
  });

  const detail = useQuery({
    queryKey: ["kb-doc", openId],
    queryFn: () => getDocument(openId!),
    enabled: openId !== null,
  });
  const chunks = useQuery({
    queryKey: ["kb-chunks", openId],
    queryFn: () => listChunks(openId!),
    enabled: openId !== null,
  });
  const log = useQuery({
    queryKey: ["kb-log", openId],
    queryFn: () => documentLog(openId!),
    enabled: openId !== null,
  });

  return (
    <>
      <div className="panel">
        <h2>Документы базы знаний</h2>
        <table>
          <thead>
            <tr>
              <th>Название</th>
              <th>Категория</th>
              <th>Статус</th>
              <th>Индексация</th>
              <th>Чанки</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(documents.data?.items ?? []).map((doc) => (
              <tr key={doc.id}>
                <td>
                  <button className="link-btn" onClick={() => setOpenId(doc.id)}>
                    {doc.title}
                  </button>
                </td>
                <td className="muted">{doc.category ?? "—"}</td>
                <td>
                  <select
                    value={doc.status}
                    onChange={(e) =>
                      patch.mutate({
                        id: doc.id,
                        payload: { status: e.target.value as never },
                      })
                    }
                  >
                    <option value="draft">черновик</option>
                    <option value="active">активен</option>
                    <option value="archived">архив</option>
                  </select>
                </td>
                <td>
                  <span className={doc.index_state === "failed" ? "tag danger" : "tag"}>
                    {INDEX_LABEL[doc.index_state] ?? doc.index_state}
                  </span>
                  {doc.index_error && (
                    <div className="error" style={{ marginTop: 4 }}>
                      {doc.index_error}
                    </div>
                  )}
                </td>
                <td>{doc.chunk_count}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <label className="switch-inline">
                    <input
                      type="checkbox"
                      checked={doc.is_searchable}
                      onChange={(e) =>
                        patch.mutate({
                          id: doc.id,
                          payload: { is_searchable: e.target.checked },
                        })
                      }
                    />
                    в поиске
                  </label>
                  <button onClick={() => reindex.mutate(doc.id)}>Переиндексировать</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {documents.data?.items.length === 0 && (
          <p className="muted">
            Документов нет. Импортируйте базу знаний командой <code>make kb-import</code>{" "}
            или добавьте вручную ниже.
          </p>
        )}
      </div>

      {openId !== null && detail.data && (
        <div className="panel">
          <h2>{detail.data.title}</h2>
          <label htmlFor="doc-body">Текст документа (версия {detail.data.version})</label>
          <textarea
            id="doc-body"
            rows={10}
            defaultValue={detail.data.body}
            onBlur={(e) =>
              e.target.value !== detail.data?.body &&
              patch.mutate({ id: openId, payload: { body: e.target.value } })
            }
          />
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            <button onClick={() => setOpenId(null)}>Закрыть</button>
            <button className="danger" onClick={() => remove.mutate(openId)}>
              Удалить
            </button>
          </div>

          <h2 style={{ marginTop: 20 }}>Чанки ({chunks.data?.items.length ?? 0})</h2>
          {(chunks.data?.items ?? []).map((chunk) => (
            <div key={chunk.id} className="chunk">
              <div className="muted">
                #{chunk.chunk_index} · {chunk.token_count} токенов · {chunk.embedding_model}
              </div>
              {chunk.content}
            </div>
          ))}
          {chunks.data?.items.length === 0 && (
            <p className="muted">
              Чанков нет: документ индексируется только когда он активен и включён в поиск.
            </p>
          )}

          <h2 style={{ marginTop: 20 }}>Лог обработки</h2>
          <table>
            <tbody>
              {(log.data?.items ?? []).map((row, i) => (
                <tr key={i}>
                  <td className="muted">
                    {new Date(row.created_at).toLocaleString("ru-RU")}
                  </td>
                  <td>{row.step}</td>
                  <td>{row.status}</td>
                  <td className="muted">{row.detail}</td>
                  <td>{row.tokens_used || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <h2>Новый документ</h2>
        <form
          className="narrow"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <label htmlFor="new-title">Название</label>
          <input
            id="new-title"
            value={draft.title}
            onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            required
          />
          <label htmlFor="new-cat">Категория</label>
          <input
            id="new-cat"
            placeholder="gym / rental / org…"
            value={draft.category}
            onChange={(e) => setDraft({ ...draft, category: e.target.value })}
          />
          <label htmlFor="new-body">Текст</label>
          <textarea
            id="new-body"
            rows={5}
            value={draft.body}
            onChange={(e) => setDraft({ ...draft, body: e.target.value })}
            required
          />
          <div style={{ marginTop: 14 }}>
            <button className="primary" disabled={create.isPending}>
              Создать черновик
            </button>
          </div>
          {error && <div className="error">{error}</div>}
        </form>
      </div>
    </>
  );
}

/* --------------------------------- FAQ --------------------------------- */

function FaqRow({ faq }: { faq: FaqEntry }) {
  const queryClient = useQueryClient();
  const [patternDraft, setPatternDraft] = useState("");
  const [patternLang, setPatternLang] = useState<KbLang>("ru");
  const [error, setError] = useState("");
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["kb-faq"] });

  const toggle = useMutation({
    mutationFn: () => activateFaq(faq.id, !faq.is_active),
    onSuccess: () => {
      setError("");
      invalidate();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Ошибка"),
  });
  const addPattern = useMutation({
    mutationFn: () => addFaqPattern(faq.id, patternLang, patternDraft),
    onSuccess: () => {
      setPatternDraft("");
      invalidate();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Ошибка"),
  });

  return (
    <div className="faq-card">
      <div className="faq-head">
        <strong>{faq.normalized_key}</strong>
        <span className="muted">{faq.category ?? "—"}</span>
        <span className="spacer" />
        {faq.is_active ? (
          <span className="tag ok">активна</span>
        ) : (
          <span className="tag">выключена</span>
        )}
        <button onClick={() => toggle.mutate()} disabled={toggle.isPending}>
          {faq.is_active ? "Выключить" : "Включить"}
        </button>
      </div>
      <table>
        <tbody>
          {(["ru", "kk", "en"] as KbLang[]).map((lang) => (
            <tr key={lang}>
              <td style={{ width: 40 }}>
                <strong>{lang}</strong>
              </td>
              <td>{statusChip(faq[`answer_${lang}_status`] as AnswerStatus)}</td>
              <td className="muted">
                {faq[`answer_${lang}`] ?? <em>нет текста</em>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="faq-patterns">
        {faq.patterns.map((p) => (
          <span key={p.id} className="tag">
            {p.language}: {p.pattern}
          </span>
        ))}
      </div>
      <form
        className="faq-pattern-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (patternDraft.trim()) addPattern.mutate();
        }}
      >
        <select
          value={patternLang}
          onChange={(e) => setPatternLang(e.target.value as KbLang)}
        >
          <option value="ru">ru</option>
          <option value="kk">kk</option>
          <option value="en">en</option>
        </select>
        <input
          placeholder="+ формулировка вопроса"
          value={patternDraft}
          onChange={(e) => setPatternDraft(e.target.value)}
        />
      </form>
      {error && <div className="error">{error}</div>}
    </div>
  );
}

function FaqTab() {
  const queryClient = useQueryClient();
  const faq = useQuery({ queryKey: ["kb-faq"], queryFn: listFaq });
  const [draft, setDraft] = useState({
    key: "",
    category: "",
    answer: "",
    ru: "",
    kk: "",
  });
  const [error, setError] = useState("");

  const create = useMutation({
    mutationFn: () =>
      createFaq({
        normalized_key: draft.key,
        category: draft.category || null,
        answer_ru: draft.answer,
        patterns_ru: draft.ru.split("\n").map((s) => s.trim()).filter(Boolean),
        patterns_kk: draft.kk.split("\n").map((s) => s.trim()).filter(Boolean),
      }),
    onSuccess: () => {
      setDraft({ key: "", category: "", answer: "", ru: "", kk: "" });
      setError("");
      void queryClient.invalidateQueries({ queryKey: ["kb-faq"] });
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Ошибка"),
  });

  return (
    <>
      <div className="panel">
        <h2>Прямые ответы</h2>
        <p className="muted">
          Отвечают без обращения к модели. Сюда попадает только то, что меняется реже
          раза в год: адрес, телефоны, график, как добраться. Цены, наличие мест и
          акции — только через документы и RAG.
        </p>
        {(faq.data?.items ?? []).map((entry) => (
          <FaqRow key={entry.id} faq={entry} />
        ))}
        {faq.data?.items.length === 0 && <p className="muted">Записей пока нет.</p>}
      </div>

      <div className="panel">
        <h2>Новая запись</h2>
        <form
          className="narrow"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <label htmlFor="faq-key">Ключ (латиницей)</label>
          <input
            id="faq-key"
            placeholder="address"
            value={draft.key}
            onChange={(e) => setDraft({ ...draft, key: e.target.value })}
            required
          />
          <label htmlFor="faq-cat">Категория</label>
          <input
            id="faq-cat"
            value={draft.category}
            onChange={(e) => setDraft({ ...draft, category: e.target.value })}
          />
          <label htmlFor="faq-answer">Ответ на русском (источник истины)</label>
          <textarea
            id="faq-answer"
            rows={3}
            value={draft.answer}
            onChange={(e) => setDraft({ ...draft, answer: e.target.value })}
            required
          />
          <label htmlFor="faq-ru">Формулировки на русском (по одной в строке)</label>
          <textarea
            id="faq-ru"
            rows={3}
            value={draft.ru}
            onChange={(e) => setDraft({ ...draft, ru: e.target.value })}
            required
          />
          <label htmlFor="faq-kk">Формулировки на казахском (обязательны)</label>
          <textarea
            id="faq-kk"
            rows={3}
            value={draft.kk}
            onChange={(e) => setDraft({ ...draft, kk: e.target.value })}
            required
          />
          <div style={{ marginTop: 14 }}>
            <button className="primary" disabled={create.isPending}>
              Создать
            </button>
          </div>
          {error && <div className="error">{error}</div>}
        </form>
      </div>
    </>
  );
}

/* --------------------------- Translations --------------------------- */

function TranslationRowCard({ row }: { row: TranslationRow }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(row.draft ?? "");
  const [error, setError] = useState("");
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["kb-translations"] });
    void queryClient.invalidateQueries({ queryKey: ["kb-faq"] });
  };
  const save = useMutation({
    mutationFn: (status: AnswerStatus) =>
      updateFaqAnswer(row.faq_id, row.language, status === "absent" ? null : text, status),
    onSuccess: invalidate,
    onError: (e) => setError(e instanceof ApiError ? e.message : "Ошибка"),
  });

  return (
    <div className="translation-row">
      <div className="translation-cell">
        <div className="muted">
          {row.normalized_key} · ru (источник)
          {row.miss_count > 0 && (
            <span className="tag warn" style={{ marginLeft: 8 }}>
              промахов: {row.miss_count}
            </span>
          )}
        </div>
        <div className="source-text">{row.source_ru}</div>
      </div>
      <div className="translation-cell">
        <div className="muted">
          {row.language} · {statusChip(row.status)}
        </div>
        <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
        <div className="translation-actions">
          <button
            className="primary"
            disabled={save.isPending || !text.trim()}
            onClick={() => save.mutate("approved")}
          >
            Утвердить
          </button>
          <button disabled={save.isPending} onClick={() => save.mutate("machine")}>
            Сохранить как черновик
          </button>
          <button
            className="danger"
            disabled={save.isPending}
            onClick={() => save.mutate("absent")}
          >
            Удалить черновик
          </button>
        </div>
        {error && <div className="error">{error}</div>}
      </div>
    </div>
  );
}

function TranslationsTab() {
  const [language, setLanguage] = useState<KbLang>("kk");
  const [status, setStatus] = useState("machine");
  const rows = useQuery({
    queryKey: ["kb-translations", language, status],
    queryFn: () => listTranslations(language, status),
  });
  const misses = useQuery({ queryKey: ["kb-misses"], queryFn: languageMisses });

  return (
    <>
      <div className="panel">
        <h2>Проверка переводов</h2>
        <p className="muted">
          Машинный черновик никогда не отдаётся клиенту — только после того, как вы
          нажали «Утвердить». Утверждение только по одной записи: массового нет
          намеренно.
        </p>
        <div className="filter-chips" style={{ marginBottom: 14 }}>
          {(["kk", "en"] as KbLang[]).map((l) => (
            <button
              key={l}
              className={language === l ? "chip-btn active" : "chip-btn"}
              onClick={() => setLanguage(l)}
            >
              {l}
            </button>
          ))}
          <span style={{ width: 12 }} />
          {["machine", "absent", "approved", "all"].map((s) => (
            <button
              key={s}
              className={status === s ? "chip-btn active" : "chip-btn"}
              onClick={() => setStatus(s)}
            >
              {STATUS_LABEL[s as AnswerStatus] ?? "все"}
            </button>
          ))}
        </div>
        {(rows.data?.items ?? []).map((row) => (
          <TranslationRowCard key={`${row.faq_id}-${row.language}`} row={row} />
        ))}
        {rows.data?.items.length === 0 && <p className="muted">Записей нет.</p>}
      </div>

      <div className="panel">
        <h2>Промахи по языку</h2>
        <p className="muted">
          Сколько вопросов ушло в модель, потому что утверждённого ответа на языке
          клиента не было. Это и есть список того, что переводить дальше.
        </p>
        <table>
          <thead>
            <tr>
              <th>Язык</th>
              <th>Запись</th>
              <th>Категория</th>
              <th>Промахов</th>
            </tr>
          </thead>
          <tbody>
            {(misses.data?.items ?? []).map((row, i) => (
              <tr key={i}>
                <td>{row.language}</td>
                <td>{row.normalized_key ?? "—"}</td>
                <td className="muted">{row.category ?? "—"}</td>
                <td>{row.misses}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {misses.data?.total === 0 && <p className="muted">Промахов пока нет.</p>}
      </div>
    </>
  );
}

/* ------------------------------- Search ------------------------------- */

function SearchTab() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const result = useQuery({
    queryKey: ["kb-search", submitted],
    queryFn: () => searchPreview(submitted),
    enabled: submitted.length > 0,
  });

  return (
    <div className="panel">
      <h2>Проверка поиска</h2>
      <p className="muted">
        Показывает, что именно получит модель по этому вопросу. Сам ответ модели не
        генерируется — только прямой FAQ и найденные фрагменты.
      </p>
      <form
        className="narrow"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(query.trim());
        }}
      >
        <label htmlFor="q">Вопрос клиента</label>
        <input id="q" value={query} onChange={(e) => setQuery(e.target.value)} required />
        <div style={{ marginTop: 12 }}>
          <button className="primary">Проверить</button>
        </div>
      </form>

      {result.data && (
        <div style={{ marginTop: 18 }}>
          <p>
            Определён язык: <strong>{result.data.detected_language}</strong>
          </p>
          {result.data.faq_hit && (
            <div className="plaque ok-plaque">
              <strong>Прямой ответ FAQ (0 токенов):</strong> {result.data.faq_hit}
            </div>
          )}
          {result.data.faq_language_miss && (
            <div className="plaque warn">
              Формулировка найдена, но утверждённого ответа на этом языке нет — вопрос
              уйдёт в модель, и это записано как промах.
            </div>
          )}
          <h2 style={{ marginTop: 16 }}>Фрагменты ({result.data.hits.length})</h2>
          {result.data.hits.map((hit) => (
            <div key={hit.chunk_id} className="chunk">
              <div className="muted">
                {hit.title} · релевантность {hit.score}
              </div>
              {hit.content}
            </div>
          ))}
          {result.data.hits.length === 0 && (
            <p className="muted">
              Фрагментов нет: либо ничего не прошло порог релевантности, либо не
              настроен ключ OpenAI (тогда индексация и поиск отключены).
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default function KnowledgePage() {
  const [tab, setTab] = useState<Tab>("documents");
  return (
    <div className="page-narrow">
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={tab === t.key ? "tab active" : "tab"}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "documents" && <DocumentsTab />}
      {tab === "faq" && <FaqTab />}
      {tab === "translations" && <TranslationsTab />}
      {tab === "search" && <SearchTab />}
    </div>
  );
}
