import { createApp } from 'vue';
import App from './App.vue';
import AppIcon from './components/AppIcon.vue';
import router from './router';
import { WORKSPACE_PATHS, consumeWorkspaceReloadRoute, markWorkspaceReload, rememberWorkspaceRoute, rememberedWorkspaceRoute } from './authWorkflow.js';
import { GUEST_SESSION_KEY, loadGuestWorkspace } from './guestWorkspace.js';
import 'material/demo/css/light.css';
import 'material/app/bar.js';
import 'material/buttons/button.js';
import 'material/buttons/icon-button.js';
import 'material/buttons/fab.js';
import 'material/card/card.js';
import 'material/checkbox/checkbox.js';
import 'material/dialog/dialog.js';
import 'material/list/list.js';
import 'material/list/list-item.js';
import 'material/nav/rail.js';
import 'material/nav/bar.js';
import 'material/nav/tab.js';
import 'material/indicators/progress.js';
import 'material/select/select.js';
import 'material/select/select-option.js';
import 'material/text/text-field.js';
import 'material/divider/divider.js';
import '../styles.css';
import '../theme.css';
import '../calendar.css';
import '../a11y.css';
import '../dashboard-sketch.css';
import '../../material-dashboard.css';
import '../workspace-routes.css';
import '../capture-proposal.css';
import '../settings.css';
import '../omt-v2.css';
import '../demo-polish-v14.css';
import '../review-scroll-v25.css';

const API = import.meta.env.VITE_API_URL || '/api';

async function bootstrapAuthSession() {
  try {
    if (window.sessionStorage.getItem(GUEST_SESSION_KEY)) {
      const guest = loadGuestWorkspace(window.sessionStorage);
      return { status: 'signed-in', user: guest.user, guest: true };
    }
    const response = await fetch(`${API}/auth/me`, { credentials: 'include' });
    if (response.status === 401) return { status: 'signed-out', user: null };
    if (!response.ok) return { status: 'unknown', user: null };
    const payload = await response.json();
    const user = payload?.user || null;
    return { status: user ? 'signed-in' : 'signed-out', user };
  } catch {
    return { status: 'unknown', user: null };
  }
}

async function boot() {
  const authBootstrap = await bootstrapAuthSession();
  globalThis.__OMT_AUTH_BOOTSTRAP__ = authBootstrap;

  // Resolve the most common signed-in entry route before Vue mounts. This keeps
  // PublicEntry out of the render tree entirely for an existing session instead
  // of showing a few introduction-page frames before /auth/me finishes.
  const workspacePaths = new Set(WORKSPACE_PATHS);
  const nextUrl = new URL(window.location.href);
  if (workspacePaths.has(nextUrl.pathname)) {
    try {
      rememberWorkspaceRoute(window.sessionStorage, nextUrl.pathname);
      // A reload that arrived with its real deep link does not need the fallback marker anymore.
      window.sessionStorage.removeItem('omt:workspace-reload');
    } catch {}
  }
  if (authBootstrap.status === 'signed-in' && nextUrl.pathname === '/') {
    let rememberedRoute = '';
    try {
      const navigation = performance.getEntriesByType?.('navigation')?.[0];
      rememberedRoute = consumeWorkspaceReloadRoute(window.sessionStorage);
      if (!rememberedRoute && navigation?.type === 'reload') rememberedRoute = rememberedWorkspaceRoute(window.sessionStorage);
    } catch {}
    nextUrl.pathname = rememberedRoute || `${import.meta.env.BASE_URL || '/'}today`.replace(/\/{2,}/g, '/');
    ['auth', 'auth_error', 'error', 'error_description'].forEach((key) => nextUrl.searchParams.delete(key));
    window.history.replaceState({}, '', `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
  } else if (authBootstrap.status === 'signed-out' && workspacePaths.has(nextUrl.pathname)) {
    // Resolve protected deep links before Vue mounts. Auth checking never
    // renders PublicEntry on a signed-in session, and signed-out sessions do
    // not briefly mount a protected route before being redirected.
    nextUrl.pathname = import.meta.env.BASE_URL || '/';
    window.history.replaceState({}, '', `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
  }

  createApp(App).component('AppIcon', AppIcon).use(router).mount('#app');
}

const rememberReloadTarget = () => {
  try { markWorkspaceReload(window.sessionStorage, window.location.pathname); } catch {}
};
window.addEventListener('pagehide', rememberReloadTarget);
window.addEventListener('beforeunload', rememberReloadTarget);

void boot();
