import assert from 'node:assert/strict';
import test from 'node:test';

import { WORKSPACE_NAV_ITEMS } from './components/workspaceNavigation.js';

test('workspace navigation model has one desktop contract and only three mobile destinations', () => {
  assert.deepEqual(WORKSPACE_NAV_ITEMS, [
    { path: '/today', label: '今天', mobileLabel: '今天', icon: 'home', desktop: true, mobile: true },
    { path: '/calendar', label: '行事曆', mobileLabel: '行事曆', icon: 'calendar_month', desktop: true, mobile: true },
    { path: '/lists', label: '待辦', mobileLabel: '待辦', icon: 'check_box', desktop: true, mobile: true },
  ]);
});

test('workspace navigation icons use one local SVG icon vocabulary', () => {
  assert.deepEqual(WORKSPACE_NAV_ITEMS.map((item) => item.icon), ['home', 'calendar_month', 'check_box']);
  assert.equal(new Set(WORKSPACE_NAV_ITEMS.map((item) => item.icon)).size, 3);
});
