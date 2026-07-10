import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  AreaSeries,
  LineSeries,
  HistogramSeries,
  LineStyle,
} from "lightweight-charts";
import { getIntradayBars, type IntradayBars } from "@/api/market";
import { cn } from "@/lib/utils";

/**
 * <IntradayChart> — 当日分时图(A 股 1 分钟 K 线)
 *
 * 数据源: /api/market/intraday/{ts_code}/bars (后端 get_intraday_bars, akshare 1min 不复权)
 *
 * 雪球风分时呈现:
 *   - 主线: 当日 1 分钟收盘价 area 线, 颜色按「最新价 vs 昨收」红涨绿跌
 *   - 昨收基准虚线(prev_close)
 *   - VWAP 叠加线(累计 amount/累计 vol, 浅灰)
 *   - 下方成交量柱(红绿按当分钟 close vs open)
 *   - 时间轴只标 9:30 / 11:30 / 13:00 / 15:00
 *
 * 时间处理: akshare 返回北京时间 "YYYY-MM-DD HH:MM:SS"。
 * lightweight-charts 的 UTCTimestamp 按 UTC 读, 直接喂会偏 8 小时。
 * 这里把北京时间戳 +8h 偏移(让数值代表"北京时间但 chart 当 UTC 渲染"),
 * 再用 tickMarkFormatter 把标签写回北京时间(HH:MM), 双管齐下显示正确。
 */
export interface IntradayChartProps {
  tsCode: string;
  /** YYYYMMDD；缺省=当日(后端取 akshare 最新一天) */
  tradeDate?: string;
  /** 数据到位回调, 供卡片头取最新价/涨跌幅(避免父层重复拉一次 bars) */
  onDataLoaded?: (data: IntradayBars) => void;
  /** 图表高度(px), 默认 320; 看板可传 220 更紧凑 */
  height?: number;
  className?: string;
}

// 红涨绿跌 token(与 index.css 一致)
const UP = "#e14b4b";
const DOWN = "#2ba84a";
const FLAT = "#9b9b9b";
const VWAP = "#b8a35a"; // 浅金, 不抢主线
const VOL_UP = "rgba(225, 75, 75, 0.4)";
const VOL_DOWN = "rgba(43, 168, 74, 0.4)";

/**北京时间 "YYYY-MM-DD HH:MM:SS" → chart 用 UTCTimestamp(偏移 +8h 让显示对) */
function bjTimeToTs(s: string): number {
  // 北京时间 "YYYY-MM-DD HH:MM:SS" 直接当 UTC 解析(不偏移): ts 的 HH:MM == 北京 HH:MM。
  // 时区无关 -- fmtTickLabel 用 getUTCHours 还原, 不依赖运行机器本地时区
  // (旧逻辑假设机器在北京时区, 非北京机器刻度偏移显示成 UTC 时间)。
  const [d, t] = s.split(" ");
  const [Y, M, D] = d.split("-").map(Number);
  const [h, m] = (t || "00:00:00").split(":").map(Number);
  return Date.UTC(Y, M - 1, D, h, m, 0) / 1000;
}

