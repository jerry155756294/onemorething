import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const appSourcePromise = readFile(new URL('./App.vue', import.meta.url), 'utf8');
const routerSourcePromise = readFile(new URL('./router.js', import.meta.url), 'utf8');
const workspaceShellSourcePromise = readFile(new URL('./components/WorkspaceShell.vue', import.meta.url), 'utf8');
const workspaceNavigationSourcePromise = readFile(new URL('./components/workspaceNavigation.js', import.meta.url), 'utf8');
const calendarSourcePromise = readFile(new URL('./views/CalendarView.vue', import.meta.url), 'utf8');
const captureComposerSourcePromise = readFile(new URL('./components/CaptureComposer.vue', import.meta.url), 'utf8');
const listViewSourcePromise = readFile(new URL('./views/ListsView.vue', import.meta.url), 'utf8');
const inboxViewSourcePromise = readFile(new URL('./views/InboxView.vue', import.meta.url), 'utf8');
const proposalReviewSourcePromise = readFile(new URL('./components/ProposalReviewDialog.vue', import.meta.url), 'utf8');
const proposalItemSourcePromise = readFile(new URL('./components/ProposalItem.vue', import.meta.url), 'utf8');
const taskDetailSourcePromise = readFile(new URL('./components/TaskDetail.vue', import.meta.url), 'utf8');
const eventEditorSourcePromise = readFile(new URL('./components/EventEditorDialog.vue', import.meta.url), 'utf8');
const sourceDetailSourcePromise = readFile(new URL('./components/SourceDetailPanel.vue', import.meta.url), 'utf8');
const captureStylesPromise = readFile(new URL('../capture-proposal.css', import.meta.url), 'utf8');
const calendarDataSourcePromise = readFile(new URL('./calendarData.js', import.meta.url), 'utf8');
const workspaceStylesPromise = readFile(new URL('../workspace-routes.css', import.meta.url), 'utf8');
const omtStylesPromise = readFile(new URL('../omt-v2.css', import.meta.url), 'utf8');
const todaySourcePromise = readFile(new URL('./views/TodayView.vue', import.meta.url), 'utf8');
const settingsSourcePromise = readFile(new URL('./views/SettingsView.vue', import.meta.url), 'utf8');
const settingsStylesPromise = readFile(new URL('../settings.css', import.meta.url), 'utf8');
const baseStylesPromise = readFile(new URL('../styles.css', import.meta.url), 'utf8');
const mainSourcePromise = readFile(new URL('./main.js', import.meta.url), 'utf8');
const appIconSourcePromise = readFile(new URL('./components/AppIcon.vue', import.meta.url), 'utf8');
const publicEntrySourcePromise = readFile(new URL('./views/PublicEntry.vue', import.meta.url), 'utf8');
const themeStylesPromise = readFile(new URL('../theme.css', import.meta.url), 'utf8');
const demoPolishStylesPromise = readFile(new URL('../demo-polish-v14.css', import.meta.url), 'utf8');
const reviewScrollV25StylesPromise = readFile(new URL('../review-scroll-v25.css', import.meta.url), 'utf8');
const indexSourcePromise = readFile(new URL('../index.html', import.meta.url), 'utf8');

