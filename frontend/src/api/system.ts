import { api } from "./client";

export interface AiSwitch {
  enabled: boolean;
}

export const getAiSwitch = () => api<AiSwitch>("GET", "/api/system/ai-switch");

export const setAiSwitch = (enabled: boolean) =>
  api<AiSwitch>("PUT", "/api/system/ai-switch", { enabled });
