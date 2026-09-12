import { api } from "./client";

export interface PeriodInfo {
  start: string;
  end: string;
  days: number;
  timezone: string;
}

export interface DaySpend {
  day: string;
  by_call_type: Record<string, string>;
  total_usd: string;
  total_kzt: string;
}

export interface DayTokens {
  day: string;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  audio_seconds: number;
  cost_usd: string;
  cost_kzt: string;
}

export interface BudgetPeriod {
  limit_usd: string;
  spent_usd: string;
  reserved_usd: string;
  period_start: string;
  period_end: string;
}

export interface BudgetState {
  day: BudgetPeriod | null;
  month: BudgetPeriod | null;
  forecast_date: string | null;
  /** Everything the call journal says was paid this month. Larger than
   *  month.spent_usd, which only counts spending that went through a
   *  budget reservation. */
  month_calls_usd: string;
}

export interface CostsResponse {
  period: PeriodInfo;
  labels: Record<string, string>;
  spend_by_day: DaySpend[];
  tokens_by_day: DayTokens[];
  by_model: {
    model: string;
    call_type: string;
    calls: number;
    cost_usd: string;
    cost_kzt: string;
  }[];
  top_dialogs: {
    chat_id: number;
    phone: string;
    display_name: string | null;
    calls: number;
    cost_usd: string;
    cost_kzt: string;
  }[];
  budget: BudgetState;
  total_usd: string;
  total_kzt: string;
}

export interface Workload {
  incoming: number;
  answered_by_ai: number;
  handed_to_human: number;
  ignored: number;
  replies_from_panel: number;
  replies_from_phone: number;
  answerable: number;
  automation_percent: number;
}

export interface AnalyticsResponse {
  period: PeriodInfo;
  workload: Workload;
  dialogs: {
    total: number;
    new: number;
    with_spend: number;
    average_cost_kzt: string;
  };
  answer_sources: Record<string, number>;
  errors: { code: string; count: number }[];
  semantic_cache_enabled: boolean;
  language_gaps: { language: string; faq_id: number | null; key: string | null; misses: number }[];
  not_measured: string[];
}

export interface DashboardResponse {
  queue_depth: number;
  needs_attention: number;
  unread_chats: number;
  failures_24h: number;
  spend_today_kzt: string;
  spend_month_kzt: string;
  today: Workload;
  budget: BudgetState;
  dry_run: boolean;
  ai_enabled: boolean;
}

export const getDashboard = () => api<DashboardResponse>("GET", "/api/analytics/dashboard");

export const getCosts = (days: number) =>
  api<CostsResponse>("GET", `/api/analytics/costs?days=${days}`);

export const getAnalytics = (days: number) =>
  api<AnalyticsResponse>("GET", `/api/analytics?days=${days}`);

export const exportUrl = (days: number, fmt: "csv" | "xlsx") =>
  `/api/analytics/costs/export?days=${days}&fmt=${fmt}`;
