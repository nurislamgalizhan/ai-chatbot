import { api } from "./client";

export interface LoginResponse {
  challenge_token: string;
  totp_required: boolean;
}

export interface Account {
  phone: string;
  full_name: string;
  password_changed_at: string;
}

export interface SessionInfo {
  id: number;
  user_agent: string | null;
  ip: string | null;
  created_at: string;
  last_seen_at: string;
  is_current: boolean;
}

export const login = (phone: string, password: string) =>
  api<LoginResponse>("POST", "/api/auth/login", { phone, password });

export const verifyTotp = (challenge_token: string, code: string) =>
  api<{ status: string }>("POST", "/api/auth/verify-totp", {
    challenge_token,
    code,
  });

export const logout = () => api<{ status: string }>("POST", "/api/auth/logout");

export const getAccount = () => api<Account>("GET", "/api/account");

export const listSessions = () =>
  api<{ sessions: SessionInfo[] }>("GET", "/api/account/sessions");

export const revokeSession = (id: number) =>
  api<{ status: string }>("POST", `/api/account/sessions/${id}/revoke`);

export const revokeOtherSessions = () =>
  api<{ status: string }>("POST", "/api/account/sessions/revoke-others");

export const changePassword = (current_password: string, new_password: string) =>
  api<{ status: string }>("POST", "/api/account/change-password", {
    current_password,
    new_password,
  });
