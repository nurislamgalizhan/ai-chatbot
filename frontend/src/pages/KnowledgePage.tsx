import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  activateFaq,
  addFaqPattern,
  createDocument,
  createFaq,
  deleteDocument,
  deleteFaq,
  deleteFaqPattern,
  type FaqEntry,
  getDocument,
  type KbDocument,
  type KbLang,
  listDocuments,
  listFaq,
  searchPreview,
  type SearchResult,
  suggestPatterns,
  translateFaq,
  updateDocument,
  updateFaq,
  updateFaqAnswer,
} from "../api/kb";
import { ApiError } from "../api/client";
import Icon from "../components/icons";

/* The section has exactly two things in it, and the owner should be able
 * to tell them apart without reading documentation: exact answers the
 * bot repeats word for word, and long texts it composes answers from.
 * Everything else (translations, phrasings, checking, indexing state)
 * lives inside one record — it is not a section of its own. */
type Tab = "qa" | "docs";

const LANGUAGES: { code: KbLang; label: string }[] = [
  { code: "ru", label: "Русский" },
  { code: "kk", label: "Қазақша" },
  { code: "en", label: "English" },
];

/** The row title is the first Russian phrasing — shown as a sentence,
 *  while the stored pattern keeps the exact text matching relies on. */
function asTitle(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function errorText(error: unknown): string | null {
  if (error instanceof ApiError) return error.message;
  return error ? "Не удалось выполнить действие" : null;
}

/* ------------------------------ shared bits ----------------------------- */

function Toggle({
  checked,
  onChange,
  disabled,
  title,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      className={checked ? "switch on" : "switch"}
      disabled={disabled}
      title={title}
      aria-pressed={checked}
      onClick={(e) => {
        e.stopPropagation();
        onChange(!checked);
      }}
    >
      <span className="knob" />
    </button>
  );
}

function RowMenu({ items }: { items: { label: string; danger?: boolean; run: () => void }[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="row-menu" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="icon-btn" onClick={() => setOpen((v) => !v)} title="Ещё">
        ⋮
      </button>
      {open && (
        <>
          <div className="menu-backdrop" onClick={() => setOpen(false)} />
          <div className="menu">
            {items.map((item) => (
              <button
                key={item.label}
                type="button"
                className={item.danger ? "danger" : ""}
                onClick={() => {
                  setOpen(false);
                  item.run();
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/** Paid actions always say so — the same rule as the sandbox and limits. */
function AiButton({
  label,
  onClick,
  busy,
}: {
  label: string;
  onClick: () => void;
  busy?: boolean;
}) {
  return (
    <button type="button" className="ai-btn" onClick={onClick} disabled={busy}>
      <Icon name="sparkle" size={15} /> {busy ? "Готовлю…" : label}
    </button>
  );
}

/* ------------------------------ FAQ drawer ------------------------------ */

const STATUS_NOTE: Record<string, { text: string; tone: string }> = {
  absent: {
    text: "Перевода нет. Если клиент напишет на этом языке, отвечать будет ИИ — дольше и платно.",
    tone: "muted",
  },
  machine: {
    text: "Черновик перевода. Клиент его НЕ получит, пока вы не нажмёте «Утвердить».",
    tone: "warn",
  },
  approved: { text: "Утверждено — бот отвечает этим текстом.", tone: "ok" },
};

function FaqDrawer({ entry, onClose }: { entry: FaqEntry; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [lang, setLang] = useState<KbLang>("ru");
  const [text, setText] = useState("");
  const [phrase, setPhrase] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const answers: Record<KbLang, string | null> = {
    ru: entry.answer_ru,
    kk: entry.answer_kk,
    en: entry.answer_en,
  };
  const statuses: Record<KbLang, string> = {
    ru: entry.answer_ru_status,
    kk: entry.answer_kk_status,
    en: entry.answer_en_status,
  };
  // Remounting on language change keeps the textarea from ever showing
  // another language's text (the old page had exactly that bug).
  useEffect(() => setText(answers[lang] ?? ""), [entry.id, lang, entry.updated_at]);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["kb-faq"] });
  const saveRu = useMutation({
    mutationFn: () => updateFaq(entry.id, { answer_ru: text.trim() }),
    onSuccess: () => {
      setNote("Русский ответ сохранён.");
      void refresh();
    },
  });
  const saveOther = useMutation({
    mutationFn: (status: "machine" | "approved") =>
      updateFaqAnswer(entry.id, lang, text.trim() || null, status),
    onSuccess: (_data, status) => {
      setNote(status === "approved" ? "Утверждено — бот будет так отвечать." : "Черновик сохранён.");
      void refresh();
    },
  });
  const translate = useMutation({
    mutationFn: () => translateFaq(entry.id, lang as "kk" | "en"),
    onSuccess: (result) => {
      setText(result.item[`answer_${lang}` as "answer_kk"] ?? "");
      setNote(`Перевод готов (${result.cost_kzt} ₸). Проверьте и утвердите.`);
      void refresh();
    },
  });
  const suggest = useMutation({
    mutationFn: (language: KbLang) => suggestPatterns(entry.id, language),
    onSuccess: (result) => {
      setNote(`Добавлено формулировок: ${result.added} (${result.cost_kzt} ₸).`);
      void refresh();
    },
  });
  const addPhrase = useMutation({
    mutationFn: (language: KbLang) => addFaqPattern(entry.id, language, phrase.trim()),
    onSuccess: () => {
      setPhrase("");
      void refresh();
    },
  });
  const dropPhrase = useMutation({
    mutationFn: (patternId: number) => deleteFaqPattern(entry.id, patternId),
    onSuccess: () => void refresh(),
  });
  const toggle = useMutation({
    mutationFn: (value: boolean) => activateFaq(entry.id, value),
    onSuccess: () => void refresh(),
  });

  const failure =
    errorText(saveRu.error) ??
    errorText(saveOther.error) ??
    errorText(translate.error) ??
    errorText(suggest.error) ??
    errorText(addPhrase.error) ??
    errorText(dropPhrase.error) ??
    errorText(toggle.error);
  const status = statuses[lang];
  const note2 = STATUS_NOTE[status];
  const phrases = entry.patterns.filter((p) => p.language === lang);

  return (
    <div className="drawer">
      <header className="drawer-head">
        <button type="button" className="icon-btn" onClick={onClose} title="Закрыть">
          ‹
        </button>
        <h2>
          {asTitle(
            entry.patterns.find((p) => p.language === "ru")?.pattern ?? entry.normalized_key,
          )}
        </h2>
        <div className="switch-card">
          <span>Использовать</span>
          <Toggle
            checked={entry.is_active}
            disabled={!entry.can_activate && !entry.is_active}
            title={entry.can_activate ? "" : entry.blockers.join(", ")}
            onChange={(v) => toggle.mutate(v)}
          />
        </div>
      </header>

      {!entry.can_activate && (
        <div className="readiness">
          <strong>Чтобы бот отвечал этой записью, не хватает:</strong>
          <ul>
            {entry.blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="drawer-body">
        <section>
          <h3>Ответ</h3>
          <div className="segmented">
            {LANGUAGES.map((l) => (
              <button
                key={l.code}
                type="button"
                className={lang === l.code ? "active" : ""}
                onClick={() => setLang(l.code)}
              >
                {l.label}
                {statuses[l.code] === "approved" && <span className="dot ok" />}
                {statuses[l.code] === "machine" && <span className="dot warn" />}
              </button>
            ))}
          </div>

          {lang !== "ru" && (
            <div className={`plaque ${note2.tone === "ok" ? "ok-plaque" : note2.tone}`}>
              {note2.text}
            </div>
          )}
          {lang === "kk" && entry.misses_kk > 0 && (
            <p className="muted">
              На казахском спрашивали {entry.misses_kk} раз — перевод здесь нужнее всего.
            </p>
          )}

          <textarea
            rows={5}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={
              lang === "ru"
                ? "Что бот ответит клиенту слово в слово"
                : "Перевод ответа или нажмите «Перевести»"
            }
          />
          <div className="actions-row">
            {lang === "ru" ? (
              <button
                className="primary"
                disabled={saveRu.isPending || !text.trim()}
                onClick={() => saveRu.mutate()}
              >
                Сохранить ответ
              </button>
            ) : (
              <>
                <AiButton
                  label="Перевести с русского"
                  busy={translate.isPending}
                  onClick={() => translate.mutate()}
                />
                <button
                  disabled={saveOther.isPending || !text.trim()}
                  onClick={() => saveOther.mutate("machine")}
                >
                  Сохранить черновик
                </button>
                <button
                  className="primary"
                  disabled={saveOther.isPending || !text.trim()}
                  onClick={() => saveOther.mutate("approved")}
                >
                  Утвердить
                </button>
              </>
            )}
          </div>
        </section>

        <section>
          <div className="section-head">
            <h3>Как ещё спросят ({LANGUAGES.find((l) => l.code === lang)?.label})</h3>
            <AiButton
              label="Подобрать формулировки"
              busy={suggest.isPending}
              onClick={() => suggest.mutate(lang)}
            />
          </div>
          <p className="muted">
            Бот отвечает этой записью, только если клиент напишет похоже. Чем больше живых
            формулировок, тем чаще срабатывает точный ответ вместо ИИ.
          </p>
          <div className="chips">
            {phrases.map((p) => (
              <span key={p.id} className="chip removable">
                {p.pattern}
                <button
                  type="button"
                  title="Удалить формулировку"
                  onClick={() => dropPhrase.mutate(p.id)}
                >
                  ×
                </button>
              </span>
            ))}
            {phrases.length === 0 && <span className="muted">Пока ни одной.</span>}
          </div>
          <form
            className="inline-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (phrase.trim()) addPhrase.mutate(lang);
            }}
          >
            <input
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              placeholder="Например: где вы находитесь"
            />
            <button disabled={!phrase.trim() || addPhrase.isPending}>Добавить</button>
          </form>
        </section>

        {note && <div className="plaque ok-plaque">{note}</div>}
        {failure && <div className="error">{failure}</div>}
      </div>
    </div>
  );
}

/* ------------------------------- QA tab --------------------------------- */

function QaTab({ openId, onOpen }: { openId: number | null; onOpen: (id: number | null) => void }) {
  const queryClient = useQueryClient();
  const faq = useQuery({ queryKey: ["kb-faq"], queryFn: listFaq });
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");

  const create = useMutation({
    mutationFn: () =>
      createFaq({
        category: null,
        answer_ru: answer.trim(),
        patterns_ru: [question.trim()],
        patterns_kk: [],
      }),
    onSuccess: (created) => {
      setCreating(false);
      setQuestion("");
      setAnswer("");
      void queryClient.invalidateQueries({ queryKey: ["kb-faq"] });
      onOpen(created.id);
    },
  });
  const remove = useMutation({
    mutationFn: deleteFaq,
    onSuccess: () => {
      onOpen(null);
      void queryClient.invalidateQueries({ queryKey: ["kb-faq"] });
    },
  });
  const toggle = useMutation({
    mutationFn: ({ id, value }: { id: number; value: boolean }) => activateFaq(id, value),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["kb-faq"] }),
  });

  const items = faq.data?.items ?? [];
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((entry) =>
      [entry.answer_ru ?? "", ...entry.patterns.map((p) => p.pattern)]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [items, query]);
  const opened = items.find((i) => i.id === openId) ?? null;

  if (faq.isPending) return <p className="muted">Загрузка…</p>;
  if (faq.isError)
    return (
      <div className="error">
        Не удалось загрузить записи.{" "}
        <button onClick={() => void faq.refetch()}>Повторить</button>
      </div>
    );

  return (
    <>
      <div className="hint-row">
        <Icon name="info" size={15} />
        Точные пары «вопрос клиента — ответ». Бот отдаёт их дословно, не обращаясь к ИИ:
        мгновенно, предсказуемо и бесплатно. Сюда — адрес, часы работы, телефоны.
      </div>

      <div className="toolbar">
        <button className="primary" onClick={() => setCreating(true)}>
          + Добавить
        </button>
        <div className="search-row">
          <Icon name="search" size={15} className="search-icon" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Найти запись…"
          />
        </div>
      </div>

      {creating && (
        <form
          className="create-card"
          onSubmit={(e) => {
            e.preventDefault();
            if (question.trim() && answer.trim()) create.mutate();
          }}
        >
          <label>
            <span>Как спрашивает клиент</span>
            <input
              autoFocus
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Где вы находитесь?"
            />
          </label>
          <label>
            <span>Что ответить</span>
            <textarea
              rows={3}
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              placeholder="Мы находимся по адресу…"
            />
          </label>
          {errorText(create.error) && <div className="error">{errorText(create.error)}</div>}
          <div className="actions-row">
            <button type="button" onClick={() => setCreating(false)}>
              Отменить
            </button>
            <button className="primary" disabled={create.isPending}>
              Создать
            </button>
          </div>
          <p className="muted">
            Запись создаётся выключенной. Внутри добавите казахский — и её можно включить.
          </p>
        </form>
      )}

      <div className="list">
        {filtered.map((entry) => {
          const title = asTitle(
            entry.patterns.find((p) => p.language === "ru")?.pattern ?? entry.normalized_key,
          );
          return (
            <div key={entry.id} className="list-row" onClick={() => onOpen(entry.id)}>
              <div className="list-main">
                <strong>{title}</strong>
                <span className="muted">{(entry.answer_ru ?? "").slice(0, 90)}</span>
              </div>
              {!entry.can_activate && <span className="chip warn">{entry.blockers[0]}</span>}
              {entry.misses_kk > 0 && (
                <span className="chip">спрашивали на казахском · {entry.misses_kk}</span>
              )}
              <Toggle
                checked={entry.is_active}
                disabled={!entry.can_activate && !entry.is_active}
                title={entry.can_activate ? "Бот использует эту запись" : entry.blockers.join(", ")}
                onChange={(value) => toggle.mutate({ id: entry.id, value })}
              />
              <RowMenu
                items={[
                  { label: "Открыть", run: () => onOpen(entry.id) },
                  {
                    label: "Удалить",
                    danger: true,
                    run: () => {
                      if (confirm(`Удалить запись «${title}»? Это необратимо.`))
                        remove.mutate(entry.id);
                    },
                  },
                ]}
              />
            </div>
          );
        })}
        {filtered.length === 0 && (
          <p className="empty">
            {query ? "Ничего не найдено." : "Записей пока нет. Нажмите «Добавить»."}
          </p>
        )}
      </div>

      {opened && <FaqDrawer entry={opened} onClose={() => onOpen(null)} />}
    </>
  );
}

/* ------------------------------ docs tab -------------------------------- */

const DOC_STATE: Record<string, { label: string; tone: string }> = {
  indexed: { label: "Бот это знает", tone: "ok" },
  pending: { label: "Готовится, обычно 1–2 минуты", tone: "warn" },
  not_indexed: { label: "Бот не использует", tone: "muted" },
  failed: { label: "Не получилось прочитать", tone: "danger" },
};

function DocDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const queryClient = useQueryClient();
  const doc = useQuery({ queryKey: ["kb-doc", id], queryFn: () => getDocument(id) });
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (doc.data && !loaded) {
      setTitle(doc.data.title);
      setBody(doc.data.body);
      setLoaded(true);
    }
  }, [doc.data, loaded]);

  const save = useMutation({
    mutationFn: () => updateDocument(id, { title: title.trim(), body }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["kb-docs"] });
      void queryClient.invalidateQueries({ queryKey: ["kb-doc", id] });
    },
  });

  if (!doc.data) return <div className="drawer">Загрузка…</div>;
  const dirty = title !== doc.data.title || body !== doc.data.body;
  const state = DOC_STATE[doc.data.index_state] ?? DOC_STATE.not_indexed;

  return (
    <div className="drawer">
      <header className="drawer-head">
        <button type="button" className="icon-btn" onClick={onClose} title="Закрыть">
          ‹
        </button>
        <h2>{doc.data.title}</h2>
      </header>
      <div className="drawer-body">
        <label>
          <span>Название</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>
          <span>Текст, из которого бот составляет ответ</span>
          <textarea rows={16} value={body} onChange={(e) => setBody(e.target.value)} />
        </label>
        <p className={`muted ${state.tone}`}>{state.label}</p>
        {doc.data.index_error && <div className="error">{doc.data.index_error}</div>}
        {errorText(save.error) && <div className="error">{errorText(save.error)}</div>}
        <div className="actions-row">
          {dirty && <span className="muted">Есть несохранённые правки</span>}
          <button className="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
            Сохранить
          </button>
        </div>
      </div>
    </div>
  );
}

function DocsTab({ openId, onOpen }: { openId: number | null; onOpen: (id: number | null) => void }) {
  const queryClient = useQueryClient();
  const docs = useQuery({ queryKey: ["kb-docs"], queryFn: () => listDocuments() });
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["kb-docs"] });
  const create = useMutation({
    mutationFn: () => createDocument({ title: title.trim(), body, category: null }),
    onSuccess: (created) => {
      setCreating(false);
      setTitle("");
      setBody("");
      void refresh();
      onOpen(created.id);
    },
  });
  // One switch instead of two: "the bot uses this" means active AND
  // searchable — the old pair of controls made people think it was broken.
  const toggle = useMutation({
    mutationFn: ({ id, value }: { id: number; value: boolean }) =>
      updateDocument(id, { status: value ? "active" : "draft", is_searchable: value }),
    onSuccess: () => void refresh(),
  });
  const remove = useMutation({
    mutationFn: deleteDocument,
    onSuccess: () => {
      onOpen(null);
      void refresh();
    },
  });

  const items: KbDocument[] = docs.data?.items ?? [];
  const filtered = items.filter((d) => d.title.toLowerCase().includes(query.trim().toLowerCase()));

  if (docs.isPending) return <p className="muted">Загрузка…</p>;
  if (docs.isError)
    return (
      <div className="error">
        Не удалось загрузить.{" "}
        <button onClick={() => void docs.refetch()}>Повторить</button>
      </div>
    );

  return (
    <>
      <div className="hint-row">
        <Icon name="info" size={15} />
        Тексты, из которых бот сам составляет ответ, когда точной записи нет. Сюда — цены,
        условия, описания услуг: всё длинное и часто меняющееся.
      </div>
      <div className="toolbar">
        <button className="primary" onClick={() => setCreating(true)}>
          + Добавить
        </button>
        <div className="search-row">
          <Icon name="search" size={15} className="search-icon" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Найти раздел…"
          />
        </div>
      </div>

      {creating && (
        <form
          className="create-card"
          onSubmit={(e) => {
            e.preventDefault();
            if (title.trim() && body.trim()) create.mutate();
          }}
        >
          <label>
            <span>Название раздела</span>
            <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)} />
          </label>
          <label>
            <span>Текст</span>
            <textarea rows={6} value={body} onChange={(e) => setBody(e.target.value)} />
          </label>
          {errorText(create.error) && <div className="error">{errorText(create.error)}</div>}
          <div className="actions-row">
            <button type="button" onClick={() => setCreating(false)}>
              Отменить
            </button>
            <button className="primary" disabled={create.isPending}>
              Создать
            </button>
          </div>
        </form>
      )}

      <div className="list">
        {filtered.map((doc) => {
          const state = DOC_STATE[doc.index_state] ?? DOC_STATE.not_indexed;
          return (
            <div key={doc.id} className="list-row" onClick={() => onOpen(doc.id)}>
              <div className="list-main">
                <strong>{doc.title}</strong>
                <span className={`muted ${state.tone}`}>{state.label}</span>
              </div>
              <Toggle
                checked={doc.status === "active" && doc.is_searchable}
                title="Бот использует этот текст в ответах"
                onChange={(value) => toggle.mutate({ id: doc.id, value })}
              />
              <RowMenu
                items={[
                  { label: "Открыть", run: () => onOpen(doc.id) },
                  {
                    label: "Удалить",
                    danger: true,
                    run: () => {
                      if (confirm(`Удалить раздел «${doc.title}»?`)) remove.mutate(doc.id);
                    },
                  },
                ]}
              />
            </div>
          );
        })}
        {filtered.length === 0 && (
          <p className="empty">
            {query ? "Ничего не найдено." : "Разделов пока нет. Нажмите «Добавить»."}
          </p>
        )}
      </div>

      {openId !== null && <DocDrawer id={openId} onClose={() => onOpen(null)} />}
    </>
  );
}

