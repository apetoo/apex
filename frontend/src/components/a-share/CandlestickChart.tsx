import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  CandlestickSeries,
  LineSeries,
  LineStyle,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesPrimitive,
  type ISeriesMarkersPluginApi,
  type IPrimitivePaneView,
  type IPrimitivePaneRenderer,
  type SeriesAttachedParameter,
  type Time,
} from "lightweight-charts";
import { CHAN_BSP_LABEL, type ChanStructure } from "@/api/chan";

/**
 * <CandlestickChart> - 缠论 K 线 + 笔/中枢/买卖点叠加（/chan 页）
 *
 * lightweight-charts v5。三层叠加：
 *   1. CandlestickSeries - 不复权日线/分钟 K 线（A 股红涨绿跌）
 *   2. 笔 - 每笔一条 LineSeries（红=向上/绿=向下，虚线=未确认）
 *   3. 中枢 - ISeriesPrimitive 自绘矩形（已确认灰、延伸中金、虚线边）
 *   4. 买卖点 - createSeriesMarkers（一/二/三 买卖点箭头 + 标签）
 *
 * repaint 披露：最后一笔 confirmed=false 会随行情重画，前端虚线呈现。
 */

const ZS_CONFIRMED_FILL = "rgba(120, 120, 130, 0.08)";
const ZS_CONFIRMED_STROKE = "rgba(120, 120, 130, 0.55)";
const ZS_EXTENDING_FILL = "rgba(184, 163, 90, 0.10)";
const ZS_EXTENDING_STROKE = "rgba(184, 163, 90, 0.6)";

function aShareChartColors() {
  const styles = getComputedStyle(document.documentElement);
  return {
    up: styles.getPropertyValue("--color-up").trim(),
    down: styles.getPropertyValue("--color-down").trim(),
  };
}

/** "YYYY-MM-DD" | "YYYY-MM-DD HH:MM" -> UTCTimestamp（当 UTC 渲染，日级只看日期） */
function dtToTime(dt: string): Time {
  const [d, t] = dt.split(" ");
  const [Y, M, D] = d.split("-").map(Number);
  const [h, m] = (t || "00:00").split(":").map(Number);
  return (Date.UTC(Y, M - 1, D, h, m, 0) / 1000) as Time;
}

// ── 中枢矩形 ISeriesPrimitive ────────────────────────────────────────────────

export interface ZsRect {
  sdt: Time;
  edt: Time;
  zg: number;
  zd: number;
  state: "confirmed" | "extending";
}

export class ZsOverlay implements ISeriesPrimitive {
  private _series?: ISeriesApi<"Candlestick">;
  private _chart?: IChartApi;
  private _requestUpdate?: () => void;
  private _rects: ZsRect[] = [];

  attached(p: SeriesAttachedParameter) {
    this._chart = p.chart;
    this._series = p.series as ISeriesApi<"Candlestick">;
    this._requestUpdate = p.requestUpdate;
  }
  detached() {
    this._series = undefined;
    this._chart = undefined;
  }
  setRects(rects: ZsRect[]) {
    this._rects = rects;
    this._requestUpdate?.();
  }
  /** 测试用：当前持有的矩形（无 canvas 依赖） */
  getRects(): readonly ZsRect[] {
    return this._rects;
  }
  updateAllViews() {}

  private _paneView: IPrimitivePaneView = { renderer: () => this._renderer };
  paneViews(): readonly IPrimitivePaneView[] {
    return [this._paneView];
  }

  private _renderer: IPrimitivePaneRenderer = {
    draw: (target) => {
      if (!this._chart || !this._series || this._rects.length === 0) return;
      target.useBitmapCoordinateSpace((scope) => {
        const ctx = scope.context;
        const ts = this._chart!.timeScale();
        const px = scope.horizontalPixelRatio;
        const py = scope.verticalPixelRatio;
        for (const r of this._rects) {
          const x1 = ts.timeToCoordinate(r.sdt);
          const x2 = ts.timeToCoordinate(r.edt);
          const y1 = this._series!.priceToCoordinate(r.zg);
          const y2 = this._series!.priceToCoordinate(r.zd);
          if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
          const extending = r.state === "extending";
          ctx.fillStyle = extending
            ? ZS_EXTENDING_FILL
            : ZS_CONFIRMED_FILL;
          ctx.strokeStyle = extending
            ? ZS_EXTENDING_STROKE
            : ZS_CONFIRMED_STROKE;
          ctx.lineWidth = 1 * py;
          ctx.setLineDash(extending ? [4 * px, 3 * px] : []);
          const x = x1 * px;
          const w = Math.max((x2 - x1) * px, 2 * px);
          const yy = Math.min(y1, y2) * py;
          const h = Math.max(Math.abs(y1 - y2) * py, 2 * py);
          ctx.fillRect(x, yy, w, h);
          ctx.strokeRect(x, yy, w, h);
        }
      });
    },
  };
}

