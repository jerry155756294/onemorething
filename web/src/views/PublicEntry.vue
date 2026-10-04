<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

const props = defineProps({
  authSessionState: { type: String, default: 'signed-out' },
  openAuthModal: { type: Function, required: true },
});

const sceneEls = ref([]);
const activeScene = ref(0);
const typedText = ref('');
const capturePhase = ref('idle');
const calendarPhase = ref(0);
const taskPhase = ref(0);
const captureShell = ref(null);
const captureAction = ref(null);
const calendarShell = ref(null);
const calendarNavAction = ref(null);
const calendarDayAction = ref(null);
const calendarAgendaAction = ref(null);
const taskShell = ref(null);
const taskNavAction = ref(null);
const taskRowAction = ref(null);
const taskDetailAction = ref(null);
const capturePointerStyle = ref({});
const calendarPointerStyle = ref({});
const taskPointerStyle = ref({});

const demoNow = new Date();
const demoEventDate = new Date(demoNow.getFullYear(), demoNow.getMonth(), demoNow.getDate() + 1, 14, 20);
const demoEventEnd = new Date(demoEventDate.getFullYear(), demoEventDate.getMonth(), demoEventDate.getDate(), 15, 10);
const demoDateLabel = new Intl.DateTimeFormat('zh-Hant-TW', { month: 'long', day: 'numeric' }).format(demoEventDate);
const demoWeekdayLabel = new Intl.DateTimeFormat('zh-Hant-TW', { weekday: 'long' }).format(demoEventDate);
const demoMonthLabel = new Intl.DateTimeFormat('zh-Hant-TW', { year: 'numeric', month: 'long' }).format(demoEventDate);
const demoTimeLabel = `${String(demoEventDate.getHours()).padStart(2, '0')}:${String(demoEventDate.getMinutes()).padStart(2, '0')}–${String(demoEventEnd.getHours()).padStart(2, '0')}:${String(demoEventEnd.getMinutes()).padStart(2, '0')}`;
const samplePrompt = '明天下午 2:20 團隊討論，地點在圖書館討論區，記得帶企劃書。';

const calendarCells = computed(() => {
  const weekday = demoEventDate.getDay();
  const offsetFromMonday = (weekday + 6) % 7;
  const start = new Date(demoEventDate.getFullYear(), demoEventDate.getMonth(), demoEventDate.getDate() - offsetFromMonday);
  return Array.from({ length: 14 }, (_, index) => {
    const value = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index);
    return { day: value.getDate(), selected: value.toDateString() === demoEventDate.toDateString() };
  });
});

let observer = null;
let timers = [];

function trackScene(el, index) {
  if (el) sceneEls.value[index] = el;
}
function setCalendarDayAction(el, selected) {
  if (selected) calendarDayAction.value = el || null;
}
function clearTimers() {
  timers.forEach((timer) => window.clearTimeout(timer));
  timers = [];
}
function later(callback, delay) {
  const timer = window.setTimeout(callback, delay);
  timers.push(timer);
  return timer;
}
function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}
function pointerStyle(shell, target) {
  if (!shell || !target) return { opacity: 0 };
  const shellRect = shell.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  // The cursor tip should land on the control itself. Previous versions used
  // viewport-ish offsets, so the animation drifted whenever responsive layout
  // moved the button. Anchor every move to the live target rectangle instead.
  const x = targetRect.left - shellRect.left + targetRect.width * .5;
  const y = targetRect.top - shellRect.top + targetRect.height * .5;
  return {
    left: `${Math.max(10, Math.min(shellRect.width - 24, x))}px`,
    top: `${Math.max(10, Math.min(shellRect.height - 24, y))}px`,
    opacity: 1,
  };
}
function updatePointerPositions() {
  capturePointerStyle.value = pointerStyle(captureShell.value, captureAction.value);
  const calendarTarget = calendarPhase.value >= 3 ? calendarAgendaAction.value : calendarPhase.value >= 2 ? calendarDayAction.value : calendarNavAction.value;
  calendarPointerStyle.value = pointerStyle(calendarShell.value, calendarTarget);
  const taskTarget = taskPhase.value >= 3 ? taskDetailAction.value : taskPhase.value >= 2 ? taskRowAction.value : taskNavAction.value;
  taskPointerStyle.value = pointerStyle(taskShell.value, taskTarget);
}
async function syncPointers() {
  await nextTick();
  window.requestAnimationFrame(updatePointerPositions);
}
function runCaptureDemo() {
  clearTimers();
  typedText.value = '';
  capturePhase.value = 'typing';
  if (prefersReducedMotion()) {
    typedText.value = samplePrompt;
    capturePhase.value = 'done';
    void syncPointers();
    return;
  }
  let index = 0;
  const typeNext = () => {
    if (activeScene.value !== 0) return;
    typedText.value = samplePrompt.slice(0, index + 1);
    index += 1;
    if (index < samplePrompt.length) later(typeNext, 34 + (index % 4) * 7);
    else {
      capturePhase.value = 'ready';
      void syncPointers();
      later(() => { if (activeScene.value === 0) { capturePhase.value = 'click'; void syncPointers(); } }, 460);
      later(() => { if (activeScene.value === 0) { capturePhase.value = 'done'; void syncPointers(); } }, 840);
    }
  };
  later(typeNext, 300);
}
function runCalendarDemo() {
  clearTimers();
  calendarPhase.value = prefersReducedMotion() ? 3 : 0;
  void syncPointers();
  if (prefersReducedMotion()) return;
  later(() => { if (activeScene.value === 1) { calendarPhase.value = 1; void syncPointers(); } }, 360);
  later(() => { if (activeScene.value === 1) { calendarPhase.value = 2; void syncPointers(); } }, 940);
  later(() => { if (activeScene.value === 1) { calendarPhase.value = 3; void syncPointers(); } }, 1560);
}
function runTaskDemo() {
  clearTimers();
  taskPhase.value = prefersReducedMotion() ? 3 : 0;
  void syncPointers();
  if (prefersReducedMotion()) return;
  later(() => { if (activeScene.value === 2) { taskPhase.value = 1; void syncPointers(); } }, 340);
  later(() => { if (activeScene.value === 2) { taskPhase.value = 2; void syncPointers(); } }, 920);
  later(() => { if (activeScene.value === 2) { taskPhase.value = 3; void syncPointers(); } }, 1500);
}
watch(activeScene, (scene) => {
  if (scene === 0) runCaptureDemo();
  else if (scene === 1) runCalendarDemo();
  else if (scene === 2) runTaskDemo();
  else clearTimers();
});

