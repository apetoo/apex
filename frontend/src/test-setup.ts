import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// Node 24 暴露了需要 --localstorage-file 的实验性 global localStorage。
// 部分 24.x + Vitest worker 组合下它会抢在 jsdom 之前被复制到 window，
// 形成没有 clear/getItem 的假 Storage。测试统一使用完整的内存实现。
const storageData = new Map<string, string>();
const memoryStorage: Storage = {
  get length() {
    return storageData.size;
  },
  clear() {
    storageData.clear();
  },
  getItem(key) {
    return storageData.get(String(key)) ?? null;
  },
  key(index) {
    return Array.from(storageData.keys())[index] ?? null;
  },
  removeItem(key) {
    storageData.delete(String(key));
  },
  setItem(key, value) {
    storageData.set(String(key), String(value));
  },
};

vi.stubGlobal("localStorage", memoryStorage);
