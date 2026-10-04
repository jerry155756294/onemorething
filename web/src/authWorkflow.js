export const WORKSPACE_PATHS = Object.freeze(['/today', '/calendar', '/lists', '/settings']);

export const WORKSPACE_ROUTE_KEY = 'omt:last-workspace-route';
export const WORKSPACE_RELOAD_KEY = 'omt:workspace-reload';

export function rememberWorkspaceRoute(storage, path) {
  if (!storage || !WORKSPACE_PATHS.includes(path)) return false;
  storage.setItem(WORKSPACE_ROUTE_KEY, path);
  return true;
}

export function markWorkspaceReload(storage, path, now = Date.now()) {
  if (!storage || !WORKSPACE_PATHS.includes(path)) return false;
  storage.setItem(WORKSPACE_RELOAD_KEY, JSON.stringify({ path, at: Number(now) || Date.now() }));
  rememberWorkspaceRoute(storage, path);
  return true;
}

export function consumeWorkspaceReloadRoute(storage, now = Date.now(), maxAgeMs = 30000) {
  if (!storage) return '';
  const raw = storage.getItem(WORKSPACE_RELOAD_KEY);
  if (!raw) return '';
  storage.removeItem(WORKSPACE_RELOAD_KEY);
  try {
    const parsed = JSON.parse(raw);
    const path = String(parsed?.path || '');
    const at = Number(parsed?.at || 0);
    if (!WORKSPACE_PATHS.includes(path)) return '';
    if (!Number.isFinite(at) || Math.abs(Number(now) - at) > maxAgeMs) return '';
    return path;
  } catch {
    return '';
  }
}

export function rememberedWorkspaceRoute(storage) {
  if (!storage) return '';
  const path = storage.getItem(WORKSPACE_ROUTE_KEY) || '';
  return WORKSPACE_PATHS.includes(path) ? path : '';
}

export async function logoutAfterServerConfirmation(requestJson, clearAuthState) {
  await requestJson('/auth/logout', { method: 'POST' });
  clearAuthState();
}
