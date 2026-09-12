/** Why a message got the answer it got, in the owner's words.
 *
 *  The worker stores a machine code in messages.error_code for every
 *  outcome that is not a plain model answer — including outcomes that are
 *  not failures at all (a topic refusal, a file handed to a manager).
 *  Showing the raw code as "Ошибка" would tell the owner his bot is
 *  broken every time it behaved exactly as configured. */

export type ReasonTone = "danger" | "warn" | "info";

export interface MessageReason {
  tone: ReasonTone;
  text: string;
}

const REASONS: Record<string, MessageReason> = {
  stale: {
    tone: "warn",
    text: "Сообщение старше часа — бот не отвечает, диалог у менеджера",
  },
  media_unsupported: {
    tone: "info",
    text: "Такой файл бот не читает — диалог передан менеджеру",
  },
  empty: { tone: "info", text: "В сообщении нет текста" },
  ai_off: { tone: "info", text: "В этом чате автоответы выключены" },
  rate_limited: {
    tone: "warn",
    text: "Слишком много сообщений подряд — бот сделал паузу",
  },
  spam: { tone: "warn", text: "Одинаковые сообщения подряд — бот не отвечает" },
  orphaned_processing: {
    tone: "danger",
    text: "Обработка прервалась — диалог передан менеджеру",
  },
  injection: {
    tone: "info",
    text: "Попытка изменить поведение бота — бот вежливо отказал",
  },
  offtopic: { tone: "info", text: "Вопрос не по теме — бот вежливо отказал" },
  too_long: { tone: "info", text: "Сообщение слишком длинное для бота" },
  prompt_leak: {
    tone: "warn",
    text: "Ответ модели был отклонён проверкой и заменён",
  },
  link_not_allowed: {
    tone: "warn",
    text: "Ответ модели содержал чужую ссылку и был заменён",
  },
  already_charged: {
    tone: "warn",
    text: "Файл уже был оплачен ранее — диалог передан менеджеру",
  },
};

const LIMIT_SCOPES: Record<string, string> = {
  global_day: "дневной лимит расходов",
  global_month: "месячный лимит расходов",
  chat: "лимит расходов на один диалог",
  user_day: "дневной лимит на одного клиента",
};

/** null = nothing worth showing next to the message. */
export function messageReason(
  code: string | null,
  state: string,
): MessageReason | null {
  if (!code) return null;
  if (code.startsWith("blocked_by_limit:")) {
    const scope = LIMIT_SCOPES[code.slice("blocked_by_limit:".length)];
    return {
      tone: "warn",
      text: scope
        ? `Исчерпан ${scope} — бот не отвечает, диалог у менеджера`
        : "Исчерпан лимит расходов — диалог у менеджера",
    };
  }
  const known = REASONS[code];
  if (known) return known;
  // Unknown code: a real technical failure. Keep the code visible — it is
  // what the owner reads out to us when something is wrong.
  if (state === "ANSWERED" || state === "SENT") {
    return { tone: "info", text: `Особый случай: ${code}` };
  }
  return { tone: "danger", text: `Сбой: ${code}` };
}
