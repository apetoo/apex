import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Eye, ShieldAlert, ThermometerSun } from "lucide-react";

import {
  getSectorSentimentOverview,
  getSectorSentimentDetail,
  getSectorSentimentValidation,
  type AlertState,
  type SectorSentimentScore,
} from "@/api/sector-sentiment";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/base/card";
import { cn } from "@/lib/utils";

const STATE_META: Record<AlertState, { label: string; className: string }> = {
  normal: { label: "正常", className: "text-flat" },
  observe: { label: "观察", className: "text-amber-600" },
  warning: { label: "警戒", className: "text-down" },
  resolved: { label: "已解除", className: "text-up" },
  insufficient_data: { label: "数据不足", className: "text-flat" },
};

export function SectorSentimentPage() {
  const [taxonomy, setTaxonomy] = useState<"industry" | "concept">("concept");
  const overview = useQuery({ queryKey: ["sector-sentiment", "overview"], queryFn: () => getSectorSentimentOverview() });
  const validation = useQuery({ queryKey: ["sector-sentiment", "validation"], queryFn: getSectorSentimentValidation });
  const sectors = (overview.data?.sectors ?? []).filter((item) => item.taxonomy === taxonomy);

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <ThermometerSun className="h-6 w-6 text-amber-600" />
            <h1 className="font-serif text-3xl font-semibold">情绪预警</h1>
          </div>
          <p className="mt-1 text-sm text-text-secondary">板块共识拥挤与价格背离 · 研究观察，不触发交易</p>
        </div>
        <div className="rounded-md border border-border bg-bg-card px-4 py-2 text-right text-xs text-text-secondary">
          <p>数据覆盖 {(100 * (overview.data?.coverage ?? 0)).toFixed(0)}%</p>
          <p>{validation.data ? `${validation.data.trading_days} / ${validation.data.target_days} 个交易日` : "验证进度加载中"}</p>
        </div>
      </div>

      {overview.data?.data_quality !== "ok" && (
        <div className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-700">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          平台覆盖不足或报告尚未生成；缺失数据不会显示为低风险。
        </div>
      )}

      <div className="flex gap-2">
        {(["concept", "industry"] as const).map((value) => (
          <button key={value} onClick={() => setTaxonomy(value)}
            className={cn("rounded-md border px-3 py-1.5 text-sm", taxonomy === value ? "border-text-primary bg-bg-card" : "border-border text-text-secondary")}>
            {value === "concept" ? "概念板块" : "申万行业"}
          </button>
        ))}
      </div>

      {overview.isLoading ? <p className="text-sm text-text-secondary">加载中…</p> : sectors.length === 0 ? (
        <Card><CardContent className="py-10 text-center text-sm text-text-secondary">暂无该分类的有效情绪数据</CardContent></Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {sectors.map((sector) => <SectorCard key={sector.sector_id} sector={sector} />)}
        </div>
      )}
    </div>
  );
}

function SectorCard({ sector }: { sector: SectorSentimentScore }) {
  const [expanded, setExpanded] = useState(false);
  const detail = useQuery({
    queryKey: ["sector-sentiment", "detail", sector.sector_id],
    queryFn: () => getSectorSentimentDetail(sector.sector_id),
    enabled: expanded,
  });
  const meta = STATE_META[sector.state] ?? STATE_META.normal;
  const Icon = sector.state === "warning" ? ShieldAlert : Eye;
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div><CardTitle>{sector.sector_name}</CardTitle><p className="mt-1 text-xs text-text-secondary">{sector.platforms.join(" · ")} · {sector.independent_authors} 位独立作者</p></div>
          <span className={cn("inline-flex items-center gap-1 text-sm font-medium", meta.className)}><Icon className="h-4 w-4" />{meta.label}</span>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Risk value={sector.short_risk} label="短线" />
          <Risk value={sector.swing_risk} label="波段" />
        </div>
        <div className="grid grid-cols-4 gap-2 border-t border-border pt-3 text-center text-xs text-text-secondary">
          <Metric label="情绪" value={sector.sentiment_extreme} />
          <Metric label="传播" value={sector.attention_acceleration} />
          <Metric label="拥挤" value={sector.consensus_crowding} />
          <Metric label="背离" value={sector.market_divergence} />
        </div>
        <button type="button" onClick={() => setExpanded((value) => !value)}
          aria-label={`查看${sector.sector_name}证据`}
          className="w-full rounded-md border border-border px-3 py-2 text-sm text-text-secondary hover:text-text-primary">
          {expanded ? "收起证据" : "查看趋势与证据"}
        </button>
        {expanded && (
          <div className="space-y-3 border-t border-border pt-3">
            <div>
              <p className="text-xs font-medium">60日风险轨迹</p>
              <div className="mt-2 flex h-12 items-end gap-1">
                {(detail.data?.history ?? []).map((point) => (
                  <div key={point.trade_date} title={`${point.trade_date} 短线${point.short_risk} 波段${point.swing_risk}`}
                    className="min-w-1 flex-1 bg-amber-500/60" style={{ height: `${Math.max(4, point.short_risk)}%` }} />
                ))}
              </div>
            </div>
            <div>
              <p className="text-xs font-medium">平台贡献</p>
              <div className="mt-1 text-xs text-text-secondary">
                {Object.entries(sector.platform_contributions).map(([platform, value]) => (
                  <p key={platform}>{platform} · {value.records}条 · 净情绪{value.net_sentiment.toFixed(2)}</p>
                ))}
              </div>
            </div>
            <div>
              <p className="text-xs font-medium">脱敏证据</p>
              {(detail.data?.sector.evidence ?? sector.evidence).map((item, index) => (
                <blockquote key={`${item.platform}-${index}`} className="mt-1 border-l-2 border-border pl-2 text-xs text-text-secondary">
                  {item.text || "—"}
                </blockquote>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Risk({ label, value }: { label: string; value: number }) {
  return <div className="rounded-md bg-bg-base p-3"><p className="text-xs text-text-secondary">{label}风险</p><p className={cn("num mt-1 text-xl font-semibold", value >= 75 ? "text-down" : value >= 55 ? "text-amber-600" : "text-flat")}>{label} {value.toFixed(0)}</p></div>;
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div><p>{label}</p><p className="num mt-1 text-sm text-text-primary">{(value * 100).toFixed(0)}</p></div>;
}
