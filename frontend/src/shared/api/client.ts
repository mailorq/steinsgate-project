const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

async function ensureCsrfCookie(): Promise<void> {
  if (!getCookie("csrftoken")) {
    await fetch("/api/auth/csrf", { credentials: "same-origin" });
  }
}

function extractDetail(data: unknown, status: number): string {
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (item && typeof item === "object" && "msg" in item ? item.msg : ""))
        .filter(Boolean)
        .join("; ");
    }
  }
  return `Ошибка запроса (${status})`;
}

interface RequestOptions {
  method?: string;
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;

  if (MUTATING_METHODS.has(method)) {
    await ensureCsrfCookie();
    headers["X-CSRFToken"] = getCookie("csrftoken") ?? "";
  }

  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form;
  }

  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body,
    credentials: "same-origin",
    signal: options.signal,
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const data = await response.json().catch(() => null);

  if (!response.ok) {
    const retryAfter = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
    throw new ApiError(
      response.status,
      extractDetail(data, response.status),
      Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
    );
  }

  return data as T;
}
