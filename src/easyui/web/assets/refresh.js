// Keeping the page fresh without waste: ask the server for its revision every
// minute, but only while the tab is visible and the person has done something
// in the last ten minutes. Otherwise the page says it is paused, and checks
// again as soon as they come back.

import { api } from "./api.js";

const ACTIVITY_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll", "focus"];

/**
 * startRefresh({rev, onChange(rev), onState(state)}) -> stop()
 * state is one of "live", "checking", "paused", "offline".
 */
export function startRefresh({ rev, onChange, onState, interval = 60_000, idle = 600_000 }) {
  let known = rev;
  let lastActivity = Date.now();
  let lastCheck = Date.now();
  let timer = null;
  let state = null;
  let busy = false;
  let stopped = false;

  const set = (s) => {
    if (s !== state) {
      state = s;
      onState(s);
    }
  };

  const awake = () => !document.hidden && Date.now() - lastActivity < idle;

  async function check() {
    if (busy || stopped) return;
    busy = true;
    lastCheck = Date.now();
    try {
      const data = await api.rev();
      if (stopped) return;
      set("live");
      if (data && typeof data.rev === "string" && data.rev !== known) {
        known = data.rev;
        await onChange(known);
      }
    } catch (err) {
      if (!stopped && !(err && err.status === 401)) set("offline");
    } finally {
      busy = false;
    }
  }

  function schedule() {
    clearTimeout(timer);
    if (stopped) return;
    if (!awake()) {
      set("paused");
      return; // resumes on activity or when the tab is shown again
    }
    if (state === "paused") set("live");
    const wait = Math.max(0, interval - (Date.now() - lastCheck));
    timer = setTimeout(async () => {
      if (awake()) await check();
      schedule();
    }, wait);
  }

  let throttle = 0;
  function onActivity() {
    const now = Date.now();
    if (now - throttle < 1000) return;
    throttle = now;
    const wasPaused = state === "paused";
    lastActivity = now;
    if (wasPaused) {
      set("live");
      if (now - lastCheck >= interval) check().then(schedule);
      else schedule();
    }
  }

  function onVisibility() {
    if (document.hidden) {
      clearTimeout(timer);
      set("paused");
    } else {
      lastActivity = Date.now();
      set("live");
      if (Date.now() - lastCheck >= interval) check().then(schedule);
      else schedule();
    }
  }

  for (const type of ACTIVITY_EVENTS) window.addEventListener(type, onActivity, { passive: true, capture: true });
  document.addEventListener("visibilitychange", onVisibility);
  set(document.hidden ? "paused" : "live");
  schedule();

  return function stop() {
    stopped = true;
    clearTimeout(timer);
    for (const type of ACTIVITY_EVENTS) window.removeEventListener(type, onActivity, { capture: true });
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
