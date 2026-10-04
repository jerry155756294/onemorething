<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { WORKSPACE_NAV_ITEMS } from './workspaceNavigation.js';

const props = defineProps({
  dashboardError: { type: String, default: '' },
  mobileNavOpen: { type: Boolean, default: false },
  profileName: { type: String, default: 'My workspace' },
  profileAvatarUrl: { type: String, default: '' },
});

const emit = defineEmits(['close-mobile-nav', 'navigate', 'reload', 'sign-out', 'toggle-mobile-nav']);
const route = useRoute();
const accountMenuOpen = ref(false);
const avatarLoadFailed = ref(false);
watch(() => props.profileAvatarUrl, () => { avatarLoadFailed.value = false; });
const desktopNavItems = WORKSPACE_NAV_ITEMS.filter((item) => item.desktop !== false);
const mobileNavItems = WORKSPACE_NAV_ITEMS.filter((item) => item.mobile !== false);
const isWorkspaceShellRoute = computed(() => ['/today', '/calendar', '/lists', '/settings'].includes(route.path));
// Keep the OMT product layer mounted for the entire authenticated workspace.
// During an out-in route transition, Vue updates route.path before the leaving
// view unmounts; dropping this class on Settings used to flash legacy CSS over
// Today/Calendar/Tasks and re-introduce native details markers and stretched controls.
const isOmtV2Route = isWorkspaceShellRoute;
const viewLabel = computed(() => route.meta.label || '總覽');

function navigate(path) {
  closeAccountMenu();
  emit('navigate', path);
}

function toggleAccountMenu() {
  accountMenuOpen.value = !accountMenuOpen.value;
}

function closeAccountMenu() {
  accountMenuOpen.value = false;
}

function signOut() {
  closeAccountMenu();
  emit('sign-out');
}

function handleDocumentClick(event) {
  if (!event.target.closest('.account-menu-wrap')) closeAccountMenu();
}

function handleKeydown(event) {
  if (event.key === 'Escape') closeAccountMenu();
}

onMounted(() => {
  document.addEventListener('click', handleDocumentClick);
  document.addEventListener('keydown', handleKeydown);
});
onBeforeUnmount(() => {
  document.removeEventListener('click', handleDocumentClick);
  document.removeEventListener('keydown', handleKeydown);
});
</script>

