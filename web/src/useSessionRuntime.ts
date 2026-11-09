/**
 * 会话保活：每个会话一份运行时（消息、流、确认/提问、轨迹、滚动与草稿）。
 *
 * 设计取舍（见与用户的约定）：
 *  · 只保活「运行中」的会话，后台最多 3 个；非运行的会话切走即释放，
 *    切回走 /api/history + SSE 接入（A+B 兜底），只记住滚动位置与输入草稿。
 *  · 保活的是状态与 SSE，不是 React 树/DOM —— 隐藏 DOM 的内存和滚动坑更多。
 *  · 终态后关闭流但保留数据，空闲 10 分钟或超上限时释放。
 *  · 后台会话的确认/提问不自动拒绝：继续等待，由侧栏红点 + 顶部横幅提示。
 */
import { useCallback, useMemo, useRef, useState } from "react";
import type { ChatItem, ConfirmInfo, PendingAsk, TraceEvent } from "./types";

export interface SessionRuntime {
  sid: string;
  items: ChatItem[];
  /** 当前正在流式的助手消息 id */
  curAsstId: string | null;
  confirm: ConfirmInfo | null;
  ask: PendingAsk | null;
  traces: TraceEvent[];
  /** 本页是否正在消费该会话的 SSE */
  streaming: boolean;
  /** items 是否已经由 history / snapshot 建好 */
  loaded: boolean;
  /** 本页已收到终态：挡住轮询未清时的重复接入 */
  finished: boolean;
  scrollTop: number;
  draft: string;
  touchedAt: number;
}

/** 非运行会话释放后仍然记住的轻量信息 */
interface Memo {
  scrollTop: number;
  draft: string;
}

export const KEEP_IDLE_MS = 10 * 60 * 1000;
/** 后台运行中的会话最多保活几个（浏览器同域连接有限，SSE 各占一条） */
export const MAX_BACKGROUND = 3;

type Updater = (rt: SessionRuntime) => void;

export function useSessionRuntime(activeSid: string) {
  const mapRef = useRef(new Map<string, SessionRuntime>());
  const memoRef = useRef(new Map<string, Memo>());
  const activeRef = useRef(activeSid);
  activeRef.current = activeSid;
  const [ver, force] = useState(0);
  const bump = useCallback(() => force((n) => n + 1), []);

  const ensure = useCallback((sid: string): SessionRuntime => {
    let rt = mapRef.current.get(sid);
    if (!rt) {
      const memo = memoRef.current.get(sid);
      memoRef.current.delete(sid);
      rt = {
        sid,
        items: [],
        curAsstId: null,
        confirm: null,
        ask: null,
        traces: [],
        streaming: false,
        loaded: false,
        finished: false,
        scrollTop: memo?.scrollTop ?? 0,
        draft: memo?.draft ?? "",
        touchedAt: Date.now(),
      };
      mapRef.current.set(sid, rt);
    }
    return rt;
  }, []);

  /** 改一份运行时；当前会话的改动触发重渲染，后台会话默认不触发 */
  const update = useCallback(
    (sid: string, fn: Updater, forceRender = false) => {
      const rt = ensure(sid);
      fn(rt);
      rt.touchedAt = Date.now();
      if (forceRender || sid === activeRef.current) bump();
    },
    [ensure, bump]
  );

  /** 只记住滚动/草稿，不触发渲染（滚动事件很频繁） */
  const saveScroll = useCallback((sid: string, top: number) => {
    const rt = mapRef.current.get(sid);
    if (rt) rt.scrollTop = top;
    const memo = memoRef.current.get(sid);
    if (memo) memo.scrollTop = top;
  }, []);

  const saveDraft = useCallback((sid: string, text: string) => {
    const rt = mapRef.current.get(sid);
    if (rt) rt.draft = text;
    const memo = memoRef.current.get(sid);
    if (memo) memo.draft = text;
  }, []);

  const scrollOf = useCallback((sid: string) => ensure(sid).scrollTop, [ensure]);
  const draftOf = useCallback((sid: string) => ensure(sid).draft, [ensure]);

  /** 释放运行时：非运行态才允许，滚动/草稿转存到 memo，切回时还能还原 */
  const release = useCallback((sid: string) => {
    const rt = mapRef.current.get(sid);
    if (!rt || rt.streaming) return;
    memoRef.current.set(sid, { scrollTop: rt.scrollTop, draft: rt.draft });
    mapRef.current.delete(sid);
  }, []);

  /** 标记某会话本页是否正在接收实时事件（共享流订阅 / 本页发送） */
  const setStreaming = useCallback(
    (sid: string, on: boolean, forceRender = false) => {
      let rt = mapRef.current.get(sid);
      if (!rt) {
        if (!on) return;
        rt = ensure(sid);
      }
      if (rt.streaming === on) return;
      rt.streaming = on;
      rt.touchedAt = Date.now();
      if (forceRender || sid === activeRef.current) bump();
    },
    [ensure, bump]
  );

  /** 按订阅集合整体同步（共享流只有一条，订阅关系是一份集合而不是每会话一条连接） */
  const syncStreaming = useCallback(
    (subscribed: Set<string>) => {
      let changed = false;
      for (const rt of mapRef.current.values()) {
        const want = subscribed.has(rt.sid);
        if (rt.streaming !== want) {
          rt.streaming = want;
          rt.touchedAt = Date.now();
          changed = true;
        }
      }
      if (changed) bump();
    },
    [bump]
  );

  const isStreaming = useCallback(
    (sid: string) => !!mapRef.current.get(sid)?.streaming,
    []
  );

  const isFinished = useCallback(
    (sid: string) => !!mapRef.current.get(sid)?.finished,
    []
  );

  const has = useCallback((sid: string) => mapRef.current.has(sid), []);

  /** 轮询确认已空闲的会话：清掉「本页刚收过终态」挡板 */
  const clearFinished = useCallback((running: Set<string>) => {
    for (const rt of mapRef.current.values()) {
      if (!running.has(rt.sid)) rt.finished = false;
    }
  }, []);

  /** 清理空闲运行时（订阅关系由 App 控制，这里只负责释放数据） */
  const prune = useCallback(
    (current: string) => {
      const now = Date.now();
      for (const rt of [...mapRef.current.values()]) {
        if (rt.sid === current || rt.streaming) continue;
        if (now - rt.touchedAt > KEEP_IDLE_MS) release(rt.sid);
      }
    },
    [release]
  );

  return useMemo(
    () => ({
      ensure,
      update,
      saveScroll,
      saveDraft,
      scrollOf,
      draftOf,
      release,
      setStreaming,
      syncStreaming,
      isStreaming,
      isFinished,
      has,
      clearFinished,
      prune,
      /** 后台在等用户确认/回答的会话（侧栏红点 + 顶部横幅） */
      attention: [...mapRef.current.values()].filter(
        (rt) => rt.sid !== activeSid && (rt.confirm || rt.ask)
      ),
    }),
    // ver 让后台确认/提问（forceRender）时重新计算 attention
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ensure, update, saveScroll, saveDraft, scrollOf, draftOf, release, setStreaming, syncStreaming, isStreaming, isFinished, has, clearFinished, prune, activeSid, ver]
  );
}