onMounted(async () => {
  await nextTick();
  observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    const index = Number(visible.target.dataset.scene || 0);
    if (Number.isFinite(index)) activeScene.value = index;
  }, { threshold: [0.45, 0.62, 0.78] });
  sceneEls.value.forEach((el) => el && observer.observe(el));
  window.addEventListener('resize', updatePointerPositions, { passive: true });
  runCaptureDemo();
  void syncPointers();
});
onBeforeUnmount(() => {
  observer?.disconnect();
  clearTimers();
  window.removeEventListener('resize', updatePointerPositions);
});
</script>

<template>
  <section class="landing-story" aria-label="One More Thing 產品介紹">
    <header class="landing-story-nav">
      <a href="#demo-capture" class="landing-story-brand" aria-label="One More Thing 首頁">
        <span class="landing-story-brand-mark" aria-hidden="true">✳</span>
        <span>one more thing</span>
      </a>
      <md-button color="filled" size="small" @click="props.openAuthModal">登入</md-button>
    </header>

    <main>
      <section id="demo-capture" :ref="(el) => trackScene(el, 0)" data-scene="0" class="landing-scene landing-scene--capture" :class="`phase-${capturePhase}`">
        <div class="landing-scene-copy landing-scene-copy--intro">
          <span>從一段資訊開始</span>
          <h1>貼進來。<br />先看懂，再加入。</h1>
          <p>文字、網址、圖片或 PDF 都可以。AI 只整理候選結果，最後由你確認。</p>
        </div>
        <div ref="captureShell" class="landing-demo-shell landing-demo-shell--today" aria-label="動畫範例：輸入資訊並整理">
          <div class="landing-demo-rail" aria-hidden="true">
            <b>✳</b><span class="is-active"><AppIcon name="home" />今天</span><span><AppIcon name="calendar_month" />行事曆</span><span><AppIcon name="check_box" />待辦</span>
          </div>
          <div class="landing-demo-main">
            <header><strong>{{ demoDateLabel }}</strong><small>{{ demoWeekdayLabel }}</small></header>
            <div class="landing-demo-capture">
              <label>有什麼要記的？</label>
              <div class="landing-demo-input" :class="{ 'is-typing': capturePhase === 'typing' }"><span>{{ typedText }}</span><i v-if="capturePhase === 'typing'" aria-hidden="true"></i></div>
              <button ref="captureAction" type="button" :class="{ 'is-pressed': capturePhase === 'click' }">整理</button>
            </div>
            <div class="landing-demo-review" :class="{ 'is-visible': capturePhase === 'done' }">
              <div><small>待確認</small><strong>團隊討論</strong><span>{{ demoDateLabel }} · {{ demoTimeLabel }}</span></div>
              <div><small>待辦</small><strong>帶企劃書</strong><span>{{ demoDateLabel }}</span></div>
              <b>確認加入 2 個項目</b>
            </div>
          </div>
          <div class="landing-demo-pointer landing-demo-pointer--capture" :class="{ 'is-clicking': capturePhase === 'click' }" :style="capturePointerStyle" aria-hidden="true"></div>
        </div>
        <div class="landing-story-scroll-hint" aria-hidden="true"><span>往下看</span><AppIcon name="expand_more" /></div>
      </section>

      <section :ref="(el) => trackScene(el, 1)" data-scene="1" class="landing-scene landing-scene--workspace landing-scene--calendar" :class="`phase-${calendarPhase}`">
        <div ref="calendarShell" class="landing-demo-shell landing-demo-shell--workspace" aria-hidden="true">
          <div class="landing-demo-rail">
            <b>✳</b><span><AppIcon name="home" />今天</span><button ref="calendarNavAction" type="button" class="landing-demo-rail-action" :class="{ 'is-active': calendarPhase >= 1 }"><AppIcon name="calendar_month" />行事曆</button><span><AppIcon name="check_box" />待辦</span>
          </div>
          <div class="landing-calendar-ui">
            <header><h2>行事曆</h2><div><span>{{ demoMonthLabel }}</span><button>今天</button><b>‹</b><b>›</b></div></header>
            <div class="landing-calendar-week"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
            <div class="landing-calendar-grid">
              <button v-for="(cell, index) in calendarCells" :key="index" :ref="(el) => setCalendarDayAction(el, cell.selected)" type="button" :class="{ 'is-picked': cell.selected && calendarPhase >= 2 }">{{ cell.day }}</button>
            </div>
            <button ref="calendarAgendaAction" type="button" class="landing-calendar-agenda" :class="{ 'is-revealed': calendarPhase >= 3 }"><strong>{{ demoDateLabel }}</strong><div><time>{{ demoTimeLabel }}</time><b>團隊討論</b><span>圖書館討論區</span></div></button>
          </div>
          <div class="landing-demo-pointer landing-demo-pointer--calendar" :class="{ 'is-clicking': calendarPhase === 1 || calendarPhase === 2 }" :style="calendarPointerStyle" aria-hidden="true"></div>
        </div>
        <div class="landing-scene-narrative" :class="calendarPhase <= 1 ? 'landing-scene-narrative--right' : 'landing-scene-narrative--left'">
          <small>01 · 行事曆</small>
          <h2>時間，落到真正的位置。</h2>
          <p>日期、時間和地點都清楚放進行程，需要時還能再改。</p>
        </div>
      </section>

      <section :ref="(el) => trackScene(el, 2)" data-scene="2" class="landing-scene landing-scene--workspace landing-scene--tasks" :class="`phase-${taskPhase}`">
        <div ref="taskShell" class="landing-demo-shell landing-demo-shell--workspace" aria-hidden="true">
          <div class="landing-demo-rail">
            <b>✳</b><span><AppIcon name="home" />今天</span><span><AppIcon name="calendar_month" />行事曆</span><button ref="taskNavAction" type="button" class="landing-demo-rail-action" :class="{ 'is-active': taskPhase >= 1 }"><AppIcon name="check_box" />待辦</button>
          </div>
          <div class="landing-task-ui">
            <header><h2>待辦</h2><small>2 件未完成</small></header>
            <nav><b>全部</b><span>今天</span><span>已完成</span></nav>
            <button ref="taskRowAction" type="button" class="landing-task-row" :class="{ 'is-picked': taskPhase >= 2 }"><i></i><strong>帶企劃書</strong><small>今天</small></button>
            <button type="button" class="landing-task-row"><i></i><strong>整理討論筆記</strong><small>今天</small></button>
            <aside ref="taskDetailAction" class="landing-task-detail" :class="{ 'is-revealed': taskPhase >= 3 }"><small>待辦</small><h3>帶企劃書</h3><label>待辦時間</label><div>{{ demoDateLabel }} · 18:30</div><label>所屬清單</label><div>學習</div></aside>
          </div>
          <div class="landing-demo-pointer landing-demo-pointer--tasks" :class="{ 'is-clicking': taskPhase === 1 || taskPhase === 2 }" :style="taskPointerStyle" aria-hidden="true"></div>
        </div>
        <div class="landing-scene-narrative" :class="taskPhase >= 3 ? 'landing-scene-narrative--left' : 'landing-scene-narrative--right'">
          <small>02 · 待辦</small>
          <h2>要做的事，留成可以完成的一件事。</h2>
          <p>待辦可以有自己的時間，也可以在需要時連結到某個行程。</p>
        </div>
      </section>

      <section :ref="(el) => trackScene(el, 3)" data-scene="3" class="landing-scene landing-scene--final">
        <div>
          <span>one more thing</span>
          <h2>少抄一次。<br />多留一點腦袋給真正要做的事。</h2>
          <p>想先看看也可以，展示帳號僅供參考。</p>
          <div class="landing-final-actions">
            <md-button color="filled" size="medium" @click="props.openAuthModal">Try it</md-button>
            <small v-if="props.authSessionState === 'unknown'">目前無法確認登入狀態；開啟登入時會重新檢查。</small>
          </div>
        </div>
      </section>
    </main>
  </section>
</template>
