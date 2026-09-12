import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { getAnalytics } from "../api/analytics";
import { Donut, plural } from "../components/charts";
import Icon from "../components/icons";

const PERIODS = [
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
  { days: 90, label: "90 дней" },
];

/* Plain-language names for what the database stores as codes. Anything
 * unknown is shown as-is rather than guessed at. */
const ERROR_LABELS: Record<string, string> = {
  stale: "Пришло слишком давно — ответил бы невпопад",
  media_unsupported: "Файл, который бот не читает",
  ai_off: "AI был выключен для этого чата",
  rate_limited: "Клиент писал слишком часто",
  spam: "Одно и то же сообщение подряд",
  empty: "Пустое сообщение",
  audio_too_long: "Слишком длинное голосовое",
  image_limit_reached: "Лимит фото на диалог",
  media_disabled: "Распознавание выключено",
  media_not_configured: "Не настроено распознавание",
  offtopic: "Вопрос не по теме центра",
  injection: "Попытка подменить инструкцию бота",
  too_long: "Слишком длинное сообщение",
  empty_transcript: "В голосовом ничего не разобрать",
};

const SOURCE_LABELS: Record<
  string,
  { name: string; color: string; free: boolean }
> = {
  faq: {
    name: "Точный ответ из базы (бесплатно)",
    color: "#22a06b",
    free: true,
  },
  cache: { name: "Повтор из кэша (бесплатно)", color: "#7cc4a4", free: true },
  model: { name: "Составил ИИ (платно)", color: "#2f6bff", free: false },
  service: {
    name: "Отказ или передача менеджеру",
    color: "#b47818",
    free: false,
  },
};

