import { useEffect, useState } from "react";

/**
 * useLocalStorage - 把 state 同步到 localStorage 的轻量 hook。
 *
 * 接口与 useState 完全一致(支持函数式更新), 区别仅在于:
 *   - 初始值优先从 localStorage 读; 读不到/解析失败回退到 initialValue
 *   - 每次 state 变化同步写回 localStorage(best-effort: quota 超限/隐私模式静默)
 *
 * 用途: chat 历史等需要「刷新/重开浏览器仍保留」的本地态。
 * 不做跨 tab 同步(单用户本地工具, YAGNI)。
 */
export function useLocalStorage<T>(
  key: string,
  initialValue: T,
): [T, (value: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw == null) return initialValue;
      return JSON.parse(raw) as T;
    } catch {
      // 脏数据/隐私模式禁用 localStorage -> 回退 initialValue
      return initialValue;
    }
  });

  // 写回: value 变化即同步。mount 时也会写一次(无害: 把读到的值/初始值落盘)。
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // quota 超限 / 隐私模式 -- 持久化是 best-effort, 静默
    }
  }, [key, value]);

  return [value, setValue];
}