/* --------------------------- check-the-bot panel ------------------------ */

function CheckDrawer({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const run = useMutation({ mutationFn: searchPreview, onSuccess: setResult });

  return (
    <div className="drawer narrow-drawer">
      <header className="drawer-head">
        <button type="button" className="icon-btn" onClick={onClose} title="Закрыть">
          ‹
        </button>
        <h2>Проверить бота</h2>
      </header>
      <div className="drawer-body">
        <p className="muted">
          Напишите так, как написал бы клиент, — покажу, что бот найдёт. Сам ответ здесь не
          сочиняется: для полного ответа есть «Песочница» в разделе «Агент».
        </p>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (query.trim()) run.mutate(query.trim());
          }}
        >
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Где вы находитесь?"
          />
          <button className="primary" disabled={run.isPending}>
            {run.isPending ? "Ищу…" : "Проверить"}
          </button>
        </form>
        {errorText(run.error) && <div className="error">{errorText(run.error)}</div>}

        {result && (
          <div className="check-result">
            <p className="muted">
              Клиент написал на языке:{" "}
              <strong>
                {LANGUAGES.find((l) => l.code === result.detected_language)?.label ??
                  result.detected_language}
              </strong>
            </p>
            {result.faq_hit ? (
              <div className="plaque ok-plaque">
                <strong>Бот ответит точной записью:</strong> {result.faq_hit}
              </div>
            ) : (
              <div className="plaque">
                Точной записи нет — бот составит ответ сам
                {result.hits.length > 0
                  ? ", опираясь на: " + result.hits.map((h) => h.title).join(", ")
                  : ". Подходящих разделов тоже не нашлось — вопрос уйдёт менеджеру."}
              </div>
            )}
            {result.faq_language_miss && (
              <div className="plaque warn">
                Запись нашлась, но перевода на этот язык нет — клиент получил бы ответ от ИИ.
                Откройте запись и нажмите «Перевести».
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------- page ---------------------------------- */

export default function KnowledgePage() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) === "docs" ? "docs" : "qa";
  const openId = params.get("id") ? Number(params.get("id")) : null;
  const checking = params.get("check") === "1";

  const faq = useQuery({ queryKey: ["kb-faq"], queryFn: listFaq });
  const docs = useQuery({ queryKey: ["kb-docs"], queryFn: () => listDocuments() });

  function go(next: Partial<{ tab: Tab; id: number | null; check: boolean }>) {
    const updated = new URLSearchParams(params);
    if (next.tab) {
      updated.set("tab", next.tab);
      updated.delete("id");
    }
    if (next.id !== undefined) {
      if (next.id === null) updated.delete("id");
      else updated.set("id", String(next.id));
    }
    if (next.check !== undefined) {
      if (next.check) updated.set("check", "1");
      else updated.delete("check");
    }
    setParams(updated, { replace: false });
  }

  const notReady = (faq.data?.items ?? []).filter((i) => !i.is_active).length;
  const preparing = (docs.data?.items ?? []).filter((d) => d.index_state === "pending").length;

  return (
    <div className="kb-page">
      <section className="card">
        <div className="tabs">
          <button className={tab === "qa" ? "tab active" : "tab"} onClick={() => go({ tab: "qa" })}>
            Вопрос — ответ <span className="tab-count">{faq.data?.items.length ?? 0}</span>
            {notReady > 0 && <span className="chip">{notReady} выключены</span>}
          </button>
          <button
            className={tab === "docs" ? "tab active" : "tab"}
            onClick={() => go({ tab: "docs" })}
          >
            Справка для бота <span className="tab-count">{docs.data?.items.length ?? 0}</span>
            {preparing > 0 && <span className="chip warn">{preparing} готовятся</span>}
          </button>
        </div>

        {tab === "qa" ? (
          <QaTab openId={openId} onOpen={(id) => go({ id })} />
        ) : (
          <DocsTab openId={openId} onOpen={(id) => go({ id })} />
        )}
      </section>

      <button type="button" className="side-tab" onClick={() => go({ check: true })}>
        Проверить бота
      </button>
      {checking && <CheckDrawer onClose={() => go({ check: false })} />}
    </div>
  );
}