const LANGUAGE_NAMES: Record<string, string> = {
  ru: "русском",
  kk: "казахском",
  en: "английском",
};

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const report = useQuery({
    queryKey: ["analytics", days],
    queryFn: () => getAnalytics(days),
  });

  if (report.isPending) return <p className="muted">Загрузка…</p>;
  if (report.isError || !report.data)
    return (
      <div className="error">
        Не удалось загрузить аналитику.{" "}
        <button onClick={() => void report.refetch()}>Повторить</button>
      </div>
    );

  const data = report.data;
  const work = data.workload;
  const sources = Object.entries(data.answer_sources).filter(
    ([, count]) => count > 0,
  );
  const freeAnswers = sources
    .filter(([key]) => SOURCE_LABELS[key]?.free)
    .reduce((sum, [, count]) => sum + count, 0);

  return (
    <div className="report-page">
      <section className="card">
        <div className="toolbar">
          <div className="segmented">
            {PERIODS.map((p) => (
              <button
                key={p.days}
                type="button"
                className={days === p.days ? "active" : ""}
                onClick={() => setDays(p.days)}
              >
                {p.label}
              </button>
            ))}
          </div>
          <span className="muted">
            за последние {data.period.days} дн. (сутки по UTC)
          </span>
        </div>

        <div className="metric-row">
          <div className="metric">
            <span className="value">{work.incoming}</span>
            <span className="caption">Написали клиенты</span>
          </div>
          <div className="metric">
            <span className="value">{work.answered_by_ai}</span>
            <span className="caption">Ответил бот</span>
          </div>
          <div className="metric">
            <span className="value">{work.handed_to_human}</span>
            <span className="caption">Передано менеджеру</span>
          </div>
          <div className="metric">
            <span className="value">{work.automation_percent}%</span>
            <span className="caption">Бот справился сам</span>
            <span className="sub">
              из {work.answerable}{" "}
              {plural(work.answerable, "сообщения", "сообщений", "сообщений")},
              требовавших ответа
            </span>
          </div>
        </div>

        <Donut
          slices={[
            {
              name: "Ответил бот",
              value: work.answered_by_ai,
              color: "#2f6bff",
            },
            {
              name: "Передано менеджеру",
              value: work.handed_to_human,
              color: "#b47818",
            },
          ]}
          emptyText="Пока не на чем считать: сообщений не было."
        />
        {work.ignored > 0 && (
          <p className="muted">
            Ещё {work.ignored}{" "}
            {plural(work.ignored, "сообщение", "сообщения", "сообщений")} не{" "}
            {plural(work.ignored, "требовало", "требовали", "требовали")} ответа
            (слишком старые, спам или повторы) — они не учитываются в проценте,
            чтобы он не врал.
          </p>
        )}
      </section>

      <section className="card">
        <h2>Чем именно отвечал бот</h2>
        <p className="muted">
          Ответы из базы знаний и повторы бесплатны: за них не платится ничего.
          Чем их больше, тем дешевле работает бот.
        </p>
        {sources.length === 0 ? (
          <p className="muted">Бот пока не отвечал.</p>
        ) : (
          <>
            <Donut
              slices={sources.map(([key, count]) => ({
                name: SOURCE_LABELS[key]?.name ?? key,
                value: count,
                color: SOURCE_LABELS[key]?.color ?? "#8a93a6",
              }))}
            />
            <p className="muted">
              Бесплатных ответов: <strong>{freeAnswers}</strong> из{" "}
              {sources.reduce((sum, [, count]) => sum + count, 0)}.
            </p>
          </>
        )}
      </section>

      <section className="card">
        <h2>Диалоги</h2>
        <div className="metric-row">
          <div className="metric">
            <span className="value">{data.dialogs.total}</span>
            <span className="caption">Всего диалогов</span>
          </div>
          <div className="metric">
            <span className="value">{data.dialogs.new}</span>
            <span className="caption">Новых за период</span>
          </div>
          <div className="metric">
            <span className="value">
              {Number(data.dialogs.average_cost_kzt).toLocaleString("ru-RU", {
                maximumFractionDigits: 2,
              })}{" "}
              ₸
            </span>
            <span className="caption">В среднем на диалог</span>
            <span className="sub">
              по {data.dialogs.with_spend}{" "}
              {plural(
                data.dialogs.with_spend,
                "платному диалогу",
                "платным диалогам",
                "платным диалогам",
              )}
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <h2>Что мешало отвечать</h2>
        {data.errors.length === 0 ? (
          <p className="muted">Ничего: за период таких случаев не было.</p>
        ) : (
          <table className="plain-table">
            <thead>
              <tr>
                <th>Причина</th>
                <th>Случаев</th>
              </tr>
            </thead>
            <tbody>
              {data.errors.map((row) => (
                <tr key={row.code}>
                  <td>{ERROR_LABELS[row.code] ?? row.code}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Что перевести в первую очередь</h2>
        <p className="muted">
          Клиент спросил на своём языке, а утверждённого перевода не было — бот
          отвечал через ИИ: дольше и платно.
        </p>
        {data.language_gaps.length === 0 ? (
          <p className="muted">Таких случаев не было.</p>
        ) : (
          <table className="plain-table">
            <thead>
              <tr>
                <th>Спрашивали на</th>
                <th>Запись</th>
                <th>Раз</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.language_gaps.map((row) => (
                <tr key={`${row.language}-${row.faq_id}`}>
                  <td>{LANGUAGE_NAMES[row.language] ?? row.language}</td>
                  <td>{row.key ?? "—"}</td>
                  <td>{row.misses}</td>
                  <td>
                    {row.faq_id && (
                      <Link to={`/knowledge?tab=qa&id=${row.faq_id}`}>
                        Открыть и перевести
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card subdued">
        <h2>
          <Icon name="info" size={16} /> Чего здесь намеренно нет
        </h2>
        <p className="muted">
          Эти цифры мы не показываем, потому что данных под них нет. Нарисовать
          их можно было бы, но вы принимали бы по ним решения — а они были бы
          выдуманы.
        </p>
        <ul className="muted">
          {data.not_measured.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>
    </div>
  );
}