// ── 组件 ─────────────────────────────────────────────────────────────────────

export interface CandlestickChartProps {
  structure: ChanStructure;
  height?: number;
  className?: string;
}

export function CandlestickChart({
  structure,
  height = 480,
  className,
}: CandlestickChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const biSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const zsOverlayRef = useRef<ZsOverlay | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // 建图（一次性）
  useEffect(() => {
    if (!containerRef.current) return;
    const { up, down } = aShareChartColors();
    const chart = createChart(containerRef.current, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#6b6b6b",
        fontFamily: "var(--font-mono)",
      },
      grid: {
        vertLines: { color: "rgba(0,0,0,0.04)" },
        horzLines: { color: "rgba(0,0,0,0.04)" },
      },
      rightPriceScale: { borderColor: "#ecece6" },
      timeScale: {
        borderColor: "#ecece6",
        timeVisible: structure.freq === "30" || structure.freq === "60",
        secondsVisible: false,
      },
      crosshair: { mode: CrosshairMode.Normal },
    });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
      borderVisible: false,
    });
    const overlay = new ZsOverlay();
    candle.attachPrimitive(overlay);
    chartRef.current = chart;
    candleRef.current = candle;
    zsOverlayRef.current = overlay;
    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      biSeriesRef.current = [];
      zsOverlayRef.current = null;
      markersRef.current = null;
    };
  }, [height, structure.freq]);

  // 数据更新
  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    const overlay = zsOverlayRef.current;
    if (!chart || !candle || !overlay) return;
    const { up, down } = aShareChartColors();

    // 1. K 线
    candle.setData(
      structure.bars.map((b) => ({
        time: dtToTime(b.dt),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );

    // 2. 笔：清旧建新（每笔一条 LineSeries）
    for (const s of biSeriesRef.current) {
      try {
        chart.removeSeries(s);
      } catch {
        /* 已随 chart.remove 清掉 */
      }
    }
    biSeriesRef.current = [];
    for (const bi of structure.bi_list) {
      const line = chart.addSeries(LineSeries, {
        color: bi.direction === "up" ? up : down,
        lineWidth: 2,
        lineStyle: bi.confirmed ? LineStyle.Solid : LineStyle.Dashed,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      line.setData([
        { time: dtToTime(bi.sdt), value: bi.direction === "up" ? bi.low : bi.high },
        { time: dtToTime(bi.edt), value: bi.direction === "up" ? bi.high : bi.low },
      ]);
      biSeriesRef.current.push(line);
    }

    // 3. 中枢矩形
    overlay.setRects(
      structure.zs_list.map((z) => ({
        sdt: dtToTime(z.sdt),
        edt: dtToTime(z.edt),
        zg: z.zg,
        zd: z.zd,
        state: z.state,
      })),
    );

    // 4. 买卖点 markers
    const markers = structure.bsp_list.map((b) => {
      const isBuy = b.type.endsWith("buy");
      return {
        time: dtToTime(b.dt),
        position: (isBuy ? "belowBar" : "aboveBar") as "belowBar" | "aboveBar",
        color: isBuy ? up : down,
        shape: (isBuy ? "arrowUp" : "arrowDown") as "arrowUp" | "arrowDown",
        text: CHAN_BSP_LABEL[b.type] ?? b.type,
      };
    });
    if (markersRef.current) {
      markersRef.current.setMarkers(markers);
    } else {
      markersRef.current = createSeriesMarkers(candle, markers);
    }

    chart.timeScale().fitContent();
  }, [structure]);

  return <div ref={containerRef} className={className} style={{ height }} />;
}
