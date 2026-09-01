import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { login, verifyTotp } from "../api/auth";
import Icon from "../components/icons";

export default function LoginPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState<"password" | "totp">("password");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challengeToken, setChallengeToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submitPassword(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await login(phone, password);
      setChallengeToken(result.challenge_token);
      setPassword("");
      setStep("totp");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Сервер недоступен");
    } finally {
      setBusy(false);
    }
  }

  async function submitTotp(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await verifyTotp(challengeToken, code.trim());
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Сервер недоступен");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="logo-tile">
          <Icon name="chat" size={22} />
        </div>
        <h1>Добро пожаловать</h1>
        <p className="sub">
          {step === "password"
            ? "Войдите, чтобы использовать админ-панель"
            : "Код из приложения-аутентификатора или код восстановления"}
        </p>
        {step === "password" ? (
          <form onSubmit={submitPassword}>
            <label htmlFor="phone">Телефон</label>
            <input
              id="phone"
              autoComplete="username"
              placeholder="+77011234567"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              required
            />
            <label htmlFor="password">Пароль</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              placeholder="Введите пароль"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <button className="primary" disabled={busy}>
              {busy ? "Проверка…" : "Далее →"}
            </button>
          </form>
        ) : (
          <form onSubmit={submitTotp}>
            <label htmlFor="code">Код подтверждения</label>
            <input
              id="code"
              autoComplete="one-time-code"
              inputMode="numeric"
              placeholder="000000"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              required
              autoFocus
            />
            <button className="primary" disabled={busy}>
              {busy ? "Проверка…" : "Войти →"}
            </button>
          </form>
        )}
        {error && <div className="error">{error}</div>}
      </div>
    </div>
  );
}
