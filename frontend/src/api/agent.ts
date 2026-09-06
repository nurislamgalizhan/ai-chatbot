import { api } from "./client";

export interface FallbackTemplates {
  [key: string]: { [lang: string]: string };
}

export interface InstructionPayload {
  system_prompt: string;
  style: string | null;
  fallback_templates: FallbackTemplates;
  escalation_rules: Record<string, unknown> | null;
  max_output_tokens: number | null;
  reasoning_effort: string | null;
}

export interface Instruction extends InstructionPayload {
  id: number;
  version: number;
  is_active: boolean;
  created_at: string;
}

export interface InstructionList {
  items: Instruction[];
  active_version: number | null;
}

export interface SandboxUsage {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
}

export interface SandboxResult {
  kind: string;
  text: string | null;
  error_code: string | null;
  cost_usd: string;
  cost_kzt: string;
  usage: SandboxUsage | null;
  latency_ms: number | null;
  kb_chunk_ids: number[];
  instruction_version: number | null;
  model: string | null;
  detected_language: string;
}

export const listInstructions = () =>
  api<InstructionList>("GET", "/api/agent/instructions");

export const createInstruction = (payload: InstructionPayload) =>
  api<Instruction>("POST", "/api/agent/instructions", payload);

export const activateInstruction = (id: number) =>
  api<Instruction>("POST", `/api/agent/instructions/${id}/activate`);

export const runSandbox = (question: string) =>
  api<SandboxResult>("POST", "/api/agent/sandbox", { question });
