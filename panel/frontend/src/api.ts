export type ApiMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

export type ApiClient = <T = any>(method: ApiMethod, path: string, body?: unknown) => Promise<T>;

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function createApiClient(getToken: () => string): ApiClient {
  return async function api<T = any>(method: ApiMethod, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const token = getToken().trim();
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await fetch(`/api${path}`, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const text = await response.text();
      let message = text || `${response.status} ${response.statusText}`;
      try {
        const parsed = JSON.parse(text);
        message = parsed.detail || parsed.message || message;
      } catch {
        // Keep the original response text.
      }
      throw new ApiError(response.status, message);
    }
    return response.json() as Promise<T>;
  };
}
