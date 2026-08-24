import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AnalyzeTraceStream } from "../AnalyzeTraceStream";

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    events: [{
      event: "trace",
      data: { type: "status", stage: "researching", message: "正在补证", current: 2, total: 3 },
    }],
    status: "streaming",
    error: null,
    result: null,
    connect: vi.fn(),
    abort: vi.fn(),
    reset: vi.fn(),
  }),
}));

describe("AnalyzeTraceStream", () => {
  it("shows the latest business stage and round progress while trace is collapsed", () => {
    render(<AnalyzeTraceStream tsCode="603893.SH" />);

    expect(screen.getByText(/正在补证 · 2\/3/)).toBeTruthy();
    expect(screen.queryByText("工具结果")).toBeNull();
  });
});
