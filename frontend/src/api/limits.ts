import { api } from "./client";

export interface LimitsSettings {
  max_input_tokens: number;
  max_output_tokens: number;
  max_ai_calls_per_message: number;
  max_ai_cost_per_dialog_kzt: number;
  max_ai_cost_per_user_per_day_kzt: number;
  daily_ai_budget_kzt: number;
  monthly_ai_budget_kzt: number;
  debounce_seconds: number;
  max_messages_per_user_per_minute: number;
  max_messages_per_user_per_hour: number;
  spam_repeat_threshold: number;
  ai_max_message_age_minutes: number;
  circuit_breaker_failures: number;
  circuit_breaker_cooldown_minutes: number;
  semantic_cache_enabled: boolean;
  semantic_cache_ttl_hours: number;
  allowed_link_domains: string[];
  transcription_enabled: boolean;
  vision_enabled: boolean;
  max_audio_seconds: number;
  max_audio_file_mb: number;
  max_images_per_dialog: number;
  max_image_file_mb: number;
  media_retention_days: number;
  transcription_terms: string;
}

export interface PriceItem {
  model: string;
  unit: string;
  price_usd: string;
  effective_from: string;
}

export interface LedgerSnapshot {
  scope: string;
  period: string;
  limit_usd: string;
  spent_usd: string;
  reserved_usd: string;
}

export interface LimitsResponse {
  settings: LimitsSettings;
  prices: PriceItem[];
  usd_kzt_rate: string | null;
  ledgers: LedgerSnapshot[];
  dry_run: boolean;
}

export const getLimits = () => api<LimitsResponse>("GET", "/api/limits");

export const updateLimits = (settings: LimitsSettings) =>
  api<LimitsResponse>("PUT", "/api/limits", { settings });

export const setFxRate = (rate: string) =>
  api<{ ok: boolean }>("POST", "/api/limits/fx", { rate });

export const setPrice = (model: string, unit: string, priceUsd: string) =>
  api<{ ok: boolean }>("POST", "/api/limits/prices", {
    model,
    unit,
    price_usd: priceUsd,
  });
