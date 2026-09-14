/** Fetch wrapper: same-origin cookies, CSRF header, uniform error shape.
 *  Tokens are NEVER stored in localStorage — the session lives in an
 *  HttpOnly cookie managed by the server. */

export class ApiError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(detail);
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)wabot_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** FastAPI validation errors arrive as [{loc, msg}] — name the field. */
function validationText(items: unknown[]): string | null {
  const parts = items
    .map((item) => {
      if (typeof item !== "object" || item === null) return null;
      const { loc, msg } = item as { loc?: unknown[]; msg?: unknown };
      const field = Array.isArray(loc) && loc.length ? String(loc[loc.length - 1]) : "";
      return field && typeof msg === "string" ? `${field}: ${msg}` : typeof msg === "string" ? msg : null;
    })
    .filter((x): x is string => Boolean(x));
  return parts.length ? `Проверьте поля — ${parts.join("; ")}` : null;
}

export async function api<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (MUTATING.has(method)) headers["X-CSRF-Token"] = csrfToken();

  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      // The session expired or was revoked while a page was open. Every
      // poll would keep failing and the page would show empty states
      // («Чатов нет») instead of the login form. A full navigation also
      // drops everything cached from the old session.
      window.location.assign("/login");
    }
    let detail = `Ошибка ${response.status}`;
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail)) detail = validationText(data.detail) ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
