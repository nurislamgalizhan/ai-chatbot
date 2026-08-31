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
    let detail = `Ошибка ${response.status}`;
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
