// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors
// @ducktape-generator devinfra/js/debundle/extract/init_region.mjs
// Selected-module lowered region; original owners: owner_00002, owner_00734, owner_01678, owner_01730.

import { ree } from "./atomic_module_0001__ree.js";
import { RH } from "./atomic_module_0381__RH.js";
class Ru {
  static has(t) {
    try {
      return !!window.localStorage.getItem(t);
    } catch (n) {
      return console.warn(`localStorage.getItem error: ${n.message}`), !1;
    }
  }
  static get(t) {
    try {
      const n = window.localStorage.getItem(t);
      return n ? JSON.parse(n) : null;
    } catch (n) {
      return console.warn(`localStorage.getItem error: ${n.message}`), null;
    }
  }
}
let O, T1, Mse, Re, vP, _P, wP, v9e, TP, U8, Er;
export function __dt_generated_init__atomic_module_0002__Er_Mse_O_stage_0() {
  O = (e, t, n) => (ree(e, typeof t != "symbol" ? t + "" : t, n), n);
}
export function __dt_generated_init__atomic_module_0002__Er_Mse_O_stage_1() {
  const __dt_selected_module_snapshot__owner_00734 = (() => {
    const T1 = (e, t = 5) => parseFloat(e.toPrecision(t)),
      Mse = e => T1(e.reduce((t, n) => t + n) / e.length),
      Re = class Re {
        static logChanged(t, n) {
          const i = Re.CHANGED_CACHE[t];
          if (Re.CHANGED_CACHE[t] = n, !i) return;
          const r = new Set([...Object.keys(i), ...Object.keys(n)]),
            s = {};
          for (const o of r) {
            const prev = i[o],
              next = n[o];
            RH(prev, next) || (s[o] = {
              prev,
              next
            });
          }
          Object.keys(s).length > 0 && console.info(`[${t}] changed:`, s);
        }
      };
    return {
      T1,
      Mse,
      Re
    };
  })();
  T1 = __dt_selected_module_snapshot__owner_00734.T1;
  Mse = __dt_selected_module_snapshot__owner_00734.Mse;
  Re = __dt_selected_module_snapshot__owner_00734.Re;
  O(Re, "DEBUG_LOG_TIMES", !0), O(Re, "TIMES_AGGR", {}), O(Re, "TIMES_AVG", {}), O(Re, "LAST_DEBUG_LOG_CALL", 0), O(Re, "DEBUG_LOG_INTERVAL_ID", null), O(Re, "LAST_FRAME_TIMESTAMP", 0), O(Re, "FRAME_COUNT", 0), O(Re, "ANIMATION_FRAME_ID", null), O(Re, "scheduleAnimationFrame", () => {
    Re.DEBUG_LOG_INTERVAL_ID !== null && (Re.ANIMATION_FRAME_ID = requestAnimationFrame(t => {
      Re.LAST_FRAME_TIMESTAMP !== t && (Re.LAST_FRAME_TIMESTAMP = t, Re.FRAME_COUNT++), Re.DEBUG_LOG_INTERVAL_ID !== null && Re.scheduleAnimationFrame();
    }));
  }), O(Re, "setupInterval", () => {
    Re.DEBUG_LOG_INTERVAL_ID === null && (console.info("%c(starting perf recording)", "color: lime"), Re.DEBUG_LOG_INTERVAL_ID = window.setInterval(Re.debugLogger, 1e3), Re.scheduleAnimationFrame()), Re.LAST_DEBUG_LOG_CALL = Date.now();
  }), O(Re, "debugLogger", () => {
    if (Re.DEBUG_LOG_TIMES) {
      for (const [t, {
        t: n,
        times
      }] of Object.entries(Re.TIMES_AGGR)) times.length && (console.info(t, T1(times.reduce((r, s) => r + s)), times.sort((r, s) => r - s).map(r => T1(r))), Re.TIMES_AGGR[t] = {
        t: n,
        times: []
      });
      for (const [t, {
        t: n,
        times,
        avg
      }] of Object.entries(Re.TIMES_AVG)) if (times.length) {
        const s = times.reduce((a, l) => a + l),
          o = T1(s / Re.FRAME_COUNT);
        console.info(t, `- ${times.length} calls - ${o}ms/frame across ${Re.FRAME_COUNT} frames (${T1(o / 16.67 * 100, 1)}% of frame budget)`), Re.TIMES_AVG[t] = {
          t: n,
          times: [],
          avg: avg != null ? Mse([avg, o]) : o
        };
      }
    }
    Re.FRAME_COUNT = 0, Date.now() - Re.LAST_DEBUG_LOG_CALL > 600 && Re.DEBUG_LOG_INTERVAL_ID !== null && (console.info("%c(stopping perf recording)", "color: red"), window.clearInterval(Re.DEBUG_LOG_INTERVAL_ID), window.cancelAnimationFrame(Re.ANIMATION_FRAME_ID), Re.ANIMATION_FRAME_ID = null, Re.FRAME_COUNT = 0, Re.LAST_FRAME_TIMESTAMP = 0, Re.DEBUG_LOG_INTERVAL_ID = null, Re.TIMES_AGGR = {}, Re.TIMES_AVG = {});
  }), O(Re, "logTime", (t, n = "default") => {
    Re.setupInterval();
    const i = performance.now(),
      {
        t: r,
        times
      } = Re.TIMES_AGGR[n] = Re.TIMES_AGGR[n] || {
        t: 0,
        times: []
      };
    r && times.push(t ?? i - r), Re.TIMES_AGGR[n].t = i;
  }), O(Re, "logTimeAverage", (t, n = "default") => {
    Re.setupInterval();
    const i = performance.now(),
      {
        t: r,
        times
      } = Re.TIMES_AVG[n] = Re.TIMES_AVG[n] || {
        t: 0,
        times: []
      };
    r && times.push(t ?? i - r), Re.TIMES_AVG[n].t = i;
  }), O(Re, "logWrapper", t => (n, i = "default") => (...r) => {
    const s = performance.now(),
      o = n(...r);
    return Re[t](performance.now() - s, i), o;
  }), O(Re, "logTimeWrap", Re.logWrapper("logTime")), O(Re, "logTimeAverageWrap", Re.logWrapper("logTimeAverage")), O(Re, "perfWrap", (t, n = "default") => (...i) => {
    console.time(n);
    const r = t(...i);
    return console.timeEnd(n), r;
  }), O(Re, "CHANGED_CACHE", {});
}
export function __dt_generated_init__atomic_module_0002__Er_Mse_O_stage_2() {
  O(Ru, "set", (t, n) => {
    try {
      return window.localStorage.setItem(t, JSON.stringify(n)), !0;
    } catch (i) {
      return console.warn(`localStorage.setItem error: ${i.message}`), !1;
    }
  }), O(Ru, "delete", t => {
    try {
      window.localStorage.removeItem(t);
    } catch (n) {
      console.warn(`localStorage.removeItem error: ${n.message}`);
    }
  });
}
export function __dt_generated_init__atomic_module_0002__Er_Mse_O_stage_3() {
  const __dt_selected_module_snapshot__owner_01730 = (() => {
    const vP = e => {
        const t = Array.from(e.values());
        return {
          x: wP(t, n => n.x) / t.length,
          y: wP(t, n => n.y) / t.length
        };
      },
      _P = ([e, t]) => Math.hypot(e.x - t.x, e.y - t.y),
      wP = (e, t) => e.reduce((n, i) => n + t(i), 0),
      v9e = 8,
      TP = 99999,
      U8 = e => v9e / e,
      Er = class Er {};
    return {
      vP,
      _P,
      wP,
      v9e,
      TP,
      U8,
      Er
    };
  })();
  vP = __dt_selected_module_snapshot__owner_01730.vP;
  _P = __dt_selected_module_snapshot__owner_01730._P;
  wP = __dt_selected_module_snapshot__owner_01730.wP;
  v9e = __dt_selected_module_snapshot__owner_01730.v9e;
  TP = __dt_selected_module_snapshot__owner_01730.TP;
  U8 = __dt_selected_module_snapshot__owner_01730.U8;
  Er = __dt_selected_module_snapshot__owner_01730.Er;
  O(Er, "referenceSnapPoints", null), O(Er, "visibleGaps", null), O(Er, "setReferenceSnapPoints", t => {
    Er.referenceSnapPoints = t;
  }), O(Er, "getReferenceSnapPoints", () => Er.referenceSnapPoints), O(Er, "setVisibleGaps", t => {
    Er.visibleGaps = t;
  }), O(Er, "getVisibleGaps", () => Er.visibleGaps), O(Er, "destroy", () => {
    Er.referenceSnapPoints = null, Er.visibleGaps = null;
  });
}
export { O, Re, Ru, vP, _P, TP, U8, Er };
