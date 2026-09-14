import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  activateInstruction,
  createInstruction,
  type Instruction,
  listInstructions,
  runSandbox,
  type SandboxResult,
} from "../api/agent";
import { ApiError } from "../api/client";
import Icon from "../components/icons";
import { messageReason } from "../components/reasons";

const EFFORTS = ["none", "low", "medium", "high"];

/** White-label scaffold per docs/knowledge-base-import.md §4: the owner
 *  fills in the organization specifics; SPEC §14.1 security rules are
 *  appended automatically by the backend and are NOT edited here. */
const PREFILL = `#РОЛЬ
Ты — виртуальный консультант организации. Помогай клиентам получать
информацию об услугах, собирай данные для заявки и передавай заявки
менеджерам.

#ЦЕЛИ И ЗАДАЧИ
Консультируй клиентов по услугам организации.
Подбирай подходящие услуги под запрос клиента.
Собирай данные клиента для оформления заявки: имя, дату и время, услугу.
Отвечай на вопросы о стоимости, времени и правилах.
Передавай заявки менеджерам.

#ПРИВЕТСТВИЕ
В начале нового диалога поздоровайся по времени суток (Доброе утро /
Добрый день / Добрый вечер), представься консультантом организации и
спроси, чем можешь помочь. Не повторяй приветствие в одном диалоге.

#ЯЗЫК ОТВЕТОВ
Отвечай строго на языке клиента.

#СТИЛЬ ОТВЕТА
Используй списки для перечисления вариантов.
Избегай слишком длинных ответов.
Завершай сообщение уточняющим вопросом, продвигающим диалог.

#ОГРАНИЧЕНИЯ
Не выдумывай цены, расписание и условия — только данные из справки.
Нет данных — скажи честно и предложи передать вопрос администратору.
Не обещай «сам забронирую» — только «передал заявку менеджеру».`;

function SandboxPanel() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<SandboxResult | null>(null);
  const run = useMutation({
    mutationFn: runSandbox,
    onSuccess: setResult,
  });
  const error =
    run.error instanceof ApiError ? run.error.message : run.error ? "Ошибка" : null;
  return (
    <section className="card sandbox">
      <h2>Песочница</h2>
      <p className="muted">
        Вопрос проходит весь конвейер (FAQ → база знаний → модель) с реальным
        вызовом и реальным списанием бюджета, но без отправки клиентам и без
        записи в чаты.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim()) run.mutate(question.trim());
        }}
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Вопрос клиента, например: сколько стоит абонемент?"
          maxLength={2000}
        />
        <button className="primary" disabled={run.isPending || !question.trim()}>
          {run.isPending ? "Выполняется…" : "Проверить"}
        </button>
      </form>
      {error && <div className="error">{error}</div>}
      {result && (
        <div className="sandbox-result">
          <div className="chip">{result.kind}</div>
          <div className="chip">язык: {result.detected_language}</div>
          {result.instruction_version != null && (
            <div className="chip">инструкция v{result.instruction_version}</div>
          )}
          {result.text && <div className="sandbox-answer">{result.text}</div>}
          {result.error_code &&
            (() => {
              const reason = messageReason(
                result.error_code,
                result.kind === "guardrail" || result.kind === "model" ? "ANSWERED" : "FAILED_FINAL",
              );
              return reason ? <div className={`plaque ${reason.tone}`}>{reason.text}</div> : null;
            })()}
          {result.usage && (
            <dl className="kv">
              <dt>Токены</dt>
              <dd>
                вход {result.usage.input_tokens} (кэш{" "}
                {result.usage.cached_input_tokens}), выход{" "}
                {result.usage.output_tokens}, reasoning{" "}
                {result.usage.reasoning_tokens}
              </dd>
              <dt>Стоимость</dt>
              <dd>
                {result.cost_kzt} ₸ (${result.cost_usd})
              </dd>
              {result.latency_ms != null && (
                <>
                  <dt>Время</dt>
                  <dd>{result.latency_ms} мс</dd>
                </>
              )}
              {result.kb_chunk_ids.length > 0 && (
                <>
                  <dt>Фрагменты</dt>
                  <dd>{result.kb_chunk_ids.join(", ")}</dd>
                </>
              )}
            </dl>
          )}
        </div>
      )}
    </section>
  );
}