<template>
  <div class="app-shell" :class="{ 'app-shell--omt-v2': isOmtV2Route, 'app-shell--workspace': isWorkspaceShellRoute }">
    <aside class="sidebar md3-sidebar" :class="{ open: props.mobileNavOpen }">
      <div class="brand"><span v-if="!isWorkspaceShellRoute" class="brand-mark">✳</span><svg v-else class="brand-mark-svg" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5v5.25M12 16.25v5.25M2.5 12h5.25M16.25 12h5.25M5.3 5.3l3.72 3.72m5.96 5.96 3.72 3.72m0-13.4-3.72 3.72m-5.96 5.96L5.3 18.7" /></svg><span>one more<br /><em>thing</em></span></div>
      <nav id="mobile-navigation" class="material-nav-rail" aria-label="主要導覽">
        <button
          v-for="item in desktopNavItems"
          :key="item.path"
          class="workspace-rail-item"
          :class="{ 'workspace-rail-item--active': route.path === item.path }"
          type="button"
          :aria-current="route.path === item.path ? 'page' : undefined"
          @click="navigate(item.path)"
        >
          <span class="workspace-rail-item__indicator" aria-hidden="true"><AppIcon class="workspace-nav-icon" :name="item.icon" /></span>
          <span class="workspace-rail-item__label">{{ item.label }}</span>
        </button>
      </nav>
    </aside>
    <button v-if="props.mobileNavOpen" class="mobile-nav-scrim" type="button" aria-label="關閉導覽" @click="emit('close-mobile-nav')"></button>
    <div v-if="isWorkspaceShellRoute" class="workspace-account-anchor">
      <div class="account-menu-wrap">
        <md-icon-button id="account-menu-button" aria-label="開啟帳戶選單" :aria-expanded="accountMenuOpen" aria-haspopup="menu" @click.stop="toggleAccountMenu">
          <img v-if="props.profileAvatarUrl && !avatarLoadFailed" class="account-avatar-photo" :src="props.profileAvatarUrl" alt="" @error="avatarLoadFailed = true" />
          <AppIcon v-else name="account_circle" />
        </md-icon-button>
        <Transition name="account-popover">
          <div v-if="accountMenuOpen" class="account-menu-panel" role="menu" aria-label="帳戶選單">
            <div class="account-menu-heading" role="presentation"><strong>{{ props.profileName }}</strong><span>個人工作區</span></div>
            <div class="account-menu-divider" role="separator"></div>
            <md-button class="account-menu-action" color="text" size="small" role="menuitem" @click="navigate('/settings')">設定</md-button>
            <md-button class="account-menu-action account-menu-danger" color="text" size="small" role="menuitem" @click="signOut">登出</md-button>
          </div>
        </Transition>
      </div>
    </div>
    <main class="main-content workspace-main">
      <header class="topbar md3-topbar">
        <span class="topbar-title">{{ viewLabel }}</span>
        <md-app-bar v-if="!isWorkspaceShellRoute" type="small">
          <md-icon-button slot="leading-icon" aria-label="開啟導覽" :aria-expanded="props.mobileNavOpen" aria-controls="mobile-navigation" @click="emit('toggle-mobile-nav')"><AppIcon name="menu" /></md-icon-button>
          <div slot="action-items" class="md3-app-actions"><md-icon-button disabled aria-label="搜尋功能即將加入"><AppIcon name="search" /></md-icon-button></div>
        </md-app-bar>
        <div class="breadcrumb"><strong>{{ viewLabel }}</strong></div>
      </header>
      <div v-if="props.dashboardError" class="status-banner error" role="alert"><span>{{ props.dashboardError }}</span><md-button color="text" size="small" @click="emit('reload')">重新載入</md-button></div>
      <slot />
    </main>
    <nav class="mobile-nav" role="tablist" :aria-label="`行動版主要導覽；目前頁面：${viewLabel}`">
      <md-navigation-tab
        v-for="item in mobileNavItems"
        :key="item.path"
        class="mobile-nav__item"
        :label="item.mobileLabel"
        :active="route.path === item.path"
        :aria-current="route.path === item.path ? 'page' : undefined"
        @click="navigate(item.path)"
      >
        <AppIcon slot="inactive-icon" class="workspace-nav-icon" :name="item.icon" />
        <AppIcon slot="active-icon" class="workspace-nav-icon" :name="item.icon" />
      </md-navigation-tab>
    </nav>
  </div>
</template>

<style scoped>
.mobile-nav {
  align-items: stretch;
  background: #faf7fb;
  border-top: 1px solid #e7e0eb;
  bottom: 0;
  box-sizing: border-box;
  color: var(--workspace-rail-icon, #4d4650);
  display: flex;
  left: 0;
  min-height: calc(80px + env(safe-area-inset-bottom, 0px));
  padding-bottom: env(safe-area-inset-bottom, 0px);
  position: fixed;
  right: 0;
  z-index: 30;
  width: 100%;
  box-shadow: none;
}

.mobile-nav__item {
  -webkit-tap-highlight-color: transparent;
  box-sizing: border-box;
  color: var(--workspace-rail-icon, #4d4650);
  flex: 1 1 0;
  height: 80px;
  min-height: 80px;
  min-width: 0;
  width: auto;
  --md-navigation-bar-active-indicator-width: 64px;
  --md-navigation-bar-active-indicator-height: 32px;
  --md-navigation-tab-active-indicator-width: 64px;
  --md-navigation-tab-active-indicator-height: 32px;
  --md-navigation-bar-active-indicator-color: var(--workspace-rail-active, #dedbf5);
  --md-navigation-tab-active-indicator-color: var(--workspace-rail-active, #dedbf5);
  --md-navigation-tab-label-text-color: var(--workspace-rail-icon, #4d4650);
  --md-navigation-tab-active-label-text-color: var(--workspace-rail-icon-active, #271533);
  --md-navigation-bar-hover-state-layer-opacity: 0;
  --md-navigation-bar-pressed-state-layer-opacity: 0;
  --md-navigation-bar-focus-state-layer-opacity: 0;
  --md-elevation-level: 0;
  --md-elevation-shadow-color: transparent;
}

.mobile-nav__item[active] {
  color: var(--workspace-rail-icon-active, #271533);
}

.mobile-nav__item .workspace-nav-icon {
  display: block;
  height: 24px;
  width: 24px;
}

@media (min-width: 901px) {
  .mobile-nav {
    display: none;
  }
}

.sr-only {
  clip: rect(0, 0, 0, 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}
</style>
