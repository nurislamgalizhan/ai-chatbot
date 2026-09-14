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
    text: "Этот вопрос уже был оплачен ранее, ответ потерялся — диалог передан менеджеру",
  },
  // --- inbox: the customer changed their mind ---
  superseded_by_edit: {
    tone: "info",
    text: "Клиент исправил сообщение — бот ответил на новую версию",
  },
  deleted_by_customer: { tone: "info", text: "Клиент удалил это сообщение" },
  unsupported_content: {
    tone: "info",
    text: "Геолокация, контакт или опрос — бот такое не разбирает, передано менеджеру",
  },
  send_failed: {
    tone: "danger",
    text: "Ответ не удалось отправить — диалог передан менеджеру",
  },
  // --- media: outcomes of a voice note or a photo ---
  media_disabled: {
    tone: "info",
    text: "Распознавание таких файлов выключено в «Лимитах» — передано менеджеру",
  },
  media_not_configured: {
    tone: "warn",
    text: "Распознавание файлов не настроено (нет ключа OpenAI) — передано менеджеру",
  },
  audio_too_long: {
    tone: "info",
    text: "Голосовое длиннее лимита — бот попросил написать текстом",
  },
  audio_unclear: {
    tone: "info",
    text: "В голосовом не удалось распознать речь — передано менеджеру",
  },
  empty_transcript: {
    tone: "info",
    text: "В голосовом не удалось распознать речь — передано менеджеру",
  },
  audio_unreadable: {
    tone: "warn",
    text: "Аудиофайл не удалось прочитать — передано менеджеру",
  },
  image_unreadable: {
    tone: "warn",
    text: "Изображение не удалось прочитать — передано менеджеру",
  },
  image_limit_reached: {
    tone: "info",
    text: "Исчерпан лимит фото на диалог — передано менеджеру",
  },
  media_too_large: {
    tone: "info",
    text: "Файл больше разрешённого размера — передано менеджеру",
  },
  media_no_url: {
    tone: "warn",
    text: "Провайдер не передал ссылку на файл — передано менеджеру",
  },
  media_disk_low: {
    tone: "danger",
    text: "На диске мало места — файл не скачан, передано менеджеру",
  },
  no_price: {
    tone: "warn",
    text: "Нет цены модели в «Лимитах» — файл передан менеджеру",
  },
  no_audio_price: {
    tone: "warn",
    text: "Нет цены распознавания речи в «Лимитах» — файл передан менеджеру",
  },
  media_download_failed: {
    tone: "danger",
    text: "Не удалось скачать файл у провайдера — передано менеджеру",
  },
  media_write_failed: {
    tone: "danger",
    text: "Не удалось сохранить файл на диск — передано менеджеру",
  },
  transcription_failed: {
    tone: "danger",
    text: "Сбой распознавания речи — передано менеджеру",
  },
  vision_failed: {
    tone: "danger",
    text: "Сбой распознавания изображения — передано менеджеру",
  },
  media_unexpected_error: {
    tone: "danger",
    text: "Сбой обработки файла — передано менеджеру",
  },
  // --- pipeline configuration ---
  openai_not_configured: {
    tone: "warn",
    text: "Не настроен ключ OpenAI — бот не отвечает, сообщение ждёт",
  },
  no_active_instruction: {
    tone: "warn",
    text: "Нет активной инструкции агента — бот не отвечает",
  },
  circuit_breaker_open: {
    tone: "warn",
    text: "Модель временно недоступна после серии сбоев — сообщение подождёт",
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
  // A reaction (👍 on the bot's answer) is not an outcome worth a plaque.
  if (code === "reaction") return null;
  if (code === "blocked_by_limit" || code.startsWith("blocked_by_limit:")) {
    const scope = LIMIT_SCOPES[code.slice("blocked_by_limit:".length)];
    return {
      tone: "warn",
      text: scope
        ? `Исчерпан ${scope} — бот не отвечает, диалог у менеджера`
        : "Исчерпан лимит расходов — диалог у менеджера",
    };
  }
  if (code.startsWith("sensitive:")) {
    return {
      tone: "info",
      text: "Похоже на документ с личными данными — бот его не анализирует, передано менеджеру",
    };
  }
  if (code.startsWith("pricing:")) {
    return {
      tone: "warn",
      text: "Нет цены модели или курса в «Лимитах» — бот не тратит, сообщение ждёт",
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