export default function AgentPage() {
  const queryClient = useQueryClient();
  const instructions = useQuery({
    queryKey: ["agent-instructions"],
    queryFn: listInstructions,
  });
  const items = instructions.data?.items ?? [];
  const active = items.find((i) => i.is_active) ?? null;

  const [prompt, setPrompt] = useState("");
  const [style, setStyle] = useState("");
  const [maxTokens, setMaxTokens] = useState<string>("");
  const [effort, setEffort] = useState<string>("");
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Load the active (or newest) version into the form once.
  useEffect(() => {
    if (loadedVersion !== null || items.length === 0) return;
    const source = active ?? items[0];
    setPrompt(source.system_prompt);
    setStyle(source.style ?? "");
    setMaxTokens(source.max_output_tokens?.toString() ?? "");
    setEffort(source.reasoning_effort ?? "");
    setLoadedVersion(source.version);
  }, [items, active, loadedVersion]);

  const save = useMutation({
    mutationFn: () =>
      createInstruction({
        system_prompt: prompt.trim(),
        style: style.trim() || null,
        escalation_rules: null,
        max_output_tokens: maxTokens ? Number(maxTokens) : null,
        reasoning_effort: effort || null,
      }),
    onSuccess: (created) => {
      setNotice(
        `Сохранена версия v${created.version}. Она НЕ активна — проверьте её в песочнице и активируйте.`,
      );
      void queryClient.invalidateQueries({ queryKey: ["agent-instructions"] });
    },
  });
  const activate = useMutation({
    mutationFn: activateInstruction,
    onSuccess: () => {
      setNotice(null);
      void queryClient.invalidateQueries({ queryKey: ["agent-instructions"] });
    },
  });

  function loadVersion(instruction: Instruction) {
    setPrompt(instruction.system_prompt);
    setStyle(instruction.style ?? "");
    setMaxTokens(instruction.max_output_tokens?.toString() ?? "");
    setEffort(instruction.reasoning_effort ?? "");
    setLoadedVersion(instruction.version);
    setNotice(`В редактор загружена v${instruction.version} — сохранение создаст новую версию.`);
  }

  const saveError =
    save.error instanceof ApiError ? save.error.message : save.error ? "Ошибка" : null;

  return (
    <div className="agent-layout">
      <div className="agent-main">
        <section className="card">
          <div className="section-head">
            <div>
              <h2>Инструкция агента</h2>
              <p className="muted">
                Инструкция определяет поведение агента и его ответы. Правила
                безопасности (SPEC §14.1) добавляются к каждой версии
                автоматически и не редактируются.
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                setPrompt(PREFILL);
                setNotice(
                  "Загружен образец. Заполните название организации и детали услуг, затем сохраните.",
                );
              }}
            >
              <Icon name="book" /> Заполнить по образцу
            </button>
          </div>
          <textarea
            className="instruction-editor"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={18}
            placeholder="#РОЛЬ …"
          />
          <div className="form-row">
            <label>
              Стиль ответов
              <input
                value={style}
                onChange={(e) => setStyle(e.target.value)}
                placeholder="Кратко, вежливо, со списками"
              />
            </label>
            <label>
              Макс. токенов ответа
              <input
                type="number"
                min={50}
                max={2000}
                value={maxTokens}
                onChange={(e) => setMaxTokens(e.target.value)}
                placeholder="350 (по умолчанию)"
              />
            </label>
            <label>
              Reasoning effort
              <select value={effort} onChange={(e) => setEffort(e.target.value)}>
                <option value="">по умолчанию</option>
                {EFFORTS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="muted">
            Отказы и передачу менеджеру бот формулирует сам, на языке клиента —
            заранее заданных текстов больше нет. Тон этих сообщений задаётся
            полем «Стиль» выше.
          </p>
          {notice && <div className="plaque warn">{notice}</div>}
          {saveError && <div className="error">{saveError}</div>}
          <div className="actions-row">
            <button
              className="primary"
              disabled={save.isPending || prompt.trim().length < 20}
              onClick={() => save.mutate()}
            >
              Сохранить как новую версию
            </button>
          </div>
        </section>
        <SandboxPanel />
      </div>
      <aside className="agent-side">
        <section className="card">
          <h2>Версии</h2>
          {items.length === 0 && (
            <p className="empty">
              Версий ещё нет. Бот не отвечает, пока нет активной инструкции.
            </p>
          )}
          <ul className="version-list">
            {items.map((instruction) => (
              <li key={instruction.id} className={instruction.is_active ? "active" : ""}>
                <div className="version-head">
                  <strong>v{instruction.version}</strong>
                  {instruction.is_active && <span className="chip ok">активна</span>}
                  <span className="muted">
                    {new Date(instruction.created_at).toLocaleDateString("ru-RU")}
                  </span>
                </div>
                <div className="version-preview">
                  {instruction.system_prompt.slice(0, 100)}…
                </div>
                <div className="version-actions">
                  <button type="button" onClick={() => loadVersion(instruction)}>
                    В редактор
                  </button>
                  {!instruction.is_active && (
                    <button
                      type="button"
                      disabled={activate.isPending}
                      onClick={() => activate.mutate(instruction.id)}
                    >
                      Активировать
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      </aside>
    </div>
  );
}
