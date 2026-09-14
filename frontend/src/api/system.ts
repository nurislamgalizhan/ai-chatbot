import { api } from "./client";

export interface AiSwitch {
  enabled: boolean;
}

export const getAiSwitch = () => api<AiSwitch>("GET", "/api/system/ai-switch");

export const setAiSwitch = (enabled: boolean) =>
  api<AiSwitch>("PUT", "/api/system/ai-switch", { enabled });

export interface SystemStatus {
  dry_run: boolean;
  emergency_ai_off: boolean;
  transport: {
    configured: boolean;
    instance_id: string;
    token_hint: string;
    last_polled_at: string | null;
    last_error: string | null;
    state: string | null;
    state_checked_at: string | null;
    settings_checked_at: string | null;
    settings_problems: string[];
    provider_settings: Record<string, unknown>;
  };
  model: string;
  openai_configured: boolean;
  queues: {
    queue_depth: number;
    oldest_queued_minutes: number;
    inbox_backlog: number;
    outbox_pending: number;
    failed_24h: number;
  };
  disk: {
    used_percent: number;
    free_gb: number;
    total_gb: number;
    database_mb: number;
  };
  backup: {
    last_at: string | null;
    age_hours: number | null;
    stale_after_hours: number;
  };
  alerts: {
    enabled: boolean;
    phone_hint: string;
    pending: number;
    failed: number;
    recent: {
      id: number;
      kind: string;
      event_key: string;
      body: string;
      status: string;
      created_at: string;
      sent_at: string | null;
      last_error: string | null;
    }[];
  };
}

export interface SystemEventRow {
  id: number;
  level: string;
  component: string;
  event: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface AuditRow {
  id: number;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  ip: string | null;
  created_at: string;
  summary: string;
}

export interface LoginAttemptRow {
  id: number;
  created_at: string;
  ip: string | null;
  outcome: string;
}

export const getSystemStatus = () => api<SystemStatus>("GET", "/api/system/status");

export const getSystemEvents = (level: "all" | "error", limit = 50) =>
  api<{ items: SystemEventRow[] }>(
    "GET",
    `/api/system/events?level=${level}&limit=${limit}`,
  );

export const checkConnection = () =>
  api<{
    ok: boolean;
    state: string | null;
    error: string | null;
    problems: string[];
  }>("POST", "/api/system/check-connection");

export const sendTestAlert = () =>
  api<{ queued: boolean; will_send: boolean; reason: string | null }>(
    "POST",
    "/api/system/test-alert",
  );

export const getJournal = (action: string, cursor?: number) =>
  api<{ items: AuditRow[]; next_cursor: number | null }>(
    "GET",
    `/api/account/journal?${new URLSearchParams({
      ...(action ? { action } : {}),
      ...(cursor ? { cursor: String(cursor) } : {}),
    })}`,
  );

export const getLoginHistory = () =>
  api<{ items: LoginAttemptRow[]; last_success_at: string | null }>(
    "GET",
    "/api/account/login-history",
  );

export const getRecoveryCodesStatus = () =>
  api<{ unused: number; total: number }>("GET", "/api/account/recovery-codes/status");

export const regenerateRecoveryCodes = (currentPassword: string) =>
  api<{ codes: string[] }>("POST", "/api/account/recovery-codes", {
    current_password: currentPassword,
  });
