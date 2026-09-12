import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { type CostsResponse, exportUrl, getCosts } from "../api/analytics";
import { StackedBarChart } from "../components/charts";
import Icon from "../components/icons";

/* Colours per call type, stable across charts and legend. */
const TYPE_COLORS: Record<string, string> = {
  chat: "#2f6bff",
  transcription: "#22a06b",
  embedding: "#b47818",
  vision: "#8b5cf6",
  admin: "#8a93a6",
};

const PERIODS = [
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
  { days: 90, label: "90 дней" },
];

function money(value: string | number, suffix = " ₸"): string {
  const amount = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(amount)) return `0${suffix}`;
  // Small sums keep their decimals — rounding 2.4 ₸ to "2 ₸" on the page
  // that exists to report money is how reports stop being trusted.
  const digits = amount >= 100 ? 0 : 2;
  return `${amount.toLocaleString("ru-RU", { maximumFractionDigits: digits })}${suffix}`;
}

function shortDay(iso: string): string {
  const [, month, day] = iso.split("-");
  return `${day}.${month}`;
}

function BudgetBar({ data }: { data: CostsResponse["budget"] }) {
  const month = data.month;
  if (!month) {
    return (
      <p className="muted">
        Месячный бюджет ещё не открывался: он заводится при первом платном ответе.
        Сами лимиты задаются в разделе <Link to="/limits">«Лимиты»</Link>.
      </p>
    );
  }
  const limit = Number(month.limit_usd);
  const spent = Number(month.spent_usd);
  const share = limit > 0 ? Math.min(100, Math.round((spent / limit) * 100)) : 0;
  const byCalls = Number(data.month_calls_usd ?? 0);
  return (
    <div className="budget-bar">
      <div className="track">
        <div
          className={share >= 90 ? "fill danger" : share >= 70 ? "fill warn" : "fill"}
          style={{ width: `${share}%` }}
        />
      </div>
      <p className="muted">
        Израсходовано {share}% месячного бюджета.{" "}
        {data.forecast_date ? (
          <>
            При таком же темпе он закончится <strong>{shortDay(data.forecast_date)}</strong>.
          </>
        ) : (
          "Прогноз появится, когда накопится расход."
        )}
      </p>
      {byCalls > spent + 0.000001 && (
        <p className="muted">
          Процент считается по тому счёту, который останавливает бота при
          превышении лимита. По журналу вызовов за месяц потрачено чуть
          больше — ${byCalls.toFixed(4)}: обучение базы знаний и поиск по
          ней тратят деньги, но лимитом не ограничиваются. Прогноз даты
          считается по этой, большей сумме.
        </p>
      )}
    </div>
  );
}

