import { createContext, useContext, useState, useCallback, useMemo, useRef, type ReactNode } from "react";

/**
 * ChatContextProvider — 全局 chat 上下文注入(ED2 修正)
 *
 * **不把上下文 prepend 到每条 message**(会导致 history 膨胀顶到 24000 token budget)。
 * 改为: 上下文作为**会话级单次注入**——只在会话首轮或上下文变化时拼一次到 message;
 * 后续轮次不重复注入(后端 history 已含首轮上下文, AI 可回看)。
 *
 * 用法:
 *   - 路由组件在 effect 中 setContext(本路由的上下文摘要)
 *   - ChatPanel 发消息时 prepend 上下文(仅当 contextChangedAt > lastInjectedAt)
 *   - 路由切换时 contextChangedAt 更新, 下次发消息重新注入
 */

export interface ChatContextValue {
  /** 当前路由的上下文文本(由路由 setContext 设置) */
  context: string;
  /** 设置上下文(路由切换/数据变化时调用) */
  setContext: (text: string) => void;
  /** 拼接 user message: 若上次注入的 context hash 不同, prepend 上下文 */
  buildMessage: (userText: string) => string;
  /** 上次注入的 context hash(用于判断是否变化) */
  lastInjectedHash: string | null;
}

const Ctx = createContext<ChatContextValue | null>(null);

function hash(s: string): string {
  // 简单 hash: 长度 + 头尾字符(避免引入 crypto, dev mock 足够)
  let h = s.length;
  for (let i = 0; i < Math.min(s.length, 16); i++) h = h * 31 + s.charCodeAt(i);
  return String(h);
}

export function ChatContextProvider({ children }: { children: ReactNode }) {
  const [context, setContextRaw] = useState<string>("");
  const [lastInjectedHash, setLastInjectedHash] = useState<string | null>(null);
  // ref 同步追踪 hash, 让连续 buildMessage 调用(同一渲染周期)能立即看到上次 hash
  const lastInjectedRef = useRef<string | null>(null);

  const setContext = useCallback((text: string) => {
    setContextRaw(text);
  }, []);

  const buildMessage = useCallback(
    (userText: string): string => {
      const h = hash(context);
      // 用 ref 同步比较, 避免连续调用时 setState 异步导致的重复 prepend
      if (h !== lastInjectedRef.current) {
        lastInjectedRef.current = h;
        setLastInjectedHash(h); // 总是记录: 反映"是否调用过 buildMessage"
        return context
          ? `[当前上下文]\n${context}\n\n[用户问题]\n${userText}`
          : userText;
      }
      return userText;
    },
    [context],
  );

  const value = useMemo(
    () => ({ context, setContext, buildMessage, lastInjectedHash }),
    [context, setContext, buildMessage, lastInjectedHash],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useChatContext(): ChatContextValue {
  const v = useContext(Ctx);
  if (!v) {
    throw new Error("useChatContext must be used inside ChatContextProvider");
  }
  return v;
}
