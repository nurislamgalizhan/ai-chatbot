import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  getLimits,
  type LimitsSettings,
  setFxRate,
  setPrice,
  updateLimits,
} from "../api/limits";

const UNIT_LABELS: Record<string, string> = {
  per_1m_input: "1M входных токенов",
  per_1m_cached_input: "1M кэшированных",
  per_1m_output: "1M выходных",
  per_minute_audio: "минута аудио",
};

function kzt(usd: string | number, rate: string | null): string {
  if (!rate) return "—";
  return `${(Number(usd) * Number(rate)).toFixed(2)} ₸`;
}

function NumberField({
  label,
  hint,
  value,
  onChange,
  money,
  rate,
}: {
  label: string;
  hint?: string;
  value: number;
  onChange: (v: number) => void;
  money?: boolean;
  rate?: string | null;
}) {
  return (
    <label className="limit-field">
      <span>{label}</span>
      <input
        type="number"
        value={Number.isFinite(value) ? value : ""}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {money && rate && (
        <span className="muted">≈ ${(value / Number(rate)).toFixed(4)}</span>
      )}
      {hint && <span className="muted">{hint}</span>}
    </label>
  );
}

export default function LimitsPage() {
  const queryClient = useQueryClient();
  const limits = useQuery({ queryKey: ["limits"], queryFn: getLimits });
  const [form, setForm] = useState<LimitsSettings | null>(null);
  const [fx, setFx] = useState("");
  const [price, setPriceForm] = useState({ model: "", unit: "per_1m_input", value: "" });
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (limits.data && form === null) setForm(limits.data.settings);
  }, [limits.data, form]);

  const save = useMutation({
    mutationFn: (settings: LimitsSettings) => updateLimits(settings),
    onSuccess: () => {
      setNotice("Сохранено. Изменённые бюджеты применены и к текущему дню/месяцу.");
      void queryClient.invalidateQueries({ queryKey: ["limits"] });
    },
  });
  const saveFx = useMutation({
    mutationFn: () => setFxRate(fx),
    onSuccess: () => {
      setFx("");
      void queryClient.invalidateQueries({ queryKey: ["limits"] });
    },
  });
  const savePrice = useMutation({
    mutationFn: () => setPrice(price.model, price.unit, price.value),
    onSuccess: () => {
      setPriceForm({ model: "", unit: "per_1m_input", value: "" });
      void queryClient.invalidateQueries({ queryKey: ["limits"] });
    },
  });

  if (limits.isPending || !form) return <div className="empty">Загрузка…</div>;
  if (limits.isError) return <div className="error">Не удалось загрузить лимиты</div>;
  const data = limits.data;
  const rate = data.usd_kzt_rate;
  const update = (patch: Partial<LimitsSettings>) =>
    setForm((prev) => (prev ? { ...prev, ...patch } : prev));
  const saveError =
    save.error instanceof ApiError ? save.error.message : save.error ? "Ошибка" : null;

  const day = data.ledgers.find((l) => l.scope === "global_day");
  const month = data.ledgers.find((l) => l.scope === "global_month");

  return (
    <div className="limits-page">
      <div className="plaque warn dry-run-note">
        <strong>Важно:</strong> DRY_RUN отключает только отправку сообщений в
        WhatsApp. Расходы на OpenAI при этом идут по-настоящему — их
        ограничивают лимиты на этой странице.
        {data.dry_run ? " Сейчас DRY_RUN включён." : " Сейчас DRY_RUN выключен."}
      </div>

      <div className="stat-cards">
        <div className="card stat">
          <span className="muted">Расход сегодня</span>
          <strong>{day ? kzt(day.spent_usd, rate) : "0 ₸"}</strong>
          <span className="muted">
            лимит{" "}
            {day ? kzt(day.limit_usd, rate) : `${data.settings.daily_ai_budget_kzt} ₸`}
            {day && Number(day.reserved_usd) > 0
              ? `, в резерве ${kzt(day.reserved_usd, rate)}`
              : ""}
          </span>
        </div>
        <div className="card stat">
          <span className="muted">Расход за месяц</span>
          <strong>{month ? kzt(month.spent_usd, rate) : "0 ₸"}</strong>
          <span className="muted">
            лимит{" "}
            {month
              ? kzt(month.limit_usd, rate)
              : `${data.settings.monthly_ai_budget_kzt} ₸`}
          </span>
        </div>
        <div className="card stat">
          <span className="muted">Курс USD/KZT</span>
          <strong>{rate ?? "не задан"}</strong>
          <span className="muted">
            {rate ? "задаётся вручную, журналируется" : "без курса AI не тратит деньги"}
          </span>
        </div>
      </div>

      <section className="card">
        <h2>Курс и цены моделей</h2>
        <p className="muted">
          Курс и цены — журналы: изменение действует с текущего момента,
          история расходов не пересчитывается. Периоды бюджета считаются по
          UTC.
        </p>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (fx) saveFx.mutate();
          }}
        >
          <label>
            Новый курс USD/KZT
            <input
              type="number"
              step="0.01"
              min="1"
              value={fx}
              onChange={(e) => setFx(e.target.value)}
              placeholder={rate ?? "например 512.30"}
            />
          </label>
          <button className="primary" disabled={!fx || saveFx.isPending}>
            Сохранить курс
          </button>
        </form>
        <table className="plain-table">
          <thead>
            <tr>
              <th>Модель</th>
              <th>Единица</th>
              <th>Цена, $</th>
              <th>Действует с</th>
            </tr>
          </thead>
          <tbody>
            {data.prices.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  Цены не заведены — выполните make seed-prices и сверьте с
                  актуальной страницей цен OpenAI
                </td>
              </tr>
            )}
            {data.prices.map((p) => (
              <tr key={`${p.model}-${p.unit}`}>
                <td>{p.model}</td>
                <td>{UNIT_LABELS[p.unit] ?? p.unit}</td>
                <td>{p.price_usd}</td>
                <td>{new Date(p.effective_from).toLocaleDateString("ru-RU")}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (price.model && price.value) savePrice.mutate();
          }}
        >
          <label>
            Модель
            <input
              value={price.model}
              onChange={(e) => setPriceForm({ ...price, model: e.target.value })}
              placeholder="gpt-5.6-luna"
            />
          </label>
          <label>
            Единица
            <select
              value={price.unit}
              onChange={(e) => setPriceForm({ ...price, unit: e.target.value })}
            >
              {Object.entries(UNIT_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Цена, $
            <input
              type="number"
              step="0.000001"
              min="0"
              value={price.value}
              onChange={(e) => setPriceForm({ ...price, value: e.target.value })}
            />
          </label>
          <button disabled={!price.model || !price.value || savePrice.isPending}>
            Обновить цену
          </button>
        </form>
      </section>

      <section className="card">
        <h2>Бюджеты (₸)</h2>
        <div className="limits-grid">
          <NumberField
            label="Дневной бюджет AI"
            value={form.daily_ai_budget_kzt}
            onChange={(v) => update({ daily_ai_budget_kzt: v })}
            money
            rate={rate}
          />
          <NumberField
            label="Месячный бюджет AI"
            value={form.monthly_ai_budget_kzt}
            onChange={(v) => update({ monthly_ai_budget_kzt: v })}
            money
            rate={rate}
          />
          <NumberField
            label="Лимит на один диалог"
            value={form.max_ai_cost_per_dialog_kzt}
            onChange={(v) => update({ max_ai_cost_per_dialog_kzt: v })}
            money
            rate={rate}
          />
          <NumberField
            label="Лимит на клиента в день"
            value={form.max_ai_cost_per_user_per_day_kzt}
            onChange={(v) => update({ max_ai_cost_per_user_per_day_kzt: v })}
            money
            rate={rate}
          />
        </div>
      </section>

      <section className="card">
        <h2>Контекст и поведение</h2>
        <div className="limits-grid">
          <NumberField
            label="Макс. входных токенов"
            value={form.max_input_tokens}
            onChange={(v) => update({ max_input_tokens: v })}
          />
          <NumberField
            label="Макс. токенов ответа"
            value={form.max_output_tokens}
            onChange={(v) => update({ max_output_tokens: v })}
          />
          <NumberField
            label="Дебаунс, сек"
            hint="склейка подряд идущих сообщений"
            value={form.debounce_seconds}
            onChange={(v) => update({ debounce_seconds: v })}
          />
          <NumberField
            label="Отсечка возраста, мин"
            hint="старше — не отвечаем, чат помечается"
            value={form.ai_max_message_age_minutes}
            onChange={(v) => update({ ai_max_message_age_minutes: v })}
          />
          <NumberField
            label="Сообщений от клиента в минуту"
            value={form.max_messages_per_user_per_minute}
            onChange={(v) => update({ max_messages_per_user_per_minute: v })}
          />
          <NumberField
            label="Сообщений от клиента в час"
            value={form.max_messages_per_user_per_hour}
            onChange={(v) => update({ max_messages_per_user_per_hour: v })}
          />
          <NumberField
            label="Спам: одинаковых подряд"
            value={form.spam_repeat_threshold}
            onChange={(v) => update({ spam_repeat_threshold: v })}
          />
          <NumberField
            label="Circuit breaker: ошибок подряд"
            value={form.circuit_breaker_failures}
            onChange={(v) => update({ circuit_breaker_failures: v })}
          />
          <NumberField
            label="Circuit breaker: пауза, мин"
            value={form.circuit_breaker_cooldown_minutes}
            onChange={(v) => update({ circuit_breaker_cooldown_minutes: v })}
          />
        </div>
      </section>

      <section className="card">
        <h2>Кэш ответов</h2>
        <label className="limit-field checkbox">
          <input
            type="checkbox"
            checked={form.semantic_cache_enabled}
            onChange={(e) => update({ semantic_cache_enabled: e.target.checked })}
          />
          <span>
            Включить семантический кэш (повторный одинаковый вопрос — ответ без
            модели; выключен по умолчанию)
          </span>
        </label>
        <div className="limits-grid">
          <NumberField
            label="TTL кэша, часов"
            value={form.semantic_cache_ttl_hours}
            onChange={(v) => update({ semantic_cache_ttl_hours: v })}
          />
          <label className="limit-field">
            <span>Разрешённые домены ссылок</span>
            <input
              value={form.allowed_link_domains.join(", ")}
              onChange={(e) =>
                update({
                  allowed_link_domains: e.target.value
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean),
                })
              }
              placeholder="go.2gis.com, 2gis.kz"
            />
            <span className="muted">ссылки на другие домены в ответах модели блокируются</span>
          </label>
        </div>
      </section>

      <section className="card">
        <h2>Медиа</h2>
        <label className="limit-field checkbox">
          <input
            type="checkbox"
            checked={form.transcription_enabled}
            onChange={(e) => update({ transcription_enabled: e.target.checked })}
          />
          <span>
            Распознавать голосовые сообщения (платно, по минутам аудио).
            Выключено — голосовые уходят менеджеру
          </span>
        </label>
        <label className="limit-field checkbox">
          <input
            type="checkbox"
            checked={form.vision_enabled}
            onChange={(e) => update({ vision_enabled: e.target.checked })}
          />
          <span>
            Распознавать фотографии (по умолчанию выключено). Фото с
            документами, картами и медицинскими снимками не отправляются в
            модель никогда, но фото без подписи распознать как документ
            невозможно — включайте осознанно
          </span>
        </label>
        <div className="limits-grid">
          <NumberField
            label="Макс. длина голосового, сек"
            value={form.max_audio_seconds}
            onChange={(v) => update({ max_audio_seconds: v })}
          />
          <NumberField
            label="Макс. размер аудио, МБ"
            value={form.max_audio_file_mb}
            onChange={(v) => update({ max_audio_file_mb: v })}
          />
          <NumberField
            label="Фото на диалог"
            value={form.max_images_per_dialog}
            onChange={(v) => update({ max_images_per_dialog: v })}
          />
          <NumberField
            label="Макс. размер фото, МБ"
            value={form.max_image_file_mb}
            onChange={(v) => update({ max_image_file_mb: v })}
          />
          <NumberField
            label="Хранить файлы, дней"
            value={form.media_retention_days}
            onChange={(v) => update({ media_retention_days: v })}
          />
          <label className="limit-field">
            <span>Термины для распознавания речи</span>
            <input
              value={form.transcription_terms}
              onChange={(e) => update({ transcription_terms: e.target.value })}
              placeholder="название центра, секции, улица"
            />
            <span className="muted">
              подсказка модели, чтобы правильно писала названия
            </span>
          </label>
        </div>
        <p className="muted">
          Документы, видео и GIF не читаются никогда — клиент получает шаблон, а
          чат помечается «требует внимания». Тексты распознавания остаются в
          истории после удаления самих файлов.
        </p>
      </section>

      {notice && <div className="plaque warn">{notice}</div>}
      {saveError && <div className="error">{saveError}</div>}
      <div className="actions-row">
        <button className="primary" disabled={save.isPending} onClick={() => save.mutate(form)}>
          Сохранить лимиты
        </button>
      </div>
    </div>
  );
}
