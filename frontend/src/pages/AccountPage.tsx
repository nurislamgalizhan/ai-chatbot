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
        ) : (
          <span className="muted">Загрузка…</span>
        )}
      </div>
    </div>
  );
}
