import { createRouter, createWebHistory } from 'vue-router';
import CalendarView from './views/CalendarView.vue';
import ListsView from './views/ListsView.vue';
import PublicEntry from './views/PublicEntry.vue';
import SettingsView from './views/SettingsView.vue';
import TodayView from './views/TodayView.vue';

export const workspaceRoutes = [
  { path: '/settings', name: 'settings', label: '設定', component: SettingsView, requiresAuth: true },
  { path: '/today', name: 'today', label: '今天', component: TodayView, requiresAuth: true },
  { path: '/calendar', name: 'calendar', label: '行事曆', component: CalendarView, requiresAuth: true },
  { path: '/lists', name: 'lists', label: '待辦', component: ListsView, requiresAuth: true },
];

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'entry', component: PublicEntry, meta: { label: '登入' } },
    ...workspaceRoutes.map(({ requiresAuth, ...route }) => ({ ...route, meta: { label: route.label, requiresAuth } })),
    { path: '/inbox', redirect: '/today' },
    { path: '/overview', redirect: '/today' },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) return savedPosition;
    if (to.path !== from.path) return { top: 0 };
    return undefined;
  },
});

export default router;
