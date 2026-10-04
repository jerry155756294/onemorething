<script setup>
import { computed, ref } from 'vue';

const props = defineProps({
  guestMode: { type: Boolean, default: false },
  currentUser: { type: Object, default: null },
  integrations: { type: Object, default: null },
  integrationsError: { type: String, default: '' },
  integrationsLoading: { type: Boolean, default: false },
  integrationActionMessage: { type: String, default: '' },
  integrationBusy: { type: String, default: '' },
  accountBusy: { type: String, default: '' },
  accountMessage: { type: String, default: '' },
  accountError: { type: String, default: '' },
  accountErrorAction: { type: String, default: '' },
});

const emit = defineEmits(['connect-calendar', 'connect-google-calendar', 'connect-login-provider', 'disconnect-integration', 'retry-integrations', 'sync-integration', 'change-avatar', 'delete-account']);

const avatarInput = ref(null);
const deleteDialogOpen = ref(false);

const displayName = computed(() => props.currentUser?.name || '我的工作區');
const email = computed(() => props.currentUser?.email || '尚未提供電子郵件');
const initials = computed(() => displayName.value.trim().split(/\s+/).map((part) => part[0]).join('').slice(0, 2).toUpperCase() || 'OM');
const calendarConnections = computed(() => props.integrations?.calendar || []);
const authenticationConnections = computed(() => props.integrations?.authentication || []);

function chooseAvatar() {
  if (!props.accountBusy) avatarInput.value?.click();
}

function onAvatarChange(event) {
  const file = event.target.files?.[0] || null;
  event.target.value = '';
  if (file) emit('change-avatar', file);
}

function connectionFor(provider) {
  return calendarConnections.value.find((connection) => connection?.provider === provider) || null;
}

const nextcloudConnection = computed(() => connectionFor('nextcloud_calendar'));
const googleConnection = computed(() => connectionFor('google_calendar'));
const googleNeedsWriteReconnect = computed(() => {
  const scope = String(googleConnection.value?.metadata?.scope || '');
  return Boolean(googleConnection.value && scope.includes('calendar.readonly') && !scope.split(/\s+/).includes('https://www.googleapis.com/auth/calendar'));
});

function isConnected(connection) {
  return ['connected', 'ready', 'syncing', 'error', 'expired'].includes(connection?.status);
}

function calendarConnectionLabel(connection) {
  if (!connection) return '';
  return {
    connected: '已連接',
    ready: '已連接',
    syncing: '同步中',
    error: '連接錯誤',
    expired: '連接已過期',
  }[connection.status] || '';
}

