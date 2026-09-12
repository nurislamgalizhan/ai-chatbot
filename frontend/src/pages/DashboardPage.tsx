import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { getDashboard } from "../api/analytics";
import Icon from "../components/icons";

function tenge(value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "0 ₸";
  return `${amount.toLocaleString("ru-RU", { maximumFractionDigits: amount < 1 ? 2 : 0 })} ₸`;
}

/** The first screen: what needs me today, and what is it costing. */
export default function DashboardPage() {
  const data = useQuery({
    queryKey: ["dashboard"],
    queryFn: getDashboard,
    refetchInterval: 30_000,
  });

  if (data.isPending) return <p className="muted">Загрузка…</p>;
  if (data.isError || !data.data)
    return (
      <div className="error">
        Не удалось загрузить сводку.{" "}
        <button onClick={() => void data.refetch()}>Повторить</button>
      </div>
    );

  const d = data.data;
  const month = d.budget.month;
  const share =
    month && Number(month.limit_usd) > 0
      ? Math.min(100, Math.round((Number(month.spent_usd) / Number(month.limit_usd)) * 100))
      : 0;

  return (
    <div className="report-page">
      {d.dry_run && (
        <div className="plaque warn">
          <strong>Режим приёмки:</strong> бот обрабатывает сообщения и тратит деньги на OpenAI,
          но НЕ отправляет ответы клиентам. Реальная отправка включается отдельно.
        </div>
      )}
      {!d.ai_enabled && (
        <div className="plaque danger">
          <strong>AI выключен глобально.</strong> Клиентам никто не отвечает автоматически.
        </div>
      )}

      <section className="card">
        <h2>Сегодня</h2>
        <div className="metric-row">
          <div className="metric">
            <span className="value">{d.today.incoming}</span>
            <span className="caption">Написали клиенты</span>
          </div>
          <div className="metric">
            <span className="value">{d.today.answered_by_ai}</span>
            <span className="caption">Ответил бот</span>
          </div>
          <div className="metric">
            <span className="value">{d.today.handed_to_human}</span>
            <span className="caption">Передано менеджеру</span>
          </div>
          <div className="metric">
            <span className="value">{tenge(d.spend_today_kzt)}</span>
            <span className="caption">Потрачено сегодня</span>
            <span className="sub">за месяц {tenge(d.spend_month_kzt)}</span>
          </div>
        </div>
        {month && (
          <p className="muted">
            Израсходовано {share}% месячного бюджета
            {d.budget.forecast_date ? (
              <>
                {" "}
                · при таком темпе закончится {d.budget.forecast_date.slice(8, 10)}.
                {d.budget.forecast_date.slice(5, 7)}
              </>
            ) : null}
            . <Link to="/costs">Подробнее о расходах</Link>
          </p>
        )}
      </section>

      <section className="card">
        <h2>Требует внимания</h2>
        <div className="metric-row">
          <div className="metric">
            <span className={d.needs_attention > 0 ? "value warn" : "value"}>
              {d.needs_attention}
            </span>
            <span className="caption">Чатов ждут человека</span>
          </div>
          <div className="metric">
            <span className="value">{d.unread_chats}</span>
            <span className="caption">Непрочитанных чатов</span>
          </div>
          <div className="metric">
            <span className="value">{d.queue_depth}</span>
            <span className="caption">Сообщений в очереди</span>
          </div>
          <div className="metric">
            <span className={d.failures_24h > 0 ? "value danger" : "value"}>
              {d.failures_24h}
            </span>
            <span className="caption">Сбоев за сутки</span>
          </div>
        </div>
        <div className="actions-row">
          <Link className="btn-link" to="/?needs_attention=1">
            <Icon name="chat" size={15} /> Открыть чаты, ждущие ответа
          </Link>
          <Link className="btn-link" to="/knowledge">
            <Icon name="book" size={15} /> Добавить ответ в базу знаний
          </Link>
        </div>
      </section>
    </div>
  );
}