test('Lists receives only route-specific request loading and task-query errors', async () => {
  const source = await appSourcePromise;
  assert.match(source, /listsLoading:\s*listsLoading\.value,/);
  assert.match(source, /listsLoadError:\s*listsLoadError\.value,/);
  assert.match(source, /taskListLoading:\s*taskListLoading\.value,/);
  assert.match(source, /taskListError:\s*taskListError\.value,/);
  assert.match(source, /initialDataUnresolved:\s*listDataUnresolved\.value,/);
  assert.doesNotMatch(source, /listsLoading:\s*listsLoading\.value\s*\|\|\s*dashboardBootstrapLoading\.value/);
  assert.doesNotMatch(source, /taskListError:\s*taskListError\.value\s*\|\|/);
  assert.match(source, /listsLoadError\.value = requestError\.value/);
  assert.match(source, /listError\.value = requestError\.value \|\| '清單尚未建立/);
  assert.match(source, /@retry-lists="retryListData"/);
  assert.match(source, /@retry-task-list="loadListTasks\(selectedListId\)"/);
});

test('Workspace shell does not show the background dashboard loading banner', async () => {
  const [shell, app] = await Promise.all([workspaceShellSourcePromise, appSourcePromise]);
  assert.doesNotMatch(shell, /正在載入你的工作區/);
  assert.doesNotMatch(shell, /dashboardLoading/);
  assert.doesNotMatch(app, /<WorkspaceShell[^>]*dashboard-loading/);
  assert.doesNotMatch(app, /auth-check-shell|正在確認你的工作區|正在檢查登入狀態/);
  assert.match(app, /v-if="authSessionState === 'checking'" class="auth-boot-screen"/);
  assert.ok(app.indexOf("authSessionState === 'checking'") < app.indexOf('<PublicEntry'), 'auth boot gate must render before PublicEntry');
  assert.match(shell, /v-if="props\.dashboardError" class="status-banner error"/);
});

test('Calendar does not repeat the route name in its loading message', async () => {
  const source = await calendarSourcePromise;
  assert.doesNotMatch(source, /正在載入行事曆/);
});

test('Lists keeps personal-list setup secondary and only adds Tasks through Capture', async () => {
  const [view, app, styles] = await Promise.all([listViewSourcePromise, appSourcePromise, readFile(new URL('../omt-v2.css', import.meta.url), 'utf8')]);
  assert.match(view, /<details class="list-management">/);
  assert.match(view, /<summary><AppIcon class="list-management__icon" name="folder" \/><span>清單<\/span><strong>\{\{ selectedList\?\.name \|\| '全部待辦' \}\}<\/strong><\/summary>/);
  assert.match(view, /<label for="new-list-name">新增個人清單<\/label>/);
  assert.doesNotMatch(view, /<details class="list-management" open/);
  assert.doesNotMatch(view, /create-task|new-task-title|直接新增待辦|手動待辦/);
  assert.ok(view.indexOf('class="list-management"') < view.indexOf('class="todo-filter"'));
  assert.match(styles, /\.app-shell--omt-v2 \.list-management\s*\{[^}]*margin:/);
  assert.match(styles, /\.app-shell--omt-v2 \.list-management > summary\s*\{[^}]*list-style:\s*none/);
  assert.match(styles, /\.app-shell--omt-v2 \.list-management__content\s*\{[^}]*border-block:/);
  assert.match(styles, /\.app-shell--omt-v2 \.todo-filter \.filter\.active/);
  assert.match(view, /<md-button[\s\S]*?class="filter"[\s\S]*?color="text"[\s\S]*?:aria-pressed="activeFilter === filter\.key"/);
  assert.match(view, /<Transition name="todo-filter" mode="out-in">[\s\S]*?:key="activeFilter"/);
  assert.doesNotMatch(app, /createListTask|@create-task=/);
  assert.match(view, /<md-select[\s\S]*?id="personal-list-picker"[\s\S]*?label="查看清單"/);
  assert.match(view, /<md-select-option value=""><span slot="headline">全部待辦<\/span><\/md-select-option>/);
  assert.match(view, /<md-select-option v-for="list in lists"[\s\S]*?<span slot="headline">\{\{ list\.name \}\}<\/span>/);
  assert.doesNotMatch(view, /<select\b|<option\b/);
  assert.match(view, /<TaskRow v-for="task in openTasks"/);
  assert.match(view, /<TaskRow v-for="task in visibleTasks"/);
  assert.match(view, /<details v-if="activeFilter !== 'all' && completedTasks\.length" class="omt-completed-group"/);
  assert.match(view, /class="list-empty-state omt-empty-state"/);
});

test('primary navigation reduces destinations and legacy Inbox links resume on Today', async () => {
  const [app, router, shell, nav] = await Promise.all([appSourcePromise, routerSourcePromise, workspaceShellSourcePromise, workspaceNavigationSourcePromise]);
  assert.match(router, /\{ path: '\/inbox', redirect: '\/today' \}/);
  assert.doesNotMatch(router, /name: 'inbox'/);
  assert.match(nav, /path: '\/today'/);
  assert.match(nav, /path: '\/calendar'/);
  assert.match(nav, /path: '\/lists'/);
  assert.doesNotMatch(nav, /path: '\/settings'/);
  assert.match(shell, /class="account-menu-action"[\s\S]*?@click="navigate\('\/settings'\)"/);
  assert.doesNotMatch(nav, /\/inbox|收件匣/);
  const mobileNavStart = shell.indexOf('<nav class="mobile-nav"');
  const mobileNavEnd = shell.indexOf('</nav>', mobileNavStart);
  const mobilePrimaryNav = shell.slice(mobileNavStart, mobileNavEnd);
  assert.ok(mobileNavStart > shell.indexOf('</main>'), 'mobile nav must be outside the main scroll container');
  assert.match(mobilePrimaryNav, /v-for="item in mobileNavItems"/);
  assert.match(mobilePrimaryNav, /class="mobile-nav__item"/);
  assert.match(mobilePrimaryNav, /<md-navigation-tab[\s\S]*?class="mobile-nav__item"[\s\S]*?:active="route\.path === item\.path"/);
  assert.match(mobilePrimaryNav, /<AppIcon slot="inactive-icon" class="workspace-nav-icon" :name="item\.icon" \/>/);
  assert.match(mobilePrimaryNav, /<AppIcon slot="active-icon" class="workspace-nav-icon" :name="item\.icon" \/>/);
  assert.doesNotMatch(mobilePrimaryNav, /設定/);
  assert.match(shell, /-webkit-tap-highlight-color:\s*transparent/);
  assert.match(shell, /@media \(min-width: 901px\)[\s\S]*?\.mobile-nav \{[\s\S]*?display: none/);
  assert.doesNotMatch(app, /pendingProposals|pendingProposalCount/);
});

test('workspace rail imports one navigation model and renders one vertical rail contract with local icons', async () => {
  const [shell, styles] = await Promise.all([workspaceShellSourcePromise, omtStylesPromise]);
  assert.match(shell, /import \{ WORKSPACE_NAV_ITEMS \} from ['"]\.\/workspaceNavigation\.js['"]/);
  assert.match(shell, /const desktopNavItems = WORKSPACE_NAV_ITEMS\.filter\(\(item\) => item\.desktop !== false\)/);
  assert.match(shell, /const mobileNavItems = WORKSPACE_NAV_ITEMS\.filter\(\(item\) => item\.mobile !== false\)/);
  assert.match(shell, /<button[\s\S]*?v-for="item in desktopNavItems"[\s\S]*?class="workspace-rail-item"/);
  assert.match(shell, /'workspace-rail-item--active': route\.path === item\.path/);
  assert.doesNotMatch(shell, /workspace-rail-item--utility|item\.placement/);
  assert.match(shell, /class="workspace-rail-item__indicator"[\s\S]*?<AppIcon class="workspace-nav-icon" :name="item\.icon" \/>/);
  assert.match(shell, /class="workspace-rail-item__label">\{\{ item\.label \}\}<\/span>/);
  assert.doesNotMatch(shell.slice(shell.indexOf('<nav id="mobile-navigation"'), shell.indexOf('</nav>', shell.indexOf('<nav id="mobile-navigation"'))), /md-navigation-tab/);
  assert.doesNotMatch(shell, /const navItems\s*=/);
  assert.doesNotMatch(styles, /\.workspace-rail-item--utility/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item__indicator\s*\{[^}]*width:\s*56px;[^}]*height:\s*32px/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item:not\(\.workspace-rail-item--active\) \.workspace-rail-item__indicator\s*\{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item--active \.workspace-rail-item__indicator\s*\{[^}]*background:\s*var\(--workspace-rail-active\);[^}]*box-shadow:\s*none/);
  assert.match(styles, /@media\s*\(hover:\s*hover\)\s*and\s*\(pointer:\s*fine\)[\s\S]*?workspace-rail-item:not\(\.workspace-rail-item--active\):hover \.workspace-rail-item__indicator\s*\{[^}]*background:\s*var\(--workspace-rail-hover\)/);
});

test('Today OMT v2 owns its composition without legacy Today route selectors', async () => {
  const [today, composer, legacyCss] = await Promise.all([todaySourcePromise, captureComposerSourcePromise, workspaceStylesPromise]);
  assert.match(today, /<h1><time>\{\{ todayDateLabel \}\}<\/time><\/h1>/);
  assert.match(today, /<p class="omt-date-line">\{\{ todayWeekdayLabel \}\}<\/p>/);
  assert.match(today, /<CaptureComposer[\s\S]*?inline-submit/);
  assert.equal((today.match(/class="today-section /g) || []).length, 2);
  assert.match(today, /class="today-content-grid omt-home-columns"/);
  assert.match(composer, /正在整理資訊…/);
  assert.doesNotMatch(legacyCss, /\.today-layout\b/);
  assert.doesNotMatch(legacyCss, /\.today-event-row\b/);
  assert.doesNotMatch(legacyCss, /\.today-task-row\b/);
});

test('Today v2 uses the Penpot two-column content measure instead of stretching across the workspace', async () => {
  const styles = await omtStylesPromise;
  assert.match(styles, /\.app-shell--omt-v2 \.today-content-grid,[\s\S]*?\.app-shell--omt-v2 \.omt-home-columns\s*\{[^}]*max-width:\s*996px;[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
});

test('Today section navigation sits in the Penpot section headings and stays secondary', async () => {
  const today = await todaySourcePromise;
  assert.match(today, /<div class="today-section-heading omt-section-heading">[\s\S]*?<h2 id="upcoming-heading">接下來<\/h2>[\s\S]*?<md-button[^>]*class="omt-section-heading-link"[^>]*@click="emit\('go-calendar'\)">查看行事曆/);
  assert.match(today, /<div class="today-section-heading omt-section-heading">[\s\S]*?<h2 id="today-tasks-title">待辦<\/h2>[\s\S]*?<md-button[^>]*class="omt-section-heading-link"[^>]*@click="emit\('go-lists'\)">查看全部/);
  assert.doesNotMatch(today, /omt-section-date/);
  assert.equal((today.match(/emit\('go-calendar'\)/g) || []).length, 2, 'calendar navigation remains available in the error state and heading');
  assert.equal((today.match(/emit\('go-lists'\)/g) || []).length, 1);
});

test('Today Capture keeps actions in a separate row below the MD3 field', async () => {
  const [composer, styles] = await Promise.all([captureComposerSourcePromise, omtStylesPromise]);
  assert.match(composer, /<div class="capture-input-surface">[\s\S]*?<capture-text-field[\s\S]*?<div class="capture-composer-actions">/);
  assert.match(styles, /\.app-shell--omt-v2 \.capture-input-surface\s*\{[^}]*display:\s*grid;[^}]*gap:\s*8px/);
  assert.match(styles, /\.app-shell--omt-v2 \.capture-input-surface \.capture-composer-actions\s*\{[^}]*position:\s*static/);
  assert.doesNotMatch(styles, /\.app-shell--omt-v2 \.capture-input-surface \.capture-composer-actions\s*\{[^}]*position:\s*absolute/);
  assert.match(styles, /\.app-shell--omt-v2 \.capture-input-surface \.capture-composer-textarea\s*\{[^}]*min-height:\s*112px/);
});

test('Today exposes the shared Capture composer and opens review from the page', async () => {
  const [today, app, composer] = await Promise.all([todaySourcePromise, appSourcePromise, captureComposerSourcePromise]);
  assert.match(today, /<CaptureComposer[^>]*inline-submit/);
  assert.match(app, /@capture-submit="submitCapture"/);
  assert.match(app, /@capture-files="addFiles"/);
  assert.doesNotMatch(today, /待確認的變更|查看這批|groupPendingProposalsBySource/);
  assert.doesNotMatch(today, /pendingProposals|pendingProposalCount|proposalBusy|proposalError|view-proposal/);
  assert.doesNotMatch(app, /pendingProposals|pendingProposalCount/);
  assert.match(composer, /貼上文字或輸入網址/);
  assert.match(composer, /加入圖片或檔案/);
  assert.match(composer, /props\.inlineSubmit/);
  assert.match(app, /newCaptureSourceId\.value/);
  assert.match(app, /\/discard`/);
});

test('Calendar v2 owns month, agenda, and detail composition without legacy route geometry', async () => {
  const [calendar, app, omt, legacy] = await Promise.all([calendarSourcePromise, appSourcePromise, omtStylesPromise, workspaceStylesPromise]);
  assert.ok(calendar.indexOf('class="calendar-panel') < calendar.indexOf('class="calendar-agenda"'));
  assert.doesNotMatch(calendar, /calendar-go-lists|前往清單|接下來的行程|最多顯示/);
  assert.match(app, /const selectedCalendarDateKey = ref\(dateKey\(new Date\(\)\)\)/);
  assert.match(omt, /\.app-shell--omt-v2 \.route-calendar-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(320px, 440px\);[^}]*grid-template-areas:\s*"month agenda"\s*"month detail"/);
  assert.match(omt, /@media \(max-width: 900px\)[\s\S]*?\.route-calendar-layout[\s\S]*?grid-template-areas:\s*"month" "agenda"/);
  assert.doesNotMatch(legacy, /\.route-calendar-layout\b/);
  assert.doesNotMatch(legacy, /\.route-calendar \.calendar-day\b/);
  assert.match(calendar, /class="omt-calendar-month-label"[^>]*>\{\{ monthDisplayLabel \}\}<\/strong>/);
  assert.match(calendar, /<md-button color="text" size="small" class="calendar-today-action"[^>]*>今天<\/md-button>/);
  assert.match(calendar, /<md-icon-button class="md3-month-button" aria-label="上一個月"/);
  assert.match(calendar, /<md-dialog[\s\S]*class="omt-mobile-detail-dialog"/);
  assert.match(calendar, /window\.matchMedia\('\(max-width: 900px\)'\)/);
});

test('Proposal review keeps uncertainty on the item and treats start-only Events as complete', async () => {
  const [review, item] = await Promise.all([
    proposalReviewSourcePromise,
    readFile(new URL('./components/ProposalItem.vue', import.meta.url), 'utf8'),
  ]);
  assert.match(review, /確認加入內容/);
  assert.match(review, /proposalCountLabel/);
  assert.doesNotMatch(review, /缺起迄時間|reviewSummary/);
  assert.match(review, /proposal\.target_type === 'event' \? '行程' : '待辦'/);
  assert.match(review, /class="proposal-review-card" role="dialog"/);
  assert.match(review, /先補充標示的資訊/);
  assert.match(item, /const hasStart = Boolean\(patch\.start_at\) \|\| times\.length >= 1/);
  assert.match(item, /開始時間或全天選項/);
  assert.match(item, /!expanded && !hasUncertainty/);
  assert.match(item, /aria-expanded="expanded"/);
  assert.match(item, /<md-button color="text" size="small" class="proposal-text-action"/);
  assert.match(item, /<md-button color="filled" size="small" class="proposal-primary-action"/);
  assert.doesNotMatch(item, /<button[^>]*class="proposal-(?:text|primary)-action"/);
});

test('Todo v2 keeps aggregate filter semantics and secondary list management', async () => {
  const [lists, detail, app] = await Promise.all([listViewSourcePromise, taskDetailSourcePromise, appSourcePromise]);
  assert.match(lists, /<section v-if="activeFilter === 'all' && visibleTasks\.length"[\s\S]*?<TaskRow v-for="task in visibleTasks"/);
  assert.match(lists, /<details class="list-management"/);
  assert.match(lists, /<summary>[\s\S]*?清單/);
  assert.match(lists, /window\.matchMedia\('\(max-width: 900px\)'\)/);
  assert.match(lists, /<md-dialog[^>]*class="omt-mobile-detail-dialog"/);
  assert.doesNotMatch(detail, /<dt>個人清單<\/dt>/);
  assert.match(detail, /label="所屬清單"/);
  assert.match(detail, />連結行程<\/span>/);
  assert.match(detail, />待辦時間<\/span>/);
  assert.match(detail, /class="omt-event-link-search"/);
  assert.doesNotMatch(detail, /查看原始內容|重試載入原始內容/);
  assert.doesNotMatch(detail, /全部待辦/);
  const visibleTasksBlock = app.slice(app.indexOf('const visibleTasks = computed'), app.indexOf('const calendarAgendaEvents', app.indexOf('const visibleTasks = computed')));
  assert.match(visibleTasksBlock, /activeFilter\.value === 'all'/);
  assert.match(visibleTasksBlock, /activeFilter\.value === 'done' && task\.status === 'done'/);
  assert.match(visibleTasksBlock, /activeFilter\.value === 'today' && task\.status !== 'done' && task\.dueStatus === 'today'/);
  const toggleBlock = app.slice(app.indexOf('async function toggleTask'), app.indexOf('function requestTaskDeleteConfirmation'));
  assert.doesNotMatch(toggleBlock, /activeFilter\.value\s*=/);
});

test('Preview treats omitted Task due/list as optional and keeps uncertainty item-local', async () => {
  const item = await proposalItemSourcePromise;
  assert.match(item, /const taskDueExplicitlyUncertain = computed\(\(\) =>[\s\S]*?needs_review[\s\S]*?review_reason/);
  assert.match(item, /return taskDueExplicitlyUncertain\.value \? '尚未確認' : ''/);
  assert.doesNotMatch(item, /return props\.proposal\.needs_review \? '尚未確認' : ''/);
  assert.doesNotMatch(item, /清單尚未確認|list_id.*尚未確認/);
});

test('all destructive confirmations share one compact MD3 dialog contract', async () => {
  const [app, styles, legacy] = await Promise.all([appSourcePromise, omtStylesPromise, workspaceStylesPromise]);
  assert.match(app, /class="omt-destructive-dialog event-delete-confirmation"/);
  assert.match(app, /class="omt-destructive-dialog task-delete-confirmation"/);
  assert.match(app, /class="omt-destructive-dialog list-delete-confirmation"/);
  assert.match(styles, /md-dialog\.omt-destructive-dialog\s*\{[^}]*width:\s*min\(360px, calc\(100vw - 32px\)\);[^}]*--md-dialog-container-elevation:\s*1/);
  assert.match(styles, /\.omt-destructive-dialog \[slot="headline"\][\s\S]*?padding:/);
  assert.doesNotMatch(legacy, /md-dialog\.event-delete-confirmation|md-dialog\.task-delete-confirmation/);
});

test('Task Proposal deadline uses the Material date field and the ISO due PATCH field', async () => {
  const item = await readFile(new URL('./components/ProposalItem.vue', import.meta.url), 'utf8');
  const styles = await captureStylesPromise;
  assert.match(item, /<capture-text-field color="outlined" type="date" :value="draft\.due"[^>]*aria-describedby="proposal-due-hint"/);
  assert.match(item, /patch\.due = patch\.due \|\| null/);
  assert.match(item, /delete patch\.due_label/);
  assert.match(item, /const taskDueExplicitlyUncertain = computed\(\(\) =>[\s\S]*?proposal\.needs_review[\s\S]*?期限\|截止日期\|日期/);
  assert.match(item, /return taskDueExplicitlyUncertain\.value \? '尚未確認' : ''/);
  assert.match(item, /const taskDueUnresolved = computed\(\(\) =>[\s\S]*?taskDueExplicitlyUncertain\.value/);
  assert.match(item, /v-else-if="taskDueUnresolved">截止日期/);
  assert.match(styles, /\.proposal-edit-fields capture-text-field\s*\{[^}]*width:\s*100%/);
  assert.match(styles, /\.proposal-field-hint/);
});

test('Event Editor uses precise native date/time controls with Material actions', async () => {
  const editor = await eventEditorSourcePromise;
  assert.match(editor, /<md-icon-button class="event-editor-close"/);
  assert.match(editor, /<input v-model="draft\.date_label"[^>]*type="date"/);
  assert.match(editor, /<input v-model="draft\.start_time"[^>]*type="time"[^>]*step="60"/);
  assert.match(editor, /<input v-model="draft\.end_time"[^>]*type="time"[^>]*step="60"/);
  assert.match(editor, /<md-checkbox touch-target="wrapper"/);
  assert.match(editor, /<md-button color="filled"[^>]*class="event-editor-save"/);
  assert.match(editor, /<textarea v-model="draft\.description"/);
  assert.doesNotMatch(editor, /capture-text-field|arrow_forward|event-external-notice/);
  assert.match(editor, /class="event-editor-field event-editor-field--date"/);
  assert.match(editor, /結束時間（選擇性）/);
  assert.doesNotMatch(editor, /使用 24 小時制；只填開始時間也可以。/);
  assert.match(editor, /結束時間必須晚於開始時間/);
  assert.match(editor, /\.event-series-toggle md-checkbox\s*\{[^}]*flex:\s*0 0 auto/);
});

test('Task detail stays focused on task actions and the account menu uses the real profile avatar', async () => {
  const [taskDetail, shell, app] = await Promise.all([taskDetailSourcePromise, workspaceShellSourcePromise, appSourcePromise]);
  assert.doesNotMatch(taskDetail, /view-source|retry-source|查看原始內容|重試載入原始內容/);
  assert.doesNotMatch(taskDetail, /emit\('view-source'|emit\('retry-source'/);
  assert.match(taskDetail, /label="所屬清單"/);
  assert.match(taskDetail, />連結行程<\/span>/);
  assert.match(taskDetail, /class="omt-event-link-search"/);
  assert.match(taskDetail, /placeholder="搜尋名稱、日期或星期幾"/);
  assert.match(app, /profile-avatar-url="currentUser\?\.avatar_url \|\| ''"/);
  assert.match(shell, /props\.profileAvatarUrl && !avatarLoadFailed/);
  assert.match(shell, /@error="avatarLoadFailed = true"/);
  assert.match(shell, /<AppIcon v-else name="account_circle" \/>/);
});

test('Calendar, Tasks, and Inbox source lookups normalize notice detail responses', async () => {
  const app = await appSourcePromise;
  assert.match(app, /import \{ sourceDetailsFromPayload \} from '\.\/inboxData\.js'/);
  assert.match(app, /sourceDetailsFromPayload\(result, sourceId\)/);
  assert.match(app, /sourceDetailsFromPayload\(sourcePayload, sourceId\)/);
});

test('Capture shows localized request failures and source-aware empty-result guidance', async () => {
  const [app, review] = await Promise.all([appSourcePromise, proposalReviewSourcePromise]);
  assert.match(app, /requestErrorStatus\.value = error instanceof ApiError \? error\.status : 0/);
  assert.match(app, /captureRequestFailureMessage\(sourceUrl, requestErrorStatus\.value\)/);
  assert.match(review, /captureEmptyResult\(props\.source\)/);
  assert.match(review, /emptyResult\.title/);
  assert.match(review, /emptyResult\.message/);
});

test('Calendar connection copy stays concise and does not repeat the missing-connection status', async () => {
  const settings = await settingsSourcePromise;
  const calendarStart = settings.indexOf('<article class="provider-row">');
  const calendarEnd = settings.indexOf('<section class="settings-section" aria-labelledby="settings-login-title">', calendarStart);
  const calendarSection = settings.slice(calendarStart, calendarEnd);
  const googleCard = calendarSection.match(/<article class="provider-row">[\s\S]*?Google Calendar[\s\S]*?<\/article>/)?.[0];
  const nextcloudCard = calendarSection.match(/<article class="provider-row">[\s\S]*?Nextcloud Calendar[\s\S]*?<\/article>/)?.[0];
  assert.ok(googleCard, 'Google Calendar card exists');
  assert.ok(nextcloudCard, 'Nextcloud Calendar card exists');
  assert.match(googleCard, /<h3>Google Calendar<\/h3>/);
  assert.match(googleCard, /connect-google-calendar/);
  assert.match(googleCard, /calendarConnectionLabel\(googleConnection\)/);
  assert.match(googleCard, /sync-integration[^\n]*googleConnection\.id/);
  assert.doesNotMatch(googleCard, /事件同步尚未開放/);
  assert.match(settings, /googleNeedsWriteReconnect/);
  assert.match(nextcloudCard, /calendarConnectionLabel\(nextcloudConnection\)/);
  assert.doesNotMatch(calendarSection, /provider-status|未連接|狀態未知/);
  const statusHelper = settings.slice(settings.indexOf('function calendarConnectionLabel'), settings.indexOf('function formatLastSync'));
  assert.match(statusHelper, /if \(!connection\) return ''/);
  assert.match(statusHelper, /connected: '已連接'/);
  assert.doesNotMatch(statusHelper, /disconnected:|未連接|狀態未知/);
  assert.doesNotMatch(settings, /Google Calendar 尚未連接/);
  assert.doesNotMatch(settings, /class="provider-status"[\s\S]*calendarConnectionLabel/);
});

test('Task checkbox uses the Material check glyph at frame scale', async () => {
  const styles = await workspaceStylesPromise;
  assert.match(styles, /\.route-list-surface \.md3-task-checkbox\s*\{[^}]*--md-checkbox-icon-size:\s*18px/);
});

test('Todo v2 keeps the task list and detail panel on the Penpot desktop measure', async () => {
  const styles = await omtStylesPromise;
  assert.match(styles, /\.app-shell--omt-v2 \.omt-tasks-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0, 700px\)/);
  assert.match(styles, /\.app-shell--omt-v2 \.omt-tasks-layout:has\(\.omt-task-detail-panel\)\s*\{[^}]*grid-template-columns:\s*minmax\(0, 700px\) minmax\(320px, 440px\);[^}]*gap:\s*60px/);
  assert.match(styles, /\.app-shell--omt-v2 \.omt-task-list-panel\s*\{[^}]*max-width:\s*700px/);
});

test('Task filters only set visibility and checkbox changes request an explicit status', async () => {
  const [view, taskRow, app] = await Promise.all([
    listViewSourcePromise,
    readFile(new URL('./components/TaskRow.vue', import.meta.url), 'utf8'),
    appSourcePromise,
  ]);
  assert.match(view, /:aria-pressed="activeFilter === filter\.key"/);
  assert.match(view, /@click="emit\('filter', filter\.key\)"/);
  assert.match(view, /v-if="activeFilter === 'today'">今天沒有待辦/);
  assert.match(view, /v-else-if="activeFilter === 'done'">目前沒有已完成待辦/);
  assert.match(view, /@click="emit\('filter', 'all'\)"/);
  assert.match(app, /activeFilter\.value === 'today' && task\.status !== 'done' && task\.dueStatus === 'today'/);
  assert.match(taskRow, /status: event\.target\.checked \? 'done' : 'open'/);
  assert.match(app, /if \(!target\.id \|\| nextStatus === previousStatus\) return/);
});

test('Calendar agenda heading follows the Penpot date plus item-count pattern', async () => {
  const calendar = await calendarSourcePromise;
  assert.match(calendar, /<h2 id="calendar-agenda-title">\{\{ selectedDayLabel \|\| monthDisplayLabel \}\}<\/h2>/);
  assert.doesNotMatch(calendar, /選擇日期查看行程|點選月曆中的日期/);
  assert.match(calendar, /class="calendar-agenda-count"[^>]*>\{\{ agendaEvents\.length \}\} 項<\/span>/);
  assert.doesNotMatch(calendar, /calendarAgendaRangeLabel\(/);
  assert.doesNotMatch(calendar, /\$\{selectedDayLabel\} 行程/);
});

test('Calendar places selected Event detail on the right for landscape desktop and below for portrait-ish windows', async () => {
  const styles = await omtStylesPromise;
  assert.match(styles, /\.app-shell--omt-v2 \.calendar-event-detail\s*\{[^}]*grid-area:\s*detail;/);
  assert.match(styles, /@media \(min-width: 901px\) and \(min-aspect-ratio: 7\/5\)[\s\S]*?\.route-calendar-layout\.has-selected-event[\s\S]*?grid-template-areas:\s*"month detail"\s*"agenda detail"/);
  assert.match(styles, /@media \(min-width: 901px\) and \(max-aspect-ratio: 7\/5\)[\s\S]*?\.route-calendar-layout\.has-selected-event[\s\S]*?grid-template-areas:\s*"month"\s*"agenda"\s*"detail"/);
  assert.match(styles, /@media \(min-width: 901px\) and \(min-aspect-ratio: 7\/5\)[\s\S]*?\.calendar-event-detail[\s\S]*?position:\s*sticky/);
});

test('Calendar uses the Material divider and MD3 state colors without date cell lines', async () => {
  const [calendar, styles] = await Promise.all([
    calendarSourcePromise,
    readFile(new URL('../omt-v2.css', import.meta.url), 'utf8'),
  ]);
  assert.match(calendar, /<md-divider class="md3-calendar-divider"/);
  assert.match(styles, /\.app-shell--omt-v2 \.week-labels\s*\{[^}]*border:\s*0/);
  assert.match(styles, /--md-divider-color:\s*var\(--omt-outline-variant\)/);
  assert.match(styles, /\.app-shell--omt-v2 \.calendar-day\.selected > span\s*\{[^}]*background:\s*var\(--omt-primary-container\)/);
});

test('Page 1 navigation keeps the Material 3 rail indicator and explicit keyboard focus treatment', async () => {
  const styles = await readFile(new URL('../omt-v2.css', import.meta.url), 'utf8');
  assert.match(styles, /\.workspace-rail-item__indicator\s*\{[^}]*width:\s*56px;[^}]*height:\s*32px;[^}]*border-radius:\s*9999px/);
  assert.match(styles, /\.workspace-rail-item:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--workspace-rail-icon-active\)/);
  assert.doesNotMatch(styles, /\.app-shell--workspace \.material-nav-rail md-navigation-tab/);
});

test('Settings keeps its existing MD3 composition, language, and OMT semantic tokens', async () => {
  const [settings, styles] = await Promise.all([
    settingsSourcePromise,
    readFile(new URL('../settings.css', import.meta.url), 'utf8'),
  ]);
  assert.match(settings, /class="settings-page"/);
  assert.match(settings, /個人資料/);
  assert.match(settings, /行事曆連接/);
  assert.match(settings, /登入帳戶/);
  assert.match(settings, /帳戶/);
  assert.equal((settings.match(/class="settings-divider"/g) || []).length, 3);
  assert.match(settings, /<md-button[^>]*settings-button-primary[^>]*color="filled"[^>]*size="small"/);
  assert.match(settings, /<md-button[^>]*settings-button-secondary[^>]*color="outlined"[^>]*size="small"/);
  assert.doesNotMatch(settings, /<button[^>]*settings-button/);
  assert.match(styles, /var\(--omt-primary/);
  assert.match(styles, /var\(--omt-on-surface/);
  assert.match(styles, /var\(--omt-outline-variant/);
  assert.match(styles, /--md-outlined-button-outline-color:\s*var\(--omt-outline/);
  assert.match(styles, /--md-button-container-color:\s*var\(--omt-primary/);
});

test('remaining selectors use Material ESM md-select with the locked public outlined field tokens', async () => {
  const [main, lists, detail, proposal, omtStyles, captureStyles] = await Promise.all([
    mainSourcePromise,
    listViewSourcePromise,
    taskDetailSourcePromise,
    proposalItemSourcePromise,
    omtStylesPromise,
    captureStylesPromise,
  ]);
  assert.match(main, /material\/select\/select\.js/);
  assert.match(main, /material\/select\/select-option\.js/);
  for (const source of [lists, detail, proposal]) {
    assert.match(source, /<md-select\b/);
    assert.match(source, /<md-select-option\b/);
    assert.doesNotMatch(source, /<select\b|<option\b/);
  }
  assert.match(omtStyles, /--md-outlined-select-text-field-outline-color:\s*transparent/);
  for (const source of [lists, detail, proposal]) assert.match(source, /<span slot="headline">/);
  assert.match(omtStyles, /--md-outlined-select-text-field-focus-outline-color:\s*var\(--omt-primary\)/);
  assert.match(captureStyles, /\.proposal-target-select\s*\{[\s\S]*?--md-outlined-select-text-field-outline-color:/);
});

test('core icons render from local SVG paths rather than Material Symbols ligature text', async () => {
  const [main, icon, shell, app, calendar, lists] = await Promise.all([
    mainSourcePromise,
    appIconSourcePromise,
    workspaceShellSourcePromise,
    appSourcePromise,
    calendarSourcePromise,
    listViewSourcePromise,
  ]);
  assert.match(main, /import AppIcon from ['"]\.\/components\/AppIcon\.vue['"]/);
  assert.doesNotMatch(main, /material\/icon\/icon\.js/);
  assert.match(icon, /const ICON_PATHS = Object\.freeze/);
  assert.match(icon, /home:/);
  assert.match(icon, /calendar_month:/);
  assert.match(icon, /check_box:/);
  assert.match(icon, /tune:/);
  assert.match(icon, /<svg[\s\S]*?<path :d="ICON_PATHS\[props\.name\]/);
  for (const source of [shell, app, calendar, lists]) assert.doesNotMatch(source, /<md-icon(?:\s|>)/);
});

test('Capture keeps the proven 112px field while compact actions use small Material buttons', async () => {
  const [composer, styles] = await Promise.all([captureComposerSourcePromise, omtStylesPromise]);
  assert.match(composer, /color="text" size="small" class="capture-add-file"/);
  assert.match(composer, /color="filled" size="small" class="capture-inline-submit"/);
  assert.match(styles, /\.capture-input-surface\s*\{[^}]*display:\s*grid;[^}]*gap:\s*8px/);
  assert.match(styles, /\.capture-input-surface \.capture-composer-textarea\s*\{[^}]*height:\s*112px;[^}]*min-height:\s*112px/);
});

test('workspace polish keeps motion optional, branding unselectable, and delete dialogs compact with shared elevation', async () => {
  const [shell, styles, omtStyles] = await Promise.all([workspaceShellSourcePromise, workspaceStylesPromise, omtStylesPromise]);
  assert.match(omtStyles, /\.app-shell--workspace \.omt-page-enter-active[\s\S]*?transition:\s*opacity 160ms ease, transform 160ms ease/);
  assert.match(omtStyles, /prefers-reduced-motion:\s*reduce[\s\S]*?animation:\s*none/);
  assert.match(styles, /\.brand[\s\S]*?user-select:\s*none/);
  assert.match(omtStyles, /md-dialog\.omt-destructive-dialog\s*\{[^}]*width:\s*min\(360px, calc\(100vw - 32px\)\);[^}]*--md-dialog-container-elevation:\s*1/);
  assert.doesNotMatch(styles, /md-dialog\.(?:event|task)-delete-confirmation/);
  assert.match(shell, /<md-navigation-tab[^>]*:active="route\.path === item\.path"/);
});

test('custom lists delete through a confirmation and return the UI to the ungrouped list', async () => {
  const [app, lists] = await Promise.all([appSourcePromise, listViewSourcePromise]);
  assert.match(lists, /@click="emit\('delete-list',\s*\{\s*id:\s*selectedList\.id/);
  assert.match(app, /@delete-list="requestListDeleteConfirmation"/);
  assert.match(app, /request\(`\/lists\/\$\{[^}]+\}`,\s*\{\s*method:\s*'DELETE'/);
  assert.match(app, /state\.tasks = state\.tasks\.map\(\(task\) => task\.list_id === list\.id \? \{ \.\.\.task, list_id: null \} : task\)/);
  assert.match(app, /selectedListId\.value = ''/);
});

test('Capture batch confirmation and Apply use one isolated batch request', async () => {
  const app = await appSourcePromise;
  const confirmApply = app.slice(app.indexOf('async function confirmAndApplyProposals'), app.indexOf('async function refreshProposalDomainState'));
  const acceptedApply = app.slice(app.indexOf('async function applyAcceptedProposals'), app.indexOf('async function confirmAndApplyProposals'));
  assert.match(confirmApply, /request\('\/proposals\/apply-batch'/);
  assert.match(acceptedApply, /request\('\/proposals\/apply-batch'/);
  assert.equal((confirmApply.match(/request\('\/proposals\/apply-batch'/g) || []).length, 1);
  assert.equal((acceptedApply.match(/request\('\/proposals\/apply-batch'/g) || []).length, 1);
  assert.doesNotMatch(confirmApply, /request\(`\/proposals\/\$\{proposal\.id\}\/(?:accept|apply)`/);
  assert.doesNotMatch(acceptedApply, /request\(`\/proposals\/\$\{proposal\.id\}\/apply`/);
  assert.match(confirmApply, /results/);
});

test('calendar event edits stay inside the selected detail surface', async () => {
  const [detail, editor, calendar, app] = await Promise.all([
    readFile(new URL('./components/CalendarEventDetail.vue', import.meta.url), 'utf8'),
    eventEditorSourcePromise,
    calendarSourcePromise,
    appSourcePromise,
  ]);
  assert.match(detail, /<EventEditorDialog[\s\S]*?embedded/);
  assert.doesNotMatch(detail, /omt-detail-editing[\s\S]*?<h2>編輯行程<\/h2>/);
  assert.match(editor, /embedded:\s*\{\s*type:\s*Boolean/);
  assert.match(calendar, /@edit="emit\('edit-event'/);
  assert.doesNotMatch(app, /<EventEditorDialog/);
});

test('calendar sync is scheduled in the background when the workspace returns', async () => {
  const app = await appSourcePromise;
  const nextcloudPoll = app.slice(app.indexOf('async function pollNextcloudLogin'), app.indexOf('async function startNextcloudLogin'));
  assert.match(app, /document\.addEventListener\('visibilitychange', onCalendarAppVisible\)/);
  assert.match(app, /\/calendar\/auto-sync/);
  assert.match(app, /automaticCalendarSyncTimer/);
  assert.match(nextcloudPoll, /result\.status === 'connected'[\s\S]*lastAutomaticCalendarSyncAt = 0[\s\S]*onCalendarAppVisible\(\)/);
});

test('expired Google Calendar authorization asks for reconnect instead of surfacing a generic sync failure', async () => {
  const app = await appSourcePromise;
  const sync = app.slice(app.indexOf('async function syncIntegration'), app.indexOf('function captureFileKind'));
  assert.match(sync, /error\.status === 409 && provider === 'google_calendar'/);
  assert.match(sync, /Google Calendar 授權已失效，請解除連線後重新授權。/);
  assert.match(sync, /await loadIntegrations\(\)/);
});

test('calendar and task mutations keep the cross-route caches coherent', async () => {
  const app = await appSourcePromise;
  const taskToggle = app.slice(app.indexOf('async function toggleTask'), app.indexOf('function requestTaskDeleteConfirmation'));
  const listDelete = app.slice(app.indexOf('async function deleteList'), app.indexOf('async function assignTaskList'));
  const autoSync = app.slice(app.indexOf('function onCalendarAppVisible'), app.indexOf('async function connectGoogleCalendar'));
  const disconnect = app.slice(app.indexOf('async function disconnectIntegration'), app.indexOf('async function syncIntegration'));

  assert.match(taskToggle, /state\.tasks = state\.tasks\.map\(\(item\) => item\.id === result\.id \? result : item\)/);
  assert.match(taskToggle, /state\.tasks = state\.tasks\.map\(\(item\) => item\.id === target\.id \? \{ \.\.\.item, status: previousStatus \} : item\)/);
  assert.match(listDelete, /state\.tasks = state\.tasks\.map\(\(task\) => task\.list_id === list\.id \? \{ \.\.\.task, list_id: null \} : task\)/);
  assert.match(autoSync, /Promise\.all\(\[loadCalendarEvents\(currentMonth\.value\), loadOverviewCalendarEvents\(\)\]\)/);
  assert.match(disconnect, /clearCalendarCaches\(\);[\s\S]*await loadDashboard\(\)/);
});

test('event deletion uses an accessible Material confirmation with range and focus handling', async () => {
  const [app, styles] = await Promise.all([appSourcePromise, omtStylesPromise]);
  assert.doesNotMatch(app, /window\.(confirm|alert)/);
  assert.match(app, /class="omt-destructive-dialog event-delete-confirmation"[^>]*quick aria-labelledby="event-delete-title" aria-describedby="event-delete-description" @cancel="closeEventDeleteConfirmation"/);
  assert.match(app, /刪除整個行程系列？/);
  assert.match(app, /所有行程都會移除/);
  assert.match(app, /刪除系列|刪除行程/);
  assert.match(app, /eventDeleteReturnFocus\.value\?\.focus/);
  assert.match(styles, /md-dialog\.omt-destructive-dialog/);
  assert.match(styles, /\.omt-destructive-dialog :is\(\.event-delete-confirm-action, \.task-delete-confirm-action, \.account-delete-confirm-action\)/);
});

test('successful no-content API responses remain successful request results', async () => {
  const app = await appSourcePromise;
  const requestJson = app.slice(app.indexOf('async function requestJson'), app.indexOf('const workspacePaths'));
  assert.match(requestJson, /if \(response\.status === 204\) return \{\}/);
});

test('Tasks list exposes a confirmed delete action and updates cached state after API success', async () => {
  const [app, lists, taskDetail] = await Promise.all([appSourcePromise, listViewSourcePromise, taskDetailSourcePromise]);
  const deletion = app.slice(app.indexOf('async function deleteTask'), app.indexOf('function navigateWorkspace'));
  assert.match(lists, /'delete-task'/);
  assert.match(lists, /@delete="emit\('delete-task', \$event\)"/);
  assert.match(taskDetail, /emit\('delete', props\.task\)/);
  assert.match(app, /@delete-task="requestTaskDeleteConfirmation"/);
  assert.match(app, /function requestTaskDeleteConfirmation\(task\)/);
  assert.match(app, /class="omt-destructive-dialog task-delete-confirmation"[^>]*quick aria-labelledby="task-delete-title" aria-describedby="task-delete-description"/);
  assert.match(deletion, /request\(`\/tasks\/\$\{task\.id\}`, \{ method: 'DELETE' \}\)/);
  assert.match(deletion, /state\.tasks = state\.tasks\.filter/);
  assert.match(deletion, /listTaskRecords\.value = listTaskRecords\.value\.filter/);
  assert.equal(deletion.includes('await loadDashboard()'), false);
  assert.equal(deletion.includes('await loadListTasks(selectedListId.value)'), false);
});

test('closing an applied Capture Preview does not send a discard request', async () => {
  const app = await appSourcePromise;
  const batchApply = app.slice(app.indexOf('async function confirmAndApplyProposals'), app.indexOf('async function refreshProposalDomainState'));
  const appliedGuard = app.slice(app.indexOf('function captureHasAppliedProposal'), app.indexOf('async function returnToCapture'));
  const closeReview = app.slice(app.indexOf('async function returnToCapture'), app.indexOf('const workspaceViewProps'));

  assert.match(batchApply, /if \(String\(proposal\.source_id \|\| ''\) === newCaptureSourceId\.value\) newCaptureSourceId\.value = ''/);
  assert.match(appliedGuard, /proposalApplyResults\.value\.some\(\(result\) => result\.status === 'applied'\)/);
  assert.match(appliedGuard, /proposal\.status === 'applied'/);
  assert.match(closeReview, /if \(newCaptureSourceId\.value && !captureHasAppliedProposal\(\)\)/);
  assert.match(closeReview, /request\(`\/notices\/\$\{encodeURIComponent\(newCaptureSourceId\.value\)\}\/discard`, \{ method: 'POST' \}\)/);
});

test('Capture refuses a preview whose proposals or Source detail belong elsewhere', async () => {
  const app = await appSourcePromise;
  const submit = app.slice(app.indexOf('async function submitCapture'), app.indexOf('async function refreshProposal'));
  assert.match(submit, /\(result\.proposals \|\| \[\]\)\.filter\(\(proposal\) => \['pending', 'edited'\]\.includes\(proposal\.status\) && isPreviewableProposal\(proposal\)\)/);
  assert.match(submit, /const authoritativeSourceId = String\(result\.source\?\.id \|\| ''\)/);
  assert.match(submit, /proposal\.source_id.*authoritativeSourceId/);
  assert.match(submit, /String\(proposal\.source_id \|\| ''\) === authoritativeSourceId/);
  assert.match(submit, /String\(sourceDetails\.id\) !== authoritativeSourceId/);
  assert.match(submit, /尚未顯示或套用/);
  assert.match(submit, /source: sourceDetails \|\| result\.source/);
  assert.match(submit, /processing\?\.ai\?\.status === 'too_large'/);
  assert.match(submit, /這段原始資訊太長，無法安全整理完整內容/);
});

test('Capture Preview stays compact and keeps actions visible for long proposal lists', async () => {
  const [review, styles] = await Promise.all([proposalReviewSourcePromise, demoPolishStylesPromise]);
  assert.match(review, /class="proposal-review-list"/);
  assert.match(review, /class="proposal-review-card" role="dialog"/);
  assert.match(styles, /\.proposal-review-layer\s*\{[^}]*overflow:\s*hidden/);
  assert.match(styles, /\.proposal-review-card\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*max-height:/);
  assert.match(styles, /\.proposal-review-body\s*\{[^}]*flex:\s*1 1 auto;[^}]*min-height:\s*0;[^}]*overflow:\s*auto/);
  assert.match(styles, /\.proposal-review-actions\s*\{[^}]*flex:\s*0 0 auto/);
  assert.doesNotMatch(review, /proposal-apply-results|套用結果/);
  assert.doesNotMatch(review, /proposal-source|原始內容|SourceDetailPanel|showOriginal/);
});

test('Inbox keeps the complete Source detail surface while Proposal Review stays task-focused', async () => {
  const [inbox, review, detail, styles] = await Promise.all([
    inboxViewSourcePromise,
    proposalReviewSourcePromise,
    sourceDetailSourcePromise,
    captureStylesPromise,
  ]);
  assert.match(inbox, /import SourceDetailPanel from '\.\.\/components\/SourceDetailPanel\.vue'/);
  assert.doesNotMatch(review, /import SourceDetailPanel/);
  assert.match(inbox, /const showSourcePanel = computed\(\(\) => Boolean\(props\.source \|\| props\.sourceLoading \|\| props\.sourceError\)\)/);
  assert.match(inbox, /<aside v-if="showSourcePanel" class="inbox-source-panel"/);
  assert.match(inbox, /\.inbox-workspace:not\(\.has-source-panel\)\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);/);
  assert.match(inbox, /<SourceDetailPanel v-else-if/);
  assert.doesNotMatch(review, /proposal-source|原始內容|showOriginal/);
  assert.match(detail, /sourceOriginalText\(props\.source\)/);
  assert.match(detail, /sourceAttachmentUrl\(props\.source/);
  assert.doesNotMatch(detail, /relatedProposals|proposalStatusLabel|proposal\.operation|proposal\.status|source_id|proposal_id|處理狀態：/);
  assert.doesNotMatch(detail, /props\.source\.processing\?\.status/);
  assert.match(detail, /props\.source\.created_at \|\| props\.source\.captured_at/);
  assert.match(review, /md-icon-button class="proposal-dialog-close"/);
  assert.match(detail, /sourceTypeLabel\(props\.source\)/);
  assert.match(styles, /\.proposal-dialog-close\s*\{[^}]*width:\s*48px;[^}]*height:\s*48px;/);
  assert.match(styles, /\.proposal-dialog-close:focus-visible\s*\{[^}]*outline:\s*3px/);
});

test('Capture confirms reviewable proposals first while Inbox apply-all stays accepted-only', async () => {
  const source = await appSourcePromise;
  const reviewHandler = source.slice(source.indexOf('async function confirmAndApplyProposals'), source.indexOf('async function refreshProposalDomainState'));
  assert.match(source, /@apply-all="applyAcceptedProposals"/);
  assert.match(source, /@apply-all="confirmAndApplyProposals"/);
  assert.match(reviewHandler, /confirmableProposals\(proposals\)/);
  assert.match(reviewHandler, /request\('\/proposals\/apply-batch'/);
  assert.match(reviewHandler, /confirm: true/);
  assert.doesNotMatch(reviewHandler, /request\(`\/proposals\/\$\{proposal\.id\}\/(?:accept|apply)`/);
  assert.match(reviewHandler, /if \(noticeBusy\.value\) return/);
});

test('Capture input stays compact, non-resizable, and leaves space before the footer', async () => {
  const [composer, styles, app] = await Promise.all([captureComposerSourcePromise, captureStylesPromise, appSourcePromise]);
  assert.match(composer, /accept="\.png,\.jpg,\.jpeg,\.gif,\.webp,\.bmp,\.heic,\.heif,\.pdf,\.docx,\.odt,\.ods,\.odp,\.txt,\.md,\.markdown,\.csv,\.tsv"/);
  assert.doesNotMatch(composer, /image\/\*|application\/pdf/);
  assert.match(app, /file\.type\.startsWith\('image\/'\) \|\| \/\\\.\(heic\|heif\)\$\/\.test\(filename\)/);
  assert.match(app, /filename\.endsWith\('\.docx'\)[\s\S]*?'application\/vnd\.openxmlformats-officedocument\.wordprocessingml\.document'/);
  assert.match(app, /filename\.endsWith\('\.odt'\)[\s\S]*?'application\/vnd\.oasis\.opendocument\.text'/);
  assert.match(app, /filename\.endsWith\('\.ods'\)[\s\S]*?'application\/vnd\.oasis\.opendocument\.spreadsheet'/);
  assert.match(app, /filename\.endsWith\('\.odp'\)[\s\S]*?'application\/vnd\.oasis\.opendocument\.presentation'/);
  assert.match(app, /filename\.endsWith\('\.heic'\)[\s\S]*?'image\/heic'/);
  assert.match(app, /filename\.endsWith\('\.heif'\)[\s\S]*?'image\/heif'/);
  assert.match(composer, /rows="2"/);
  assert.match(styles, /\.capture-composer-textarea[\s\S]*?resize:\s*none/);
  assert.match(styles, /\.capture-dialog-content\s*\{\s*padding-bottom:\s*8px/);
  assert.match(styles, /\.capture-composer\s*\{\s*gap:\s*2px/);
  assert.match(styles, /\.capture-composer-textarea\s*\{[^}]*height:\s*88px;[^}]*min-height:\s*88px;/);
  assert.match(styles, /\.capture-dialog-actions,\s*\.proposal-dialog-actions\s*\{[^}]*padding:\s*8px 22px 14px/);
  assert.match(styles, /@media\s*\(min-width:\s*601px\)\s*and\s*\(max-height:\s*480px\)[\s\S]*?\.capture-composer-textarea\s*\{\s*height:\s*88px;\s*min-height:\s*88px;/);
});

test('Capture review explains how to recover unreadable text attachment encodings', async () => {
  const dialog = await proposalReviewSourcePromise;
  assert.match(dialog, /attachment\.extraction_error === 'unsupported_encoding'/);
  assert.match(dialog, /另存為 UTF-8 後重新加入/);
  assert.match(dialog, /class="proposal-error proposal-attachment-encoding-error" role="alert"/);
});

test('Capture preserves Enter newlines and Ctrl+Enter submits without interrupting IME composition', async () => {
  const composer = await captureComposerSourcePromise;
  assert.match(composer, /event\.key !== 'Enter' \|\| !event\.ctrlKey \|\| event\.isComposing/);
  assert.match(composer, /event\.preventDefault\(\);\s*emit\('submit'\)/);
  assert.match(composer, /@keydown="handleComposerKeydown"/);
  assert.match(composer, /removeEventListener\('input', handleComposerInput\)/);
});

test('local event deletion calls the API and evicts cached series instances without reload', async () => {
  const [app, calendarData] = await Promise.all([appSourcePromise, calendarDataSourcePromise]);
  const handler = app.slice(app.indexOf('async function deleteEvent'), app.indexOf('async function toggleTask'));
  const deleteRequest = handler.indexOf("request(`/events/${event.id}`, { method: 'DELETE' })");
  const cacheEviction = handler.indexOf('withoutDeletedLocalEventSeries(state.events, event.id)');

  assert.ok(deleteRequest >= 0);
  assert.ok(cacheEviction > deleteRequest);
  assert.equal(handler.includes('await loadDashboard()'), false);
  assert.doesNotMatch(handler, /isExternalEvent\(event\)/);
  assert.match(handler, /eventDeleteSucceeded\.value = true/);
  assert.equal(handler.includes('refreshed'), false);
  assert.match(calendarData, /function localEventSeriesId/);
  assert.match(calendarData, /export function withoutDeletedLocalEventSeries/);
});

test('detail panels keep secondary metadata quiet instead of repeating status diagnostics', async () => {
  const [eventDetail, taskDetail] = await Promise.all([
    readFile(new URL('./components/CalendarEventDetail.vue', import.meta.url), 'utf8'),
    taskDetailSourcePromise,
  ]);
  assert.doesNotMatch(eventDetail, /<dt>類型<\/dt>/);
  assert.doesNotMatch(eventDetail, /<dt>狀態<\/dt>/);
  assert.doesNotMatch(eventDetail, /<dt>同步<\/dt>/);
  assert.match(eventDetail, /<dt>日期與時間<\/dt>/);
  assert.doesNotMatch(taskDetail, /<dt>狀態<\/dt>/);
});

test('Calendar list deletion passes the selected event into the explicit confirmation dialog', async () => {
  const [app, detail, calendar] = await Promise.all([appSourcePromise, readFile(new URL('./components/CalendarEventDetail.vue', import.meta.url), 'utf8'), calendarSourcePromise]);
  const confirmation = app.slice(app.indexOf('function requestEventDeleteConfirmation'), app.indexOf('function closeEventDeleteConfirmation'));

  assert.match(app, /@delete-event="requestEventDeleteConfirmation"/);
  assert.match(confirmation, /function requestEventDeleteConfirmation\(event = selectedEvent\.value\)/);
  assert.match(app, /function cloneEvent\(value\)/);
  assert.match(confirmation, /selectedEvent\.value = normalizeEvent\(cloneEvent\(event\)\)/);
  assert.match(confirmation, /eventDeleteDialogOpen\.value = true/);
  assert.match(detail, /@delete="emit\('delete', props\.editingDraft \|\| props\.editorEvent \|\| props\.event\)"/);
  assert.match(calendar, /@delete="emit\('delete-event', \$event\)"/);
  assert.match(app, /class="event-delete-confirm-action"[^>]*@click="deleteEvent\(\)"/);
});

test('proposal review labels updates as modifications instead of additions', async () => {
  const source = await proposalItemSourcePromise;
  assert.match(source, /if \(props\.reviewMode\) \{[\s\S]*?isUpdate\.value \? '建議修改行程' : '建議新增行程'/);
  assert.match(source, /isUpdate\.value \? '建議修改待辦' : '建議新增待辦'/);
  assert.match(source, /isUpdate\.value \? '修改行程' : '新增行程'/);
  assert.match(source, /isUpdate\.value \? '修改待辦' : '新增待辦'/);
});

test('proposal review confirmation says apply for create and update batches', async () => {
  const source = await proposalReviewSourcePromise;
  assert.match(source, /`加入 \$\{readyProposals\.value\.length\} 個項目`/);
  assert.match(source, /沒有可加入的項目/);
  assert.doesNotMatch(source, /確認並套用/);
});

test('Tasks navigation uses the shared Material icon contract', async () => {
  const [shell, nav] = await Promise.all([workspaceShellSourcePromise, workspaceNavigationSourcePromise]);
  assert.match(nav, /icon: 'check_box'/);
  assert.match(shell, /<AppIcon slot="inactive-icon" class="workspace-nav-icon" :name="item\.icon" \/>/);
  assert.match(shell, /<AppIcon slot="active-icon" class="workspace-nav-icon" :name="item\.icon" \/>/);
  assert.doesNotMatch(shell, /<md-icon[^>]*item\.icon/);
});

test('workspace keeps a fixed desktop rail and a mobile document scroller from the canonical shell authority', async () => {
  const [omt, legacy] = await Promise.all([omtStylesPromise, workspaceStylesPromise]);
  assert.match(omt, /@media\s*\(min-width:\s*901px\)[\s\S]*?\.app-shell--workspace\s*\{[^}]*height:\s*100dvh/);
  assert.match(omt, /\.app-shell--workspace \.sidebar\s*\{[\s\S]*?position:\s*sticky;[\s\S]*?top:\s*0/);
  assert.match(omt, /\.app-shell--workspace \.workspace-main\s*\{[\s\S]*?overflow-y:\s*auto/);
  assert.match(omt, /@media\s*\(max-width:\s*900px\)[\s\S]*?\.app-shell--workspace \.workspace-main\s*\{[^}]*overflow-y:\s*visible/);
  assert.match(legacy, /@media\s*\(min-width:\s*901px\)\s*and\s*\(max-height:\s*480px\)[\s\S]*?\.app-shell:not\(\.app-shell--workspace\) \.md3-sidebar/);
  assert.doesNotMatch(legacy, /^\s*\.material-nav-rail/m);
});

test('open Material dialogs dim the complete OMT shell on every viewport', async () => {
  const styles = await workspaceStylesPromise;
  assert.match(styles, /body:has\(md-dialog\[open\]\)[\s\S]*?\.app-shell--omt-v2 \.sidebar,[\s\S]*?\.app-shell--omt-v2 \.workspace-main > \.topbar,[\s\S]*?\.app-shell--omt-v2 \.mobile-nav/);
  assert.match(styles, /body:has\(md-dialog\[open\]\)[^{]*\{[^}]*z-index:\s*0\s*!important;/);
});

test('desktop OMT rail keeps a shared center line and tighter vertical rhythm', async () => {
  const styles = await omtStylesPromise;
  assert.match(styles, /\.app-shell--workspace \.sidebar\s*\{[\s\S]*?flex-direction:\s*column;[\s\S]*?align-items:\s*center;[\s\S]*?gap:\s*20px/);
  assert.match(styles, /\.app-shell--workspace \.material-nav-rail\s*\{[\s\S]*?display:\s*flex;[\s\S]*?flex-direction:\s*column;[\s\S]*?gap:\s*12px/);
  assert.doesNotMatch(await workspaceShellSourcePromise, /<md-fab/);
});

test('desktop workspace rail stays rigid instead of compressing at short or zoomed viewports', async () => {
  const styles = await omtStylesPromise;
  assert.match(styles, /\.app-shell--workspace \.material-nav-rail\s*\{[\s\S]*?overflow-y:\s*auto;[\s\S]*?scrollbar-width:\s*none/);
  assert.match(styles, /\.app-shell--workspace \.sidebar\s*\{[\s\S]*?width:\s*100px;[\s\S]*?min-width:\s*100px;[\s\S]*?max-width:\s*100px;[\s\S]*?flex:\s*0 0 100px/);
  assert.match(styles, /\.app-shell--workspace \.brand\s*\{[\s\S]*?flex:\s*0 0 auto/);
  assert.doesNotMatch(styles, /@media\s*\(min-width:\s*901px\)\s*and\s*\(max-height:\s*620px\)/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item--active \.workspace-rail-item__indicator\s*\{[^}]*background:\s*var\(--workspace-rail-active\);[^}]*box-shadow:\s*none/);
});


function mediaBlocks(source, header) {
  const blocks = [];
  let cursor = 0;
  while ((cursor = source.indexOf(header, cursor)) !== -1) {
    const open = source.indexOf('{', cursor + header.length);
    if (open === -1) break;
    let depth = 1;
    let end = open + 1;
    while (end < source.length && depth > 0) {
      if (source[end] === '{') depth += 1;
      else if (source[end] === '}') depth -= 1;
      end += 1;
    }
    blocks.push(source.slice(cursor, end));
    cursor = end;
  }
  return blocks;
}

test('workspace stylesheet keeps balanced CSS blocks after authority cleanup', async () => {
  const styles = await workspaceStylesPromise;
  assert.equal((styles.match(/\{/g) || []).length, (styles.match(/\}/g) || []).length);
});

test('workspace responsive geometry has one OMT authority and legacy CSS cannot style workspace rail', async () => {
  const [legacy, omt, settings] = await Promise.all([workspaceStylesPromise, omtStylesPromise, settingsStylesPromise]);

  assert.doesNotMatch(legacy, /^\s*\.material-nav-rail/m);
  assert.doesNotMatch(legacy, /^\s*\.md3-sidebar/m);
  assert.doesNotMatch(settings, /\.app-shell--workspace|\.material-nav-rail|--md-navigation-(?:bar|tab)-/);

  const desktopShellBlocks = mediaBlocks(omt, '@media (min-width: 901px)').filter((block) => /\.app-shell--workspace\s*\{[^}]*grid-template-columns:\s*100px/m.test(block));
  const mobileShellBlocks = mediaBlocks(omt, '@media (max-width: 900px)').filter((block) => /\.app-shell--workspace\s*\{[^}]*display:\s*block/m.test(block));
  assert.equal(desktopShellBlocks.length, 1);
  assert.equal(mobileShellBlocks.length, 1);
  assert.match(omt, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.omt-page-enter-active[\s\S]*?transition-duration:\s*1ms/);
});

test('Todo filter changes use a short upward fade and respect reduced motion', async () => {
  const [view, styles] = await Promise.all([listViewSourcePromise, omtStylesPromise]);
  assert.match(view, /<Transition name="todo-filter" mode="out-in">[\s\S]*?:key="activeFilter"/);
  assert.match(styles, /\.todo-filter-enter-from\s*\{[^}]*opacity:\s*0;[^}]*translateY\(8px\)/);
  assert.match(styles, /\.todo-filter-leave-to\s*\{[^}]*opacity:\s*0;[^}]*translateY\(-8px\)/);
  assert.match(styles, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.todo-filter-enter-active[\s\S]*?transition-duration:\s*1ms/);
});

test('workspace product styling stays mounted while routing to Settings so the leaving view cannot flash legacy UI', async () => {
  const shell = await workspaceShellSourcePromise;
  assert.match(shell, /const isWorkspaceShellRoute = computed\(\(\) => \['\/today', '\/calendar', '\/lists', '\/settings'\]\.includes\(route\.path\)\);/);
  assert.match(shell, /const isOmtV2Route = isWorkspaceShellRoute;/);
});

test('workspace informational empty states are unframed instead of dashed placeholder boxes', async () => {
  const [a11y, calendar, inbox, dashboard] = await Promise.all([
    readFile(new URL('../a11y.css', import.meta.url), 'utf8'),
    readFile(new URL('../calendar.css', import.meta.url), 'utf8'),
    inboxViewSourcePromise,
    readFile(new URL('../dashboard-sketch.css', import.meta.url), 'utf8'),
  ]);
  assert.match(a11y, /\.empty-state\{[^}]*border:0;[^}]*background:transparent/);
  assert.doesNotMatch(a11y, /\.empty-state\{[^}]*dashed/);
  assert.match(a11y, /\.calendar-login-link\{[^}]*border:0;[^}]*background:transparent/);
  assert.match(calendar, /\.route-calendar \.calendar-load-error\{[^}]*border:0;[^}]*background:transparent/);
  assert.match(calendar, /\.route-calendar \.calendar-source-error\{[^}]*border:0;[^}]*background:transparent/);
  assert.doesNotMatch(calendar, /\.add-task-button\{[^}]*dashed/);
  assert.match(inbox, /\.inbox-empty\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent/);
  assert.match(dashboard, /\.calendar-auth-modal \.empty-state\{[^}]*border:0;[^}]*background:transparent/);
});

test('workspace routes share the Settings rail palette and centered capsule geometry', async () => {
  const [shell, styles] = await Promise.all([workspaceShellSourcePromise, omtStylesPromise]);
  assert.match(shell, /isWorkspaceShellRoute = computed\(\(\) => \['\/today', '\/calendar', '\/lists', '\/settings'\]/);
  assert.match(shell, /'app-shell--workspace': isWorkspaceShellRoute/);
  assert.match(styles, /\.app-shell--workspace\s*\{[\s\S]*?--workspace-rail-surface:\s*#fbf8ff;[\s\S]*?--workspace-rail-active:\s*#dedbf5;/);
  assert.match(styles, /\.app-shell--workspace\s*\{[\s\S]*?--paper:\s*var\(--workspace-paper\);[\s\S]*?--muted:\s*var\(--workspace-rail-icon\);[\s\S]*?--panel:\s*var\(--workspace-rail-surface\);/);
  assert.match(styles, /\.app-shell--workspace \.brand\s*\{\s*color:\s*var\(--workspace-rail-icon-active\);/);
  assert.match(styles, /--workspace-paper:\s*#ffffff;/);
  assert.match(styles, /\.app-shell--workspace \.material-nav-rail\s*\{[\s\S]*?border:\s*0;[\s\S]*?border-radius:\s*0;[\s\S]*?background:\s*transparent/);
  assert.doesNotMatch(shell, /path:\s*'\/settings'.*workspace-rail/);
  assert.match(styles, /@media\s*\(min-width:\s*901px\)[\s\S]*?\.app-shell--workspace\s*\{[\s\S]*?display:\s*grid;[\s\S]*?grid-template-columns:\s*100px/);
  assert.match(styles, /\.app-shell--workspace \.sidebar\s*\{[\s\S]*?width:\s*100px;[\s\S]*?background:\s*var\(--workspace-rail-surface\)/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item\s*\{[\s\S]*?width:\s*80px;[\s\S]*?height:\s*64px;[\s\S]*?min-height:\s*64px/);
  assert.match(styles, /\.app-shell--workspace \.material-nav-rail\s*\{[\s\S]*?min-width:\s*80px;[\s\S]*?flex:\s*1 1 auto;[\s\S]*?padding:\s*0;[\s\S]*?overflow-x:\s*hidden;[\s\S]*?overflow-y:\s*auto/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item\s*\{[\s\S]*?min-width:\s*80px;[\s\S]*?flex:\s*0 0 64px/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item:not\(\.workspace-rail-item--active\) \.workspace-rail-item__indicator\s*\{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none/);
  assert.match(styles, /@media\s*\(hover:\s*hover\)\s*and\s*\(pointer:\s*fine\)[\s\S]*?workspace-rail-item:not\(\.workspace-rail-item--active\):hover \.workspace-rail-item__indicator\s*\{[^}]*background:\s*var\(--workspace-rail-hover\)/);
  assert.match(styles, /\.app-shell--workspace \.workspace-rail-item__indicator\s*\{[^}]*width:\s*56px;[^}]*height:\s*32px/);
  assert.match(styles, /@media\s*\(max-width:\s*900px\)[\s\S]*?\.app-shell--workspace \.mobile-nav\s*\{[\s\S]*?background:\s*var\(--workspace-rail-surface\);[\s\S]*?box-shadow:\s*none/);
  assert.match(styles, /\.app-shell--workspace \.sidebar\s*\{[\s\S]*?overflow-x:\s*hidden/);
  assert.match(styles, /\.app-shell--workspace \.workspace-main\s*\{[\s\S]*?overflow-x:\s*hidden/);
  assert.match(styles, /\.app-shell--workspace \.sidebar\s*\{[\s\S]*?border-right:\s*0/);
  assert.doesNotMatch(styles, /\.app-shell--workspace \.brand > span:last-child\s*\{[^}]*display:\s*none/);
  assert.match(styles, /@media\s*\(min-width:\s*901px\)[\s\S]*?\.app-shell--workspace \.brand\s*\{[\s\S]*?width:\s*80px;[\s\S]*?font:\s*600 15px/);
  assert.match(styles, /\.app-shell--workspace \.topbar md-icon-button\[slot="leading-icon"\]\s*\{[^}]*display:\s*none\s*!important/);
  assert.match(shell, /v-if="!isWorkspaceShellRoute" class="brand-mark"/);
  assert.match(shell, /<svg v-else class="brand-mark-svg"/);
  assert.match(styles, /\.app-shell--workspace \.brand > span:last-child\s*\{[^}]*display:\s*block/);
  assert.match(styles, /\.app-shell--workspace \.brand-mark-svg\s*\{[^}]*width:\s*24px;[^}]*height:\s*24px/);
  assert.match(styles, /@media\s*\(min-width:\s*901px\)\s*and\s*\(max-width:\s*1100px\)[\s\S]*?\.app-shell--workspace\.app-shell--omt-v2 \.route-calendar-layout[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(styles, /@media\s*\(min-width:\s*901px\)\s*and\s*\(max-width:\s*1100px\)[\s\S]*?\.app-shell--workspace\.app-shell--omt-v2 \.today-content-grid[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
});

test('short confirmation dialogs use real scroll geometry instead of Material false scrollbars', async () => {
  const app = await appSourcePromise;
  assert.equal((app.match(/@open="prepareConfirmationDialog"/g) || []).length, 3);
  assert.equal((app.match(/@opened="syncConfirmationDialog"/g) || []).length, 3);
  assert.match(app, /function prepareConfirmationDialog\(event\)[\s\S]*?isAtScrollTop = true;[\s\S]*?isAtScrollBottom = true;/);
  assert.match(app, /function syncConfirmationDialog\(event\)[\s\S]*?shadowRoot\?\.querySelector\('\.scroller'\)[\s\S]*?scrollHeight - scroller\.clientHeight[\s\S]*?isAtScrollTop = scroller\.scrollTop <= 1[\s\S]*?isAtScrollBottom = scroller\.scrollTop >= maxScrollTop - 1[\s\S]*?requestUpdate\?\./);
});

test('cancelled task and list confirmations return focus to a still-present trigger', async () => {
  const app = await appSourcePromise;
  assert.match(app, /const taskDeleteReturnFocus = ref\(null\)/);
  assert.match(app, /const listDeleteReturnFocus = ref\(null\)/);
  assert.match(app, /function requestListDeleteConfirmation\(list\)[\s\S]*?listDeleteReturnFocus\.value = document\.activeElement/);
  assert.match(app, /function requestTaskDeleteConfirmation\(task\)[\s\S]*?taskDeleteReturnFocus\.value = document\.activeElement/);
  assert.match(app, /function restoreConfirmationFocus\(focusTargetRef\)[\s\S]*?isConnected[\s\S]*?nextTick\([\s\S]*?focus\?\.\(\)/);
  assert.match(app, /function closeListDeleteConfirmation\(\)[\s\S]*?restoreConfirmationFocus\(listDeleteReturnFocus\)/);
  assert.match(app, /function closeTaskDeleteConfirmation\(\)[\s\S]*?restoreConfirmationFocus\(taskDeleteReturnFocus\)/);
});

test('Today hides upload budget copy in the OMT v2 composition while preserving capture errors', async () => {
  const styles = await omtStylesPromise;
  assert.match(styles, /\.app-shell--omt-v2 \.route-today \.capture-budget-hint\s*\{[^}]*display:\s*none/);
  assert.match(styles, /\.app-shell--omt-v2 \.capture-inline-error/);
});

test('Task row and detail render due dates through the shared human date formatter', async () => {
  const [row, detail] = await Promise.all([
    readFile(new URL('./components/TaskRow.vue', import.meta.url), 'utf8'),
    taskDetailSourcePromise,
  ]);
  assert.match(row, /taskDueCompactLabel/);
  assert.match(detail, /taskDueLongLabel/);
  assert.doesNotMatch(row, /\{\{\s*props\.task\.due_label\s*\|\|\s*props\.task\.due_iso/);
  assert.doesNotMatch(detail, /\{\{\s*props\.task\.due_label\s*\|\|\s*props\.task\.due_iso/);
});

test('Capture Preview supplement fields use Material field and checkbox primitives', async () => {
  const item = await proposalItemSourcePromise;
  assert.match(item, /class="proposal-edit-fields"[\s\S]*?<capture-text-field/);
  assert.match(item, /class="proposal-all-day-option"[\s\S]*?<md-checkbox/);
  assert.match(item, /class="proposal-weekday-field"[\s\S]*?<md-checkbox/);
  assert.doesNotMatch(item, /<label v-if="isEvent && missingFields\.includes\('活動名稱尚未確認'\)">活動名稱<input/);
  assert.doesNotMatch(item, /<input v-model="draft\.due" type="date"/);
});

test('Calendar v2 month and agenda dates use the spaced Traditional Chinese labels from Penpot', async () => {
  const calendar = await calendarSourcePromise;
  assert.match(calendar, /const monthDisplayLabel = computed/);
  assert.match(calendar, /\$\{date\.getMonth\(\) \+ 1\} 月 \$\{date\.getDate\(\)\} 日/);
  assert.match(calendar, /\{\{ monthDisplayLabel \}\}/);
});

test('OMT v2 product CSS does not own a second desktop rail geometry', async () => {
  const styles = await omtStylesPromise;
  const rootStart = styles.indexOf('.app-shell--omt-v2 {');
  const rootEnd = styles.indexOf('\n}', rootStart);
  const rootRule = styles.slice(rootStart, rootEnd + 2);
  assert.doesNotMatch(rootRule, /grid-template-columns:\s*84px/);
  assert.doesNotMatch(styles, /\.app-shell--omt-v2 \.sidebar\s*\{[^}]*width:\s*84px/);
  assert.doesNotMatch(styles, /\.app-shell--omt-v2 \.material-nav-rail\s*\{[^}]*display:\s*grid/);
  assert.match(styles, /\.app-shell--workspace \.sidebar\s*\{[\s\S]*?width:\s*100px/);
  assert.match(styles, /\.app-shell--workspace \.material-nav-rail\s*\{[\s\S]*?display:\s*flex/);
});

test('signed-in boot gate and account trigger are isolated from transient page layout', async () => {
  const [app, shell, styles, baseStyles] = await Promise.all([appSourcePromise, workspaceShellSourcePromise, omtStylesPromise, baseStylesPromise]);
  const main = await readFile(new URL('./main.js', import.meta.url), 'utf8');
  assert.match(app, /v-if="authSessionState === 'checking'" class="auth-boot-screen"/);
  assert.match(app, /authSessionState\.value === 'checking' \|\| authSessionState\.value === 'unknown'/);
  assert.match(app, /authSessionState\.value === 'signed-out' && workspacePaths\.has\(route\.path\)/);
  assert.match(app, /v-else-if="authSessionState === 'unknown'" class="auth-boot-screen auth-boot-screen--error"/);
  assert.match(app, /@click="loadAuth">重新確認<\/md-button>/);
  assert.doesNotMatch(app, /v-else-if="!signedIn && route\.meta\.requiresAuth"/);
  assert.match(main, /bootstrapAuthSession\(\)/);
  assert.match(main, /__OMT_AUTH_BOOTSTRAP__/);
  assert.match(main, /authBootstrap\.status === 'signed-in'[\s\S]*?nextUrl\.pathname === '\/'/);
  assert.match(main, /rememberWorkspaceRoute\(window\.sessionStorage, nextUrl\.pathname\)/);
  assert.match(main, /navigation\?\.type === 'reload'\) rememberedRoute = rememberedWorkspaceRoute\(window\.sessionStorage\)/);
  assert.match(main, /nextUrl\.pathname = rememberedRoute \|\|/);
  assert.match(app, /rememberWorkspaceRoute\(window\.sessionStorage, path\)/);
  assert.match(main, /authBootstrap\.status === 'signed-out'[\s\S]*?workspacePaths\.has\(nextUrl\.pathname\)/);
  assert.match(app, /hasDefinitiveBootstrap[\s\S]*?await requestJson\('\/auth\/me'\)/);
  assert.match(shell, /class="workspace-account-anchor"/);
  assert.ok(shell.indexOf('class="workspace-account-anchor"') < shell.indexOf('<main class="main-content workspace-main">'));
  assert.doesNotMatch(shell.slice(shell.indexOf('<header class="topbar'), shell.indexOf('</header>')), /workspace-account-anchor/);
  assert.match(styles, /\.app-shell--workspace \.workspace-account-anchor\s*\{[^}]*position:\s*fixed;[^}]*top:\s*calc\(12px \+ env\(safe-area-inset-top, 0px\)\);[^}]*right:\s*calc\(12px \+ env\(safe-area-inset-right, 0px\)\);[^}]*width:\s*44px;[^}]*height:\s*44px/);
  assert.match(shell, /<Transition name="account-popover">[\s\S]*?v-if="accountMenuOpen" class="account-menu-panel"/);
  assert.doesNotMatch(shell, /<md-menu\b/);
  assert.match(styles, /\.app-shell--workspace \.account-menu-panel\s*\{[^}]*position:\s*fixed;[^}]*z-index:\s*1000/);
  assert.match(styles, /\.app-shell--workspace \.workspace-account-anchor\s*\{[^}]*isolation:\s*isolate/);
  assert.doesNotMatch(styles, /\.workspace-account-anchor\s*\{[^}]*contain:\s*(?:layout|paint)/);
  assert.match(baseStyles, /\.auth-boot-screen--error\s*\{[^}]*display:\s*grid/);
  assert.match(styles, /@media \(min-width:\s*901px\)[\s\S]*?\.app-shell--workspace \.brand\s*\{[\s\S]*?width:\s*80px;[\s\S]*?min-width:\s*80px;[\s\S]*?max-width:\s*80px;[\s\S]*?flex:\s*0 0 62px/);
});

test('mobile Calendar detail follows the swipe, resists a second upward pull, and desktop detail responds to aspect ratio', async () => {
  const [calendar, styles] = await Promise.all([calendarSourcePromise, omtStylesPromise]);
  assert.match(calendar, /@pointerdown="beginMobileSheetDrag"/);
  assert.match(calendar, /@pointermove="moveMobileSheetDrag"/);
  assert.match(calendar, /@pointerup="endMobileSheetDrag"/);
  assert.match(calendar, /mobileSheetExpanded\.value && rawDelta <= 0/);
  assert.match(calendar, /mobileSheetExpanded\.value \? 0 : -170/);
  assert.match(calendar, /velocity < -0\.35/);
  assert.match(calendar, /--omt-sheet-drag-offset/);
  assert.match(styles, /transform:\s*translate3d\(0, var\(--omt-sheet-drag-offset, 0px\), 0\)/);
  assert.match(styles, /\.omt-mobile-detail-dialog--dragging:not\(\.omt-mobile-detail-dialog--editor\)\s*\{[^}]*transition:\s*none/);
  assert.match(styles, /@media \(min-width: 901px\) and \(min-aspect-ratio: 7\/5\)/);
  assert.match(styles, /@media \(min-width: 901px\) and \(max-aspect-ratio: 7\/5\)/);
});

test('Task and Event expose only a manual OMT-local bidirectional index', async () => {
  const [app, lists, taskDetail, calendar, eventDetail] = await Promise.all([
    appSourcePromise,
    listViewSourcePromise,
    taskDetailSourcePromise,
    calendarSourcePromise,
    readFile(new URL('./components/CalendarEventDetail.vue', import.meta.url), 'utf8'),
  ]);
  assert.match(app, /async function linkTaskEvent\(payload = \{\}\)[\s\S]*?related_event_id: eventId \|\| null/);
  assert.match(app, /@link-task-event="linkTaskEvent"/);
  assert.match(lists, /:calendar-events="props\.calendarEvents"/);
  assert.match(lists, /@link-event="emit\('link-task-event', \$event\)"/);
  assert.match(taskDetail, />連結行程<\/span>/);
  assert.match(taskDetail, /emit\('search-events', \{ task: props\.task, query \}\)/);
  assert.match(taskDetail, /emit\('link-event', \{ task: props\.task, event_id: event\?\.id \|\| null \}\)/);
  assert.match(app, /request\(`\/events\/search\?\$\{params\.toString\(\)\}`\)/);
  assert.match(calendar, /const selectedEventTasks = computed/);
  assert.match(calendar, /:related-tasks="selectedEventTasks"/);
  assert.match(eventDetail, /<h3 id="related-tasks-heading">相關待辦<\/h3>/);
  assert.doesNotMatch(eventDetail, /行程來源|外部日曆原始資料|個人標籤|整理摘要|行程檢查清單/);
});

test('Settings keeps Calendar actions compact and clearly separated', async () => {
  const settingsCss = await readFile(new URL('../settings.css', import.meta.url), 'utf8');
  assert.match(settingsCss, /\.provider-actions\s*\{[^}]*column-gap:\s*12px/);
  assert.match(settingsCss, /\.provider-actions > \.settings-button\s*\{[^}]*padding-inline:\s*10px/);
  assert.doesNotMatch(settingsCss, /margin-inline-start:\s*36px/);
});



test('September follow-up keeps Todo controls compact, weekday labels aligned, and empty copy in UI type', async () => {
  const [lists, styles] = await Promise.all([listViewSourcePromise, omtStylesPromise]);
  assert.match(styles, /\.app-shell--omt-v2 \.list-picker\s*\{[^}]*width:\s*min\(10rem, 100%\);[^}]*min-width:\s*0/);
  assert.match(styles, /\.app-shell--omt-v2 \.list-management__content\s*\{[^}]*border-block:\s*0/);
  assert.match(styles, /\.app-shell--omt-v2 \.todo-filter\s*\{[^}]*border-bottom:\s*0/);
  assert.match(styles, /\.app-shell--omt-v2 \.omt-empty-state strong\s*\{[^}]*font-family:\s*['\"]Noto Sans TC['\"]/);
  assert.match(lists, /從今天頁面整理出的待辦會顯示在這裡。/);
  assert.doesNotMatch(lists, /從今天頁面的整理流程整理出的待辦會顯示在這裡。/);
  assert.match(styles, /\.app-shell--omt-v2 \.week-labels span\s*\{[^}]*place-items:\s*center/);
});

test('Event editor keeps three desktop schedule fields equal and gives embedded editors usable time width', async () => {
  const editor = await eventEditorSourcePromise;
  assert.match(editor, /\.event-editor-schedule\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(editor, /\.event-editor-embedded \.event-editor-field--date\s*\{[^}]*grid-column:\s*1 \/ -1/);
  assert.doesNotMatch(editor, /event-editor-time-help/);
});

test('Settings exposes real avatar replacement and account deletion actions', async () => {
  const [settings, app] = await Promise.all([settingsSourcePromise, appSourcePromise]);
  assert.match(settings, /type="file"[^>]*accept="image\/png,image\/jpeg,image\/webp"/);
  assert.match(settings, /@click="chooseAvatar"/);
  assert.match(settings, /@click="deleteDialogOpen = true"/);
  assert.match(settings, /永久刪除/);
  assert.doesNotMatch(settings, /頭像更換功能尚未開放|刪除帳戶功能尚未開放/);
  assert.match(app, /async function updateAvatar\(/);
  assert.match(app, /requestJson\('\/account\/avatar'/);
  assert.match(app, /async function deleteAccount\(/);
  assert.match(app, /requestJson\('\/account'/);
  assert.match(app, /@change-avatar="updateAvatar"/);
  assert.match(app, /@delete-account="deleteAccount"/);
});

test('September interaction polish keeps calendar replacement in-place and adjacent-month selection faster', async () => {
  const [calendar, styles] = await Promise.all([calendarSourcePromise, omtStylesPromise]);
  assert.match(calendar, /monthMotionSpeed\.value = 'normal'/);
  assert.match(calendar, /!day\.isCurrentMonth[\s\S]*?monthMotionSpeed\.value = 'fast'/);
  assert.match(calendar, /<Transition :name="`calendar-month-\$\{monthMotion\}\$\{monthMotionSpeed === 'fast' \? '-fast' : ''\}`">/);
  assert.match(styles, /calendar-month-next-leave-active[\s\S]*?position:\s*absolute;[\s\S]*?inset:\s*0/);
  assert.match(styles, /calendar-month-next-fast-enter-active[\s\S]*?transition:\s*opacity 85ms ease, transform 85ms/);
});

test('September interaction polish keeps focus rings tight and mouse button feedback shape-stable', async () => {
  const [styles, lists, editor, review, detail] = await Promise.all([
    omtStylesPromise,
    listViewSourcePromise,
    eventEditorSourcePromise,
    proposalReviewSourcePromise,
    taskDetailSourcePromise,
  ]);
  assert.match(styles, /\.event-editor-control:focus-visible\s*\{[^}]*outline:\s*2px solid[^}]*outline-offset:\s*1px/);
  assert.match(styles, /\.capture-composer-textarea:focus-within\s*\{[^}]*outline:\s*2px solid[^}]*outline-offset:\s*1px/);
  assert.match(styles, /@media \(hover: hover\) and \(pointer: fine\)[\s\S]*?md-button\[pressed\][\s\S]*?border-radius:[^;]*9999px[^;]*![iI]mportant;[\s\S]*?translateY\(1px\) scale\(\.985\)/);
  assert.match(styles, /\.list-management > summary\s*\{[^}]*width:\s*fit-content;[^}]*border-radius:\s*999px;[^}]*user-select:\s*none/);
  assert.match(styles, /\.list-picker:focus-within,[\s\S]*?\.omt-detail-select:focus-within\s*\{[^}]*outline:\s*0;[^}]*box-shadow:\s*inset 0 -2px/);
  assert.match(editor, /class="event-editor-schedule"/);
  assert.match(editor, /event-editor-schedule--all-day/);
  assert.doesNotMatch(review, /套用結果|原始內容|SourceDetailPanel/);
  assert.doesNotMatch(detail, /查看原始內容|重試載入原始內容|<dt>個人清單<\/dt>/);
  assert.match(lists, /<summary>[\s\S]*?<span>清單<\/span>/);
});


test('mobile Event sheet has one hard upper snap point and editor header breathing room', async () => {
  const [calendar, editor, styles] = await Promise.all([calendarSourcePromise, eventEditorSourcePromise, omtStylesPromise]);
  assert.match(calendar, /if \(mobileSheetExpanded\.value && rawDelta <= 0\)[\s\S]*?mobileSheetDragOffset\.value = 0/);
  assert.match(calendar, /const minOffset = mobileSheetExpanded\.value \? 0 : -170/);
  assert.match(editor, /@media \(max-width: 600px\)[\s\S]*?\.event-editor-embedded \{ padding-top: 18px; \}/);
  assert.match(styles, /overscroll-behavior-y:\s*contain/);
});

test('Todo personal-list picker remains compact and keeps comfortable leading inset', async () => {
  const styles = await omtStylesPromise;
  assert.match(styles, /\.app-shell--omt-v2 \.list-picker \{[\s\S]*?max-width:\s*10rem;[\s\S]*?--md-outlined-field-leading-space:\s*18px/);
});

test('public entry tells the product story through four live UI scenes and keeps site icons', async () => {
  const [entry, html] = await Promise.all([publicEntrySourcePromise, indexSourcePromise]);
  assert.match(entry, /class="landing-scene landing-scene--capture"/);
  assert.match(entry, /landing-scene--calendar/);
  assert.match(entry, /landing-scene--tasks/);
  assert.match(entry, /landing-scene--final/);
  assert.match(entry, /samplePrompt = '明天下午 2:20 團隊討論，地點在圖書館討論區，記得帶企劃書。'/);
  assert.match(entry, /typedText\.value = samplePrompt\.slice/);
  assert.match(entry, /確認加入 2 個項目/);
  assert.match(entry, /calendarPhase\.value = 3/);
  assert.match(entry, /taskPhase\.value = 3/);
  assert.match(entry, /Try it/);
  assert.match(entry, /展示帳號僅供參考/);
  assert.doesNotMatch(entry, /基本電學實習/);
  assert.match(entry, /getBoundingClientRect/);
  assert.match(entry, /captureAction/);
  assert.match(entry, /calendarNavAction/);
  assert.match(entry, /taskNavAction/);
  assert.doesNotMatch(entry, /landing-v3-|YOUR WORKSPACE|landing-showcase|landing-demo-glow/);
  assert.match(html, /rel="icon" href="\/favicon\.svg"/);
  assert.match(html, /rel="manifest" href="\/site\.webmanifest"/);
});

test('auth modal uses a controlled surface with no provider-ready status box or internal dialog scroller', async () => {
  const [app, styles] = await Promise.all([appSourcePromise, omtStylesPromise]);
  assert.match(app, /class="auth-modal-layer"/);
  assert.match(app, /class="auth-modal-card" role="dialog" aria-modal="true"/);
  assert.doesNotMatch(app, /v-if="authStatus"/);
  assert.doesNotMatch(app, /auth-provider-hint--status/);
  assert.doesNotMatch(app, /<md-dialog v-if="authModal"/);
  assert.doesNotMatch(app, /請選擇登入方式。|請重新選擇登入方式。/);
  assert.doesNotMatch(app, /class="auth-status-box"/);
  assert.match(styles, /\.auth-modal-layer \{[\s\S]*?overflow-y:\s*auto;[\s\S]*?scrollbar-width:\s*none/);
  assert.match(styles, /\.auth-modal-card \{[\s\S]*?overflow:\s*visible/);
  assert.match(styles, /body:has\(\.auth-modal-layer\) \{[\s\S]*?overflow:\s*hidden/);
});


test('guest sign-in is ordered Google, GitHub, then local demo and disables AI Capture', async () => {
  const [app, today] = await Promise.all([appSourcePromise, todaySourcePromise]);
  const google = app.indexOf('使用 Google 登入');
  const github = app.indexOf('使用 GitHub 登入');
  const guest = app.indexOf('展示帳號</span>');
  assert.ok(google >= 0 && github > google && guest > github);
  assert.match(app, /googleAuthButtonRef[^>]*color="outlined"/);
  assert.match(app, /firstAuthButtonRef[^>]*color="filled"/);
  assert.match(app, /function startGuestSession\(\)[\s\S]*?createGuestWorkspace\(new Date\(\)\)[\s\S]*?window\.sessionStorage/);
  assert.doesNotMatch(app, /展示帳號只使用瀏覽器暫存資料/);
  assert.match(today, /:disabled="props\.guestMode"/);
  assert.match(today, /登入後即可使用 AI 整理文字、圖片與 PDF。展示帳號僅供參考。/);
});

test('Task detail keeps selection stable, offers standalone time, and searches linked events on demand', async () => {
  const [lists, detail, app] = await Promise.all([listViewSourcePromise, taskDetailSourcePromise, appSourcePromise]);
  assert.match(lists, /if \(selectedTaskId\.value === nextId\) return/);
  assert.match(detail, />待辦時間<\/span>/);
  assert.match(detail, /type="date"/);
  assert.match(detail, /type="time"/);
  assert.match(detail, />全天<\/span>/);
  assert.match(detail, /placeholder="搜尋名稱、日期或星期幾"/);
  assert.doesNotMatch(detail, /v-for="event in props\.calendarEvents"/);
  assert.ok(app.includes('request(`/events/search?${params.toString()}`)'));
  assert.match(app, /limit: query \? '12' : '6'/);
  assert.match(app, /today: dateKey\(runtimeNow\.value\)/);
});

test('successful Capture batch apply closes review instead of showing an applied-results page', async () => {
  const app = await appSourcePromise;
  const handler = app.slice(app.indexOf('async function confirmAndApplyProposals'), app.indexOf('async function refreshProposalDomainState'));
  assert.match(handler, /await refreshProposalDomainState\(\)/);
  assert.match(handler, /if \(!failures\) closeProposalModal\(\)/);
});

test('file selection stays instant and upload is an internal step of one organizing action', async () => {
  const [app, composer, attachment, today] = await Promise.all([
    appSourcePromise,
    readFile(new URL('./components/CaptureComposer.vue', import.meta.url), 'utf8'),
    readFile(new URL('./components/AttachmentItem.vue', import.meta.url), 'utf8'),
    todaySourcePromise,
  ]);
  assert.match(app, /async function uploadCaptureAttachment\(attachment\)/);
  assert.match(app, /\/capture\/uploads\?filename=/);
  assert.match(app, /body: attachment\.file/);
  assert.match(app, /async function prepareCaptureAttachments\(\)/);
  assert.doesNotMatch(app, /prepare_status/);
  assert.doesNotMatch(app, /attachmentsPreparing|captureFilesPreparing|filesPreparing/);
  const addFilesBlock = app.slice(app.indexOf('function addFiles(files)'), app.indexOf('function removeAttachment'));
  assert.doesNotMatch(addFilesBlock, /uploadCaptureAttachment\(|fetch\(/);
  const uploadBlock = app.slice(app.indexOf('async function uploadCaptureAttachment'), app.indexOf('function addFiles(files)'));
  assert.doesNotMatch(uploadBlock, /FileReader|readAsDataURL/);
  const submitBlock = app.slice(app.indexOf('async function submitCapture'), app.indexOf('async function acceptProposal'));
  assert.match(submitBlock, /captureStatus\.value = 'processing'[\s\S]*?await prepareCaptureAttachments\(\)[\s\S]*?request\('\/interpret'/);
  assert.match(app, /const incomplete = selected\.find\(\(attachment\) => attachment\?\.file && !attachment\?\.upload_id\)/);
  assert.match(submitBlock, /result\.release !== 'json-object-v26'/);
  assert.match(app, /const captureBusy = computed\(\(\) => captureStatus\.value === 'processing'\)/);
  assert.match(composer, /:disabled="props\.disabled"/);
  assert.match(composer, /整理中…/);
  assert.match(composer, /正在整理資訊…/);
  assert.doesNotMatch(composer, /上傳中|正在上傳|背景準備|準備檔案/);
  assert.doesNotMatch(attachment, /上傳中|準備中/);
  assert.match(attachment, /:disabled="props\.disabled"/);
  assert.match(today, /:submit-disabled="false"/);
});

test('guest mode keeps workspace data local and avoids backend-only refresh paths', async () => {
  const [app, settings] = await Promise.all([appSourcePromise, settingsSourcePromise]);
  assert.match(app, /async function loadAuth\(\)[\s\S]*?if \(guestMode\.value && guestWorkspace\.value\)[\s\S]*?await loadDashboard\(\)/);
  assert.match(app, /function onCalendarAppVisible\(\)[\s\S]*?if \(guestMode\.value \|\| !signedIn\.value/);
  assert.match(settings, /props\.guestMode/);
  assert.match(app, /目前無法載入工作區，請稍後再試。/);
});

test('Task detail shows an explicit ungrouped option and compact standalone-time layout', async () => {
  const [detail, styles] = await Promise.all([taskDetailSourcePromise, demoPolishStylesPromise]);
  assert.match(detail, /value="__none__"/);
  assert.match(detail, /未分組/);
  assert.match(detail, /搜尋名稱、日期或星期幾/);
  assert.match(styles, /grid-template-columns:\s*minmax\(0,\s*1fr\) minmax\(0,\s*1fr\);/);
});

test('workspace reload fallback is written before unload and can restore after auth finishes', async () => {
  const [main, app] = await Promise.all([mainSourcePromise, appSourcePromise]);
  assert.match(main, /window\.addEventListener\('pagehide', rememberReloadTarget\)/);
  assert.match(main, /consumeWorkspaceReloadRoute\(window\.sessionStorage\)/);
  assert.match(app, /route\.path === '\/'[\s\S]*?consumeWorkspaceReloadRoute\(window\.sessionStorage\)[\s\S]*?router\.replace\(reloadRoute \|\| '\/today'\)/);
});


test('Proposal review v25 owns viewport height and never scrolls the footer', async () => {
  const [main, review, styles] = await Promise.all([mainSourcePromise, proposalReviewSourcePromise, reviewScrollV25StylesPromise]);
  assert.match(main, /import '\.\.\/review-scroll-v25\.css';/);
  assert.match(review, /<header class="proposal-review-head">[\s\S]*<div class="proposal-review-body">[\s\S]*<footer class="proposal-review-actions">/);
  assert.match(styles, /grid-template-rows:\s*auto minmax\(0, 1fr\) auto\s*!important/);
  assert.match(styles, /\.proposal-review-card[\s\S]*height:\s*min\(760px, calc\(100dvh - 48px\)\)\s*!important/);
  assert.match(styles, /\.proposal-review-body[\s\S]*overflow-y:\s*auto\s*!important/);
  assert.match(styles, /@media \(max-width: 900px\)[\s\S]*\.proposal-review-card[\s\S]*height:\s*calc\(100dvh - 8px\)\s*!important/);
  assert.match(styles, /\.proposal-review-actions[\s\S]*z-index:\s*5/);
});