/** 把时间戳(已偏移) 格式化成北京时间 HH:MM 给轴标签 */
function fmtTickLabel(ts: number): string {
  // ts 是"北京当 UTC"秒; getUTCHours 直接还原北京 HH:MM
  const d = new Date(ts * 1000);
  const h = String(d.getUTCHours()).padStart(2, "0");
  const m = String(d.getUTCMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

function buildSeriesData(data: IntradayBars) {
  const bars = data.bars;
  const prevClose = data.prev_close;

  // 主线: 收盘价 area
  const priceData = bars.map((b) => ({
    time: bjTimeToTs(b.time) as never,
    value: b.close,
  }));

  // VWAP: 累计 amount / 累计 vol
  const vwapData: { time: never; value: number }[] = [];
  let cumAmt = 0;
  let cumVol = 0;
  for (const b of bars) {
    cumAmt += b.amount || 0;
    cumVol += b.vol || 0;
    if (cumVol > 0) {
      vwapData.push({ time: bjTimeToTs(b.time) as never, value: cumAmt / cumVol });
    }
  }

  // 成交量柱: 红绿按当分钟 close vs open
  const volData = bars.map((b) => ({
    time: bjTimeToTs(b.time) as never,
    value: b.vol,
    color: b.close >= b.open ? VOL_UP : VOL_DOWN,
  }));

  // 最新价 vs 昨收 定主线色
  const last = bars[bars.length - 1]?.close;
  const dir = prevClose != null && last != null ? last - prevClose : 0;
  const lineColor = dir > 0 ? UP : dir < 0 ? DOWN : FLAT;

  return { priceData, vwapData, volData, lineColor, prevClose };
}

export function IntradayChart({ tsCode, tradeDate, onDataLoaded, height, className }: IntradayChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceSeriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const vwapSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const volSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const loadRef = useRef<{ code: string; data: IntradayBars } | null>(null);

  // 拉数据 + 喂给 chart
  useEffect(() => {
    let cancelled = false;
    getIntradayBars(tsCode, tradeDate)
      .then((data) => {
        if (cancelled) return;
        loadRef.current = { code: tsCode, data };
        applyData(data);
        onDataLoaded?.(data);
      })
      .catch(() => {
        // fail-soft: 留空图
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tsCode, tradeDate]);

  function applyData(data: IntradayBars) {
    if (!chartRef.current) return;
    const { priceData, vwapData, volData, lineColor, prevClose } = buildSeriesData(data);

    if (priceSeriesRef.current) {
      priceSeriesRef.current.applyOptions({
        lineColor,
        topColor: lineColor + "33", // 20% 透明
        bottomColor: lineColor + "05",
      });
      priceSeriesRef.current.setData(priceData);
    }
    vwapSeriesRef.current?.setData(vwapData);
    volSeriesRef.current?.setData(volData);

    // 昨收基准虚线(price series 上画一条 priceLine)
    if (priceSeriesRef.current && prevClose != null) {
      priceSeriesRef.current.createPriceLine({
        price: prevClose,
        color: FLAT,
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
        axisLabelVisible: true,
        title: "昨收",
      });
    }

    chartRef.current.timeScale().fitContent();
  }

  // 建图(仅一次)
  useEffect(() => {
    if (!containerRef.current || chartRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#ffffff" },
        textColor: "#6b6b6b",
        fontFamily:
          'ui-monospace, SFMono-Regular, Menlo, Monaco, "Roboto Mono", monospace',
      },
      grid: {
        vertLines: { color: "#f5f5f0" },
        horzLines: { color: "#f5f5f0" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#9b9b9b", labelBackgroundColor: "#6b6b6b" },
        horzLine: { color: "#9b9b9b", labelBackgroundColor: "#6b6b6b" },
      },
      rightPriceScale: {
        borderColor: "#ecece6",
        scaleMargins: { top: 0.08, bottom: 0.28 },
      },
      timeScale: {
        borderColor: "#ecece6",
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (ts: number) => fmtTickLabel(ts),
      },
      width: containerRef.current.clientWidth,
      height: height ?? 320,
    });
    chartRef.current = chart;

    priceSeriesRef.current = chart.addSeries(AreaSeries, {
      lineColor: FLAT,
      lineWidth: 2,
      topColor: FLAT + "33",
      bottomColor: FLAT + "05",
      priceLineVisible: false,
      lastValueVisible: true,
    });
    vwapSeriesRef.current = chart.addSeries(LineSeries, {
      color: VWAP,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    volSeriesRef.current = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    // 成交量独立缩放区间(底部 28%)
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
    });

    // 喂已拉到的数据(建图晚于数据到达的情况)
    if (loadRef.current) applyData(loadRef.current.data);

    // 响应容器宽度变化
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) chart.applyOptions({ width: w });
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      priceSeriesRef.current = null;
      vwapSeriesRef.current = null;
      volSeriesRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className={cn("w-full", className)}>
      <div ref={containerRef} className="w-full" />
    </div>
  );
}
