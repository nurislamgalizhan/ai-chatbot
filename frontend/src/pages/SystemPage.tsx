import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  checkConnection,
  getSystemEvents,
  getSystemStatus,
  sendTestAlert,
  type SystemStatus,
} from "../api/system";
import Icon from "../components/icons";

/** Owner-facing wording for technical events. An unknown event shows its
 *  own code: better an unfamiliar word than a made-up explanation. */
const EVENT_LABELS: Record<string, string> = {
  poll_failed: "Нет связи с WhatsApp",
  instance_state_checked: "Проверка подключения",
  inbound_processing_failed: "Не удалось разобрать входящее",
  inbound_parse_failed: "Непонятное уведомление от WhatsApp",
  send_failed_final: "Сообщение не отправлено",
  circuit_breaker_opened: "ИИ приостановлен из-за ошибок OpenAI",
  budget_blocked: "Достигнут лимит расходов",
  budget_day_50pct: "Потрачено 50% дневного бюджета",
  budget_day_80pct: "Потрачено 80% дневного бюджета",
  budget_day_90pct: "Потрачено 90% дневного бюджета",
  budget_day_100pct: "Дневной бюджет исчерпан",
  budget_month_50pct: "Потрачено 50% месячного бюджета",
  budget_month_80pct: "Потрачено 80% месячного бюджета",
  budget_month_90pct: "Потрачено 90% месячного бюджета",
  budget_month_100pct: "Месячный бюджет исчерпан",
  disk_low: "Мало места на диске",
  queue_growing: "Очередь растёт",
  backup_stale: "Давно не было резервной копии",
  backup_completed: "Создана резервная копия",
  media_disk_low: "Мало места: файлы не принимаются",
  already_charged: "Оплаченный ответ не сохранился — диалог у менеджера",
  ai_not_configured: "ИИ не настроен",
  alert_delivery_failed: "Не удалось доставить оповещение",
};

const ALERT_STATUS: Record<string, string> = {
  pending: "в очереди",
  queued: "отправляется",
  sent: "доставлено",
  failed: "не доставлено",
  skipped: "не отправлено",
};