export default function CostsPage() {
  const [days, setDays] = useState(30);
  const costs = useQuery({ queryKey: ["costs", days], queryFn: () => getCosts(days) });

  if (costs.isPending) return <p className="muted">Загрузка…</p>;
  if (costs.isError || !costs.data)
    return (
      <div className="error">
        Не удалось загрузить расходы.{" "}
        <button onClick={() => void costs.refetch()}>Повторить</button>
      </div>
    );

  const data = costs.data;
  const types = Object.keys(data.labels).filter((type) =>
    data.spend_by_day.some((d) => Number(d.by_call_type[type] ?? 0) > 0),
  );
  const series = types.map((type) => ({
    name: data.labels[type],
    color: TYPE_COLORS[type] ?? "#8a93a6",
    // Already in tenge: the owner thinks in tenge, not in dollars.
    points: data.spend_by_day.map((d) => ({
      label: shortDay(d.day),
      value: Number(d.by_call_type[type] ?? 0),
    })),
  }));
  const perDay = data.spend_by_day.length
    ? Number(data.total_kzt) / data.spend_by_day.length
    : 0;
  // Sandbox runs, translations of knowledge-base answers and teaching the
  // bot are paid by the owner's own clicks, not by answering customers.
  const adminKzt = data.spend_by_day.reduce(
    (sum, d) => sum + Number(d.by_call_type.admin ?? 0),
    0,
  );

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
            {shortDay(data.period.start)} — {shortDay(data.period.end)} (сутки по UTC)
          </span>
          <div className="spacer" />
          <a className="btn-link" href={exportUrl(days, "xlsx")}>
            <Icon name="download" size={15} /> Excel
          </a>
          <a className="btn-link" href={exportUrl(days, "csv")}>
            CSV
          </a>
        </div>

        <div className="metric-row">
          <div className="metric">
            <span className="value">{money(data.total_kzt)}</span>
            <span className="caption">Всего за период</span>
            <span className="sub">${Number(data.total_usd).toFixed(4)}</span>
          </div>
          <div className="metric">
            <span className="value">{money(perDay)}</span>
            <span className="caption">В среднем в день</span>
          </div>
          <div className="metric">
            <span className="value">{money(Number(data.total_kzt) - adminKzt)}</span>
            <span className="caption">Из них на ответы клиентам</span>
            <span className="sub">
              моя работа в панели: {money(adminKzt)}
            </span>
          </div>
        </div>

        <StackedBarChart
          series={series}
          unit=" ₸"
          emptyText="За этот период бот ничего не тратил."
        />
        <BudgetBar data={data.budget} />
      </section>

      <section className="card">
        <h2>На что уходят деньги</h2>
        {data.by_model.length === 0 ? (
          <p className="muted">Пока не на что: платных вызовов не было.</p>
        ) : (
          <table className="plain-table">
            <thead>
              <tr>
                <th>Что делал бот</th>
                <th>Модель</th>
                <th>Вызовов</th>
                <th>Расход</th>
              </tr>
            </thead>
            <tbody>
              {data.by_model.map((row) => (
                <tr key={`${row.model}-${row.call_type}`}>
                  <td>{data.labels[row.call_type] ?? row.call_type}</td>
                  <td className="muted">{row.model}</td>
                  <td>{row.calls}</td>
                  <td>{money(row.cost_kzt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Самые дорогие диалоги</h2>
        {data.top_dialogs.length === 0 ? (
          <p className="muted">Пока ни одного платного диалога.</p>
        ) : (
          <table className="plain-table">
            <thead>
              <tr>
                <th>Клиент</th>
                <th>Вызовов</th>
                <th>Стоимость</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.top_dialogs.map((row) => (
                <tr key={row.chat_id}>
                  <td>{row.display_name || row.phone}</td>
                  <td>{row.calls}</td>
                  <td>{money(row.cost_kzt)}</td>
                  <td>
                    <Link to={`/?chat=${row.chat_id}`}>Открыть чат</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Из чего складывается расход</h2>
        <p className="muted">
          Токены — это единицы текста, за которые считает OpenAI. «Из кэша» дешевле обычных
          входящих, «размышления» тарифицируются как исходящие.
        </p>
        <div className="table-scroll">
          <table className="plain-table">
            <thead>
              <tr>
                <th>Дата</th>
                <th>Входящие</th>
                <th>Из них кэш</th>
                <th>Исходящие</th>
                <th>Размышления</th>
                <th>Аудио, сек</th>
                <th>Расход</th>
              </tr>
            </thead>
            <tbody>
              {data.tokens_by_day.map((row) => (
                <tr key={row.day}>
                  <td>{shortDay(row.day)}</td>
                  <td>{row.input_tokens.toLocaleString("ru-RU")}</td>
                  <td className="muted">{row.cached_input_tokens.toLocaleString("ru-RU")}</td>
                  <td>{row.output_tokens.toLocaleString("ru-RU")}</td>
                  <td className="muted">{row.reasoning_tokens.toLocaleString("ru-RU")}</td>
                  <td>{row.audio_seconds}</td>
                  <td>{money(row.cost_kzt)}</td>
                </tr>
              ))}
              {data.tokens_by_day.length === 0 && (
                <tr>
                  <td colSpan={7} className="muted">
                    За этот период платных вызовов не было.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
