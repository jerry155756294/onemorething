import assert from 'node:assert/strict';
import test from 'node:test';
import { WORKSPACE_RELOAD_KEY, consumeWorkspaceReloadRoute, logoutAfterServerConfirmation, markWorkspaceReload, rememberWorkspaceRoute, rememberedWorkspaceRoute } from './authWorkflow.js';

test('logout API failure preserves local authentication state', async () => {
  let cleared = false;

  await assert.rejects(
    logoutAfterServerConfirmation(async (path, options) => {
      assert.equal(path, '/auth/logout');
      assert.deepEqual(options, { method: 'POST' });
      throw new Error('logout unavailable');
    }, () => { cleared = true; }),
    /logout unavailable/,
  );

  assert.equal(cleared, false);
});

test('successful logout API response clears local authentication state', async () => {
  let cleared = false;

  await logoutAfterServerConfirmation(async (path) => {
    assert.equal(path, '/auth/logout');
    return { ok: true };
  }, () => { cleared = true; });

  assert.equal(cleared, true);
});


test('workspace reload marker restores the exact protected route even if hosting collapses the URL to root', () => {
  const memory = new Map();
  const storage = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, String(value)),
    removeItem: (key) => memory.delete(key),
  };
  assert.equal(rememberWorkspaceRoute(storage, '/calendar'), true);
  assert.equal(rememberedWorkspaceRoute(storage), '/calendar');
  assert.equal(markWorkspaceReload(storage, '/calendar', 1000), true);
  assert.equal(consumeWorkspaceReloadRoute(storage, 1500), '/calendar');
  assert.equal(storage.getItem(WORKSPACE_RELOAD_KEY), null);
});

test('workspace reload marker rejects stale and public paths', () => {
  const memory = new Map();
  const storage = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, String(value)),
    removeItem: (key) => memory.delete(key),
  };
  assert.equal(markWorkspaceReload(storage, '/', 1000), false);
  markWorkspaceReload(storage, '/lists', 1000);
  assert.equal(consumeWorkspaceReloadRoute(storage, 50000, 30000), '');
});