function when(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function TransportCard({ data }: { data: SystemStatus }) {
  const queryClient = useQueryClient();
  const [result, setResult] = useState<string | null>(null);
  const check = useMutation({
    mutationFn: checkConnection,
    onSuccess: (r) => {
      setResult(
        r.ok
          ? "Подключение работает: инстанс авторизован."
          : r.error === "not_configured"
            ? "Ключи WhatsApp не заданы в настройках сервера (.env)."
            : `Инстанс отвечает, но не готов: ${r.state ?? "нет ответа"}.`,
      );
      void queryClient.invalidateQueries({ queryKey: ["system-status"] });
    },
    onError: () => setResult("Не удалось связаться с WhatsApp."),
  });

  const t = data.transport;
  return (
    <section className="card">
      <h2>Связь с WhatsApp</h2>
      <div className="kv-grid">
        <div>
          <span className="caption">Номер-инстанс</span>
          <span className="value-sm">{t.instance_id || "не задан"}</span>
        </div>
        <div>
          <span className="caption">Токен</span>
          <span className="value-sm">{t.token_hint || "не задан"}</span>
        </div>
        <div>
          <span className="caption">Состояние</span>
          <span className="value-sm">
            {t.state === "authorized" ? (
              <span className="chip ok">авторизован</span>
            ) : t.state ? (
              <span className="chip warn">{t.state}</span>
            ) : (
              "не проверялось"
            )}
          </span>
        </div>
        <div>
          <span className="caption">Последний приём сообщений</span>
          <span className="value-sm">{when(t.last_polled_at)}</span>
        </div>
      </div>
      {t.last_error && (
        <div className="plaque warn">Последняя ошибка связи: {t.last_error}</div>
      )}
      <div className="row-actions">
        <button type="button" onClick={() => check.mutate()} disabled={check.isPending}>
          {check.isPending ? "Проверяем…" : "Проверить подключение"}
        </button>
        {result && <span className="muted">{result}</span>}
      </div>
    </section>
  );
}

function AlertsCard({ data }: { data: SystemStatus }) {
  const queryClient = useQueryClient();
  const [sent, setSent] = useState<string | null>(null);
  const test = useMutation({
    mutationFn: sendTestAlert,
    onSuccess: (r) => {
      setSent(
        r.will_send
          ? "Поставлено в очередь — придёт в WhatsApp в течение минуты."
          : r.reason === "no_phone"
            ? "Оповещение записано, но отправить его некуда: номер не указан."
            : "Оповещение записано, но отправка выключена в «Лимитах».",
      );
      void queryClient.invalidateQueries({ queryKey: ["system-status"] });
    },
  });

  return (
    <section className="card">
      <h2>Оповещения владельцу</h2>
      {data.alerts.phone_hint ? (
        <p className="muted">
          Отправляются в WhatsApp на номер {data.alerts.phone_hint}.{" "}
          {data.alerts.enabled ? "" : "Сейчас отключены в «Лимитах»."}
        </p>
      ) : (
        <div className="plaque warn">
          Номер для оповещений не указан — бот копит их здесь и никому не пишет.
          Укажите свой номер в разделе <Link to="/limits">«Лимиты»</Link>, чтобы
          узнавать об исчерпанном бюджете и потере связи сразу.
        </div>
      )}
      <div className="metric-row">
        <div className="metric">
          <span className="value">{data.alerts.pending}</span>
          <span className="caption">В очереди</span>
        </div>
        <div className="metric">
          <span className={data.alerts.failed > 0 ? "value warn" : "value"}>
            {data.alerts.failed}
          </span>
          <span className="caption">Не доставлено</span>
        </div>
      </div>
      {data.alerts.recent.length > 0 && (
        <table className="plain-table">
          <thead>
            <tr>
              <th>Когда</th>
              <th>Что произошло</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {data.alerts.recent.map((row) => (
              <tr key={row.id}>
                <td className="muted">{when(row.created_at)}</td>
                <td>{row.body}</td>
                <td className="muted">
                  {ALERT_STATUS[row.status] ?? row.status}
                  {row.last_error ? ` (${row.last_error})` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row-actions">
        <button type="button" onClick={() => test.mutate()} disabled={test.isPending}>
          Отправить тестовое оповещение
        </button>
        {sent && <span className="muted">{sent}</span>}
      </div>
    </section>
  );
}

export default function SystemPage() {
  const [level, setLevel] = useState<"all" | "error">("all");
  const status = useQuery({
    queryKey: ["system-status"],
    queryFn: getSystemStatus,
    refetchInterval: 30_000,
  });
  const events = useQuery({
    queryKey: ["system-events", level],
    queryFn: () => getSystemEvents(level),
    refetchInterval: 60_000,
  });

  if (status.isPending) return <p className="muted">Загрузка…</p>;
  if (status.isError || !status.data)
    return (
      <div className="error">
        Не удалось загрузить состояние системы.{" "}
        <button onClick={() => void status.refetch()}>Повторить</button>
      </div>
    );

  const data = status.data;
  const backupStale =
    data.backup.age_hours !== null && data.backup.age_hours >= data.backup.stale_after_hours;

  return (
    <div className="report-page">
      {data.dry_run && (
        <div className="plaque warn">
          <strong>Режим приёмки (DRY_RUN):</strong> сообщения клиентам обрабатываются,
          но не отправляются. Оповещения вам приходят как обычно.
        </div>
      )}
      {data.emergency_ai_off && (
        <div className="plaque danger">
          ИИ выключен глобально — на все сообщения отвечают менеджеры.
        </div>
      )}

      <TransportCard data={data} />

      <section className="card">
        <h2>Очереди</h2>
        <p className="muted">
          Если эти числа держатся большими — бот не успевает или что-то мешает
          обработке.
        </p>
        <div className="metric-row">
          <div className="metric">
            <span className="value">{data.queues.queue_depth}</span>
            <span className="caption">Ждут ответа ИИ</span>
            {data.queues.oldest_queued_minutes > 0 && (
              <span className="sub">самое старое: {data.queues.oldest_queued_minutes} мин</span>
            )}
          </div>
          <div className="metric">
            <span className="value">{data.queues.inbox_backlog}</span>
            <span className="caption">Не разобрано от WhatsApp</span>
          </div>
          <div className="metric">
            <span className="value">{data.queues.outbox_pending}</span>
            <span className="caption">Ждут отправки</span>
          </div>
          <div className="metric">
            <span className={data.queues.failed_24h > 0 ? "value warn" : "value"}>
              {data.queues.failed_24h}
            </span>
            <span className="caption">Ошибок за сутки</span>
          </div>
        </div>
      </section>

      <section className="card">
        <h2>Сервер и данные</h2>
        <div className="metric-row">
          <div className="metric">
            <span className={data.disk.used_percent >= 85 ? "value warn" : "value"}>
              {data.disk.used_percent}%
            </span>
            <span className="caption">Занято на диске</span>
            <span className="sub">
              свободно {data.disk.free_gb} ГБ из {data.disk.total_gb} ГБ
            </span>
          </div>
          <div className="metric">
            <span className="value">{data.disk.database_mb} МБ</span>
            <span className="caption">Размер базы</span>
          </div>
          <div className="metric">
            <span className={backupStale ? "value warn" : "value"}>
              {data.backup.last_at ? when(data.backup.last_at) : "нет"}
            </span>
            <span className="caption">Последняя резервная копия</span>
            <span className="sub">
              {data.backup.last_at
                ? `${data.backup.age_hours} ч назад`
                : "копии ещё не делались"}
            </span>
          </div>
        </div>
        {!data.backup.last_at && (
          <div className="plaque warn">
            Резервное копирование не настроено на сервере. Пока оно не включено,
            потеря диска означает потерю всей переписки и расходов.
          </div>
        )}
        <p className="muted">
          Модель: <strong>{data.model}</strong>
          {data.openai_configured ? " · ключ OpenAI задан" : " · ключ OpenAI не задан"}
        </p>
      </section>

      <AlertsCard data={data} />

      <section className="card">
        <div className="toolbar">
          <h2>Технические события</h2>
          <div className="spacer" />
          <div className="segmented">
            <button
              type="button"
              className={level === "all" ? "active" : ""}
              onClick={() => setLevel("all")}
            >
              Все
            </button>
            <button
              type="button"
              className={level === "error" ? "active" : ""}
              onClick={() => setLevel("error")}
            >
              Только ошибки
            </button>
          </div>
        </div>
        {events.isPending ? (
          <p className="muted">Загрузка…</p>
        ) : !events.data || events.data.items.length === 0 ? (
          <p className="muted">
            <Icon name="check" size={14} /> Событий нет — значит, всё спокойно.
          </p>
        ) : (
          <div className="table-scroll">
            <table className="plain-table">
              <thead>
                <tr>
                  <th>Когда</th>
                  <th>Что</th>
                  <th>Где</th>
                  <th>Подробности</th>
                </tr>
              </thead>
              <tbody>
                {events.data.items.map((row) => (
                  <tr key={row.id}>
                    <td className="muted">{when(row.created_at)}</td>
                    <td>
                      {(row.level === "error" || row.level === "critical") && (
                        <span className="chip warn">сбой</span>
                      )}{" "}
                      {EVENT_LABELS[row.event] ?? row.event}
                    </td>
                    <td className="muted">{row.component}</td>
                    <td className="muted">
                      {Object.entries(row.detail)
                        .map(([key, value]) => `${key}: ${String(value)}`)
                        .join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