function formatLastSync(value) {
  if (!value) return '還沒有同步紀錄';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '同步時間無法讀取';
  return new Intl.DateTimeFormat('zh-TW', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function isBusy(connectionId, action) {
  return props.integrationBusy === `${connectionId}:${action}`;
}

function canSync(connection) {
  return ['connected', 'ready', 'error'].includes(connection?.status);
}

function authStatus(provider) {
  return authenticationConnections.value.some((connection) => connection?.provider === provider) ? '已連接' : '未連接';
}

function authAvailable(provider) {
  return (props.integrations?.available?.authentication || []).some((item) => item?.provider === provider && item?.enabled);
}
</script>

<template>
  <div class="settings-page">
    <header class="settings-intro">
      <h1>設定</h1>
      <p>整理你的個人資料、登入帳戶和行事曆連接。</p>
    </header>

    <section class="settings-section settings-profile-section" aria-labelledby="settings-profile-title">
      <div class="settings-section-copy">
        <h2 id="settings-profile-title">個人資料</h2>
        <p>這是你在 One More Thing 裡使用的名字和聯絡方式。</p>
      </div>
      <div class="settings-section-content">
        <div class="profile-summary">
          <div class="settings-avatar" :aria-label="`你的頭像：${displayName}`" role="img">
            <img v-if="props.currentUser?.avatar_url" :src="props.currentUser.avatar_url" alt="" />
            <span v-else>{{ initials }}</span>
          </div>
          <div class="profile-details"><strong>{{ displayName }}</strong><span>{{ email }}</span></div>
        </div>
        <div class="settings-action-line">
          <input ref="avatarInput" class="settings-avatar-input" type="file" accept="image/png,image/jpeg,image/webp" @change="onAvatarChange" />
          <md-button class="settings-button settings-button-secondary" color="outlined" size="small" type="button" :disabled="Boolean(props.accountBusy)" @click="chooseAvatar">{{ props.accountBusy === 'avatar' ? '更換中…' : '更換頭像' }}</md-button>
          <span class="settings-help">選擇新圖片後會直接取代目前頭像，不保留舊頭像。</span>
        </div>
        <p v-if="props.accountMessage" class="settings-account-feedback" role="status">{{ props.accountMessage }}</p>
        <p v-if="props.accountError && props.accountErrorAction === 'avatar'" class="settings-account-feedback settings-account-feedback-error" role="alert">{{ props.accountError }}</p>
      </div>
    </section>

    <div class="settings-divider" role="separator"></div>

    <section class="settings-section" aria-labelledby="settings-calendar-title">
      <div class="settings-section-copy">
        <h2 id="settings-calendar-title">行事曆連接</h2>
        <p>把你的行程帶進 One More Thing，隨時掌握接下來的安排。</p>
      </div>
      <div class="settings-section-content provider-list" aria-live="polite" :aria-busy="props.integrationsLoading">
        <div v-if="props.guestMode" class="settings-feedback" role="note">
          <strong>展示帳號使用本地範例</strong><span>行事曆與待辦都能直接操作，但不連接 Google Calendar 或 Nextcloud。</span>
        </div>
        <div v-else-if="props.integrationsLoading" class="integration-skeleton" role="status" aria-label="正在讀取行事曆連接">
          <div v-for="item in 2" :key="item" class="provider-row provider-row-skeleton">
            <div><span class="skeleton-line skeleton-title"></span><span class="skeleton-line skeleton-copy"></span></div>
            <span class="skeleton-line skeleton-action"></span>
          </div>
        </div>

        <div v-else-if="props.integrationsError" class="settings-feedback settings-feedback-error" role="alert">
          <strong>無法讀取整合狀態</strong><span>{{ props.integrationsError }}</span>
          <md-button class="settings-button settings-button-secondary" color="outlined" size="small" type="button" @click="emit('retry-integrations')">重新載入</md-button>
        </div>

        <template v-else>
          <div v-if="props.integrationActionMessage" class="settings-feedback" role="status">{{ props.integrationActionMessage }}</div>

          <article class="provider-row">
            <div class="provider-copy">
              <div class="provider-heading"><h3>Google Calendar</h3><span v-if="calendarConnectionLabel(googleConnection)" class="calendar-connection-state">{{ calendarConnectionLabel(googleConnection) }}</span></div>
              <p v-if="googleConnection && googleConnection.status === 'expired'">請重新連接 Google Calendar。</p>
              <p v-else-if="googleNeedsWriteReconnect">需要重新授權一次，才能啟用新增、編輯與刪除的雙向同步。</p>
              <p v-else-if="googleConnection && googleConnection.status === 'error'">Google Calendar 同步發生錯誤，可重新同步或重新連接。</p>
              <p v-else-if="googleConnection">最後同步：{{ formatLastSync(googleConnection.last_sync_at) }}</p>
            </div>
            <div class="provider-actions">
              <template v-if="googleConnection">
                <md-button v-if="canSync(googleConnection)" class="settings-button settings-button-primary" color="filled" size="small" type="button" :disabled="Boolean(props.integrationBusy)" @click="emit('sync-integration', googleConnection.id, googleConnection.provider)"><AppIcon slot="icon" class="settings-button-icon" name="sync" />{{ isBusy(googleConnection.id, 'sync') ? '同步中…' : '立即同步' }}</md-button>
                <md-button class="settings-button settings-button-secondary settings-button-disconnect" color="outlined" size="small" type="button" :disabled="isBusy(googleConnection.id, 'disconnect')" @click="emit('disconnect-integration', googleConnection.id)">{{ isBusy(googleConnection.id, 'disconnect') ? '移除中…' : '移除連結' }}</md-button>
                <md-button v-if="googleNeedsWriteReconnect || ['error', 'expired'].includes(googleConnection.status)" class="settings-button settings-button-quiet" color="text" size="small" type="button" @click="emit('connect-google-calendar')">{{ googleNeedsWriteReconnect ? '重新授權雙向同步' : '重新連接 Google Calendar' }}</md-button>
              </template>
              <md-button v-else class="settings-button settings-button-primary" color="filled" size="small" type="button" :disabled="props.integrationBusy === 'google_calendar:connect'" @click="emit('connect-google-calendar')">{{ props.integrationBusy === 'google_calendar:connect' ? '準備中…' : '連接 Google Calendar' }}</md-button>
            </div>
          </article>

          <article class="provider-row">
            <div class="provider-copy">
              <div class="provider-heading"><h3>Nextcloud Calendar</h3><span v-if="calendarConnectionLabel(nextcloudConnection)" class="calendar-connection-state">{{ calendarConnectionLabel(nextcloudConnection) }}</span></div>
              <p v-if="nextcloudConnection">最後同步：{{ formatLastSync(nextcloudConnection.last_sync_at) }}</p>
            </div>
            <div class="provider-actions">
              <template v-if="nextcloudConnection">
                <md-button v-if="canSync(nextcloudConnection)" class="settings-button settings-button-primary" color="filled" size="small" type="button" :disabled="Boolean(props.integrationBusy)" @click="emit('sync-integration', nextcloudConnection.id, nextcloudConnection.provider)"><AppIcon slot="icon" class="settings-button-icon" name="sync" />{{ isBusy(nextcloudConnection.id, 'sync') ? '同步中…' : '立即同步' }}</md-button>
                <md-button class="settings-button settings-button-secondary settings-button-disconnect" color="outlined" size="small" type="button" :disabled="isBusy(nextcloudConnection.id, 'disconnect')" @click="emit('disconnect-integration', nextcloudConnection.id)">{{ isBusy(nextcloudConnection.id, 'disconnect') ? '移除中…' : '移除連結' }}</md-button>
                <md-button v-if="['error', 'expired'].includes(nextcloudConnection.status)" class="settings-button settings-button-quiet" color="text" size="small" type="button" @click="emit('connect-calendar')">重新連接 Nextcloud Calendar</md-button>
              </template>
              <md-button v-else class="settings-button settings-button-primary" color="filled" size="small" type="button" @click="emit('connect-calendar')">連接 Nextcloud Calendar</md-button>
            </div>
          </article>
        </template>
      </div>
    </section>

    <div class="settings-divider" role="separator"></div>

    <section class="settings-section" aria-labelledby="settings-login-title">
      <div class="settings-section-copy"><h2 id="settings-login-title">登入帳戶</h2><p>選擇你要用來登入 One More Thing 的帳戶。</p></div>
      <div class="settings-section-content provider-list">
        <div v-if="props.guestMode" class="settings-feedback" role="note"><strong>展示帳號不綁定登入帳戶</strong><span>要使用 AI 或外部同步時，再登入正式工作區即可。</span></div>
        <div v-else v-for="provider in ['google', 'github']" :key="provider" class="provider-row provider-row-compact">
          <div class="provider-copy"><div class="provider-heading"><h3>{{ provider === 'google' ? 'Google 登入' : 'GitHub 登入' }}</h3><span class="provider-status" :class="authStatus(provider) === '已連接' ? 'provider-status-connected' : 'provider-status-muted'">{{ authStatus(provider) }}</span></div><p v-if="authStatus(provider) === '已連接'">可用來登入你的工作區。</p><p v-else-if="authAvailable(provider)">連接後就能用這個帳戶登入。</p><p v-else>這個登入方式目前尚未開放。</p></div>
          <md-button v-if="authStatus(provider) !== '已連接' && authAvailable(provider)" class="settings-button settings-button-secondary" color="outlined" size="small" type="button" @click="emit('connect-login-provider', provider)">連接{{ provider === 'google' ? ' Google 登入' : ' GitHub 登入' }}</md-button>
        </div>
      </div>
    </section>

    <div class="settings-divider" role="separator"></div>

    <section class="settings-section settings-account-section" aria-labelledby="settings-account-title">
      <div class="settings-section-copy"><h2 id="settings-account-title">帳戶</h2><p>需要特別確認的帳戶操作會放在這裡。</p></div>
      <div class="settings-section-content danger-content">
        <div><h3>刪除帳戶</h3><p>永久刪除 One More Thing 帳戶、待辦、行事曆連結與已整理資料。此操作無法復原。</p></div>
        <md-button class="settings-button settings-button-danger" color="outlined" size="small" type="button" :disabled="Boolean(props.accountBusy)" @click="deleteDialogOpen = true">刪除帳戶</md-button>
      </div>
    </section>

    <md-dialog class="omt-destructive-dialog account-delete-confirmation" :open="deleteDialogOpen" quick aria-labelledby="account-delete-title" aria-describedby="account-delete-description" @cancel="deleteDialogOpen = false" @closed="deleteDialogOpen = false">
      <div slot="headline"><h2 id="account-delete-title">永久刪除帳戶？</h2></div>
      <div slot="content">
        <p id="account-delete-description">這會刪除你的 One More Thing 帳戶與所有工作區資料，且無法復原。</p>
        <p v-if="props.accountError && props.accountErrorAction === 'delete'" class="settings-account-feedback settings-account-feedback-error" role="alert">{{ props.accountError }}</p>
      </div>
      <div slot="actions">
        <md-button color="text" size="medium" type="button" :disabled="props.accountBusy === 'delete'" @click="deleteDialogOpen = false">取消</md-button>
        <md-button color="filled" size="medium" class="account-delete-confirm-action" type="button" :disabled="props.accountBusy === 'delete'" @click="emit('delete-account')">{{ props.accountBusy === 'delete' ? '刪除中…' : '永久刪除' }}</md-button>
      </div>
    </md-dialog>
  </div>
</template>
