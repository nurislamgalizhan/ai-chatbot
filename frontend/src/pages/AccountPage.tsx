import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../api/client";
import {
  changePassword,
  getAccount,
  listSessions,
  revokeOtherSessions,
  revokeSession,
} from "../api/auth";
import {
  getJournal,
  getLoginHistory,
  getRecoveryCodesStatus,
  regenerateRecoveryCodes,
} from "../api/system";

function formatDate(value: string): string {
  return new Date(value).toLocaleString("ru-RU");
}

export default function AccountPage() {
  const queryClient = useQueryClient();
  const account = useQuery({ queryKey: ["account"], queryFn: getAccount });
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: listSessions });

  const revokeOne = useMutation({
    mutationFn: revokeSession,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sessions"] }),
  });
  const revokeOthers = useMutation({
    mutationFn: revokeOtherSessions,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sessions"] }),
  });

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordMessage, setPasswordMessage] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const passwordChange = useMutation({
    mutationFn: () => changePassword(currentPassword, newPassword),
    onSuccess: () => {
      setPasswordMessage({ kind: "success", text: "Пароль обновлён" });
      setCurrentPassword("");
      setNewPassword("");
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (err) =>
      setPasswordMessage({
        kind: "error",
        text: err instanceof ApiError ? err.message : "Сервер недоступен",
      }),
  });

  return (
    <div className="page-narrow">
      <div className="panel">
        <h2>Администратор</h2>
        {account.data ? (
          <table>
            <tbody>
              <tr>
                <td className="muted">Телефон</td>
                <td>{account.data.phone}</td>
              </tr>
              <tr>
                <td className="muted">Имя</td>
                <td>{account.data.full_name}</td>
              </tr>
              <tr>
                <td className="muted">Пароль изменён</td>
                <td>{formatDate(account.data.password_changed_at)}</td>
              </tr>
            </tbody>
          </table>
        ) : account.isError ? (
          <span className="error">Не удалось загрузить данные учётной записи</span>
        ) : (
          <span className="muted">Загрузка…</span>
        )}
      </div>

      <div className="panel">
        <h2>Смена пароля</h2>
        <form
          className="narrow"
          onSubmit={(e) => {
            e.preventDefault();
            setPasswordMessage(null);
            passwordChange.mutate();
          }}
        >
          <label htmlFor="current">Текущий пароль</label>
          <input
            id="current"
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
          />
          <label htmlFor="new">Новый пароль (мин. 12 символов)</label>
          <input
            id="new"
            type="password"
            autoComplete="new-password"
            minLength={12}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
          />
          <div style={{ marginTop: 16 }}>
            <button className="primary" disabled={passwordChange.isPending}>
              Сменить пароль
            </button>
          </div>
          {passwordMessage && (
            <div className={passwordMessage.kind}>{passwordMessage.text}</div>
          )}
        </form>
        <p className="muted" style={{ marginBottom: 0 }}>
          После смены пароля все остальные сессии будут завершены.
        </p>
      </div>

      <div className="panel">
        <h2>Активные сессии</h2>
        {sessions.data ? (
          <>
            <table>
              <thead>
                <tr>
                  <th>Устройство</th>
                  <th>IP</th>
                  <th>Вход</th>
                  <th>Активность</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {sessions.data.sessions.map((s) => (
                  <tr key={s.id}>
                    <td>
                      {s.user_agent ?? "—"}{" "}
                      {s.is_current && <span className="tag ok">текущая</span>}
                    </td>
                    <td>{s.ip ?? "—"}</td>
                    <td>{formatDate(s.created_at)}</td>
                    <td>{formatDate(s.last_seen_at)}</td>
                    <td>
                      {!s.is_current && (
                        <button
                          className="danger"
                          disabled={revokeOne.isPending}
                          onClick={() => revokeOne.mutate(s.id)}
                        >
                          Завершить
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {sessions.data.sessions.length > 1 && (
              <div style={{ marginTop: 14 }}>
                <button
                  className="danger"
                  disabled={revokeOthers.isPending}
                  onClick={() => revokeOthers.mutate()}
                >
                  Завершить все остальные сессии
                </button>
              </div>
            )}
          </>
        ) : sessions.isError ? (
          <span className="error">Не удалось загрузить список сессий</span>
        ) : (
          <span className="muted">Загрузка…</span>
        )}
      </div>

      <RecoveryCodesCard />
      <LoginHistoryCard />
      <JournalCard />
    </div>
  );
}

/** Recovery codes: the only way back in when the phone with the
 *  authenticator is lost. Shown once — only hashes are stored. */
function RecoveryCodesCard() {
  const [codes, setCodes] = useState<string[] | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const status = useQuery({
    queryKey: ["recovery-codes-status"],
    queryFn: getRecoveryCodesStatus,
  });
  const regenerate = useMutation({
    mutationFn: () => regenerateRecoveryCodes(password),
    onSuccess: (data) => {
      setCodes(data.codes);
      setPassword("");
      setError(null);
      void status.refetch();
    },
    onError: (err) =>
      setError(
        err instanceof ApiError && err.status === 403
          ? "Текущий пароль неверен"
          : "Не удалось выпустить коды",
      ),
  });

  return (
    <div className="card" style={{ marginTop: 18 }}>
      <h2>Коды восстановления</h2>
      <p className="muted">
        Каждый код срабатывает один раз вместо кода из приложения. Нужны, если
        телефон с приложением потерян.
      </p>
      {status.data && (
        <p>
          Осталось неиспользованных: <strong>{status.data.unused}</strong> из{" "}
          {status.data.total}
        </p>
      )}
      {codes ? (
        <>
          <div className="plaque warn">
            Сохраните коды сейчас — показать их снова невозможно. Старые коды
            больше не работают.
          </div>
          <pre className="codes-block">{codes.join("\n")}</pre>
        </>
      ) : (
        <>
          <label className="limit-field">
            <span>Текущий пароль</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
            <span className="muted">
              Коды работают вместо второго фактора, поэтому для выпуска нужен пароль.
            </span>
          </label>
          {error && <div className="error">{error}</div>}
          <button
            type="button"
            disabled={regenerate.isPending || password.length === 0}
            onClick={() => {
              if (
                window.confirm(
                  "Выпустить новые коды? Все прежние коды перестанут работать.",
                )
              ) {
                regenerate.mutate();
              }
            }}
          >
            Выпустить новые коды
          </button>
        </>
      )}
    </div>
  );
}

function LoginHistoryCard() {
  const history = useQuery({ queryKey: ["login-history"], queryFn: getLoginHistory });
  return (
    <div className="card" style={{ marginTop: 18 }}>
      <h2>Входы в панель</h2>
      {history.data?.last_success_at && (
        <p className="muted">
          Последний успешный вход: {formatDate(history.data.last_success_at)}
        </p>
      )}
      {history.isPending ? (
        <span className="muted">Загрузка…</span>
      ) : !history.data || history.data.items.length === 0 ? (
        <span className="muted">Записей пока нет.</span>
      ) : (
        <table className="plain-table">
          <thead>
            <tr>
              <th>Когда</th>
              <th>Что произошло</th>
              <th>IP</th>
            </tr>
          </thead>
          <tbody>
            {history.data.items.map((row) => (
              <tr key={row.id}>
                <td className="muted">{formatDate(row.created_at)}</td>
                <td>{row.outcome}</td>
                <td className="muted">{row.ip ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** SPEC §12.8: what changed in settings, limits, knowledge base and chat
 *  modes — the record that answers "почему бот стал отвечать иначе". */
function JournalCard() {
  const [filter, setFilter] = useState("");
  const journal = useQuery({
    queryKey: ["journal", filter],
    queryFn: () => getJournal(filter),
  });
  const FILTERS: { value: string; label: string }[] = [
    { value: "", label: "Всё" },
    { value: "kb.", label: "База знаний" },
    { value: "limits.", label: "Лимиты" },
    { value: "agent.", label: "Агент" },
    { value: "chat.", label: "Диалоги" },
    { value: "auth.", label: "Входы" },
  ];

  return (
    <div className="card" style={{ marginTop: 18 }}>
      <div className="toolbar">
        <h2>Журнал изменений</h2>
        <div className="spacer" />
        <div className="segmented">
          {FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={filter === item.value ? "active" : ""}
              onClick={() => setFilter(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
      {journal.isPending ? (
        <span className="muted">Загрузка…</span>
      ) : !journal.data || journal.data.items.length === 0 ? (
        <span className="muted">Записей нет.</span>
      ) : (
        <div className="table-scroll">
          <table className="plain-table">
            <thead>
              <tr>
                <th>Когда</th>
                <th>Действие</th>
                <th>Объект</th>
                <th>IP</th>
              </tr>
            </thead>
            <tbody>
              {journal.data.items.map((row) => (
                <tr key={row.id}>
                  <td className="muted">{formatDate(row.created_at)}</td>
                  <td>{row.summary}</td>
                  <td className="muted">
                    {row.entity_type ? `${row.entity_type} ${row.entity_id ?? ""}` : "—"}
                  </td>
                  <td className="muted">{row.ip ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
