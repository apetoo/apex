import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Eye, ShieldAlert, ThermometerSun } from "lucide-react";

import {
  getSectorSentimentOverview,
  getSectorSentimentDetail,
  getSectorSentimentCreators,
  getSectorSentimentValidation,
  moderateSectorSentimentCreator,
  type AlertState,
  type CreatorStatus,
  type FinanceCreator,
  type RetrievalFunnel,
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
  const [view, setView] = useState<"alerts" | "creators">("alerts");
  const [creatorStatus, setCreatorStatus] = useState<CreatorStatus>("candidate");
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

      <div className="flex gap-2" aria-label="情绪预警视图">
        {(["alerts", "creators"] as const).map((value) => (
          <button key={value} type="button" aria-pressed={view === value} onClick={() => setView(value)}
            className={cn("rounded-md border px-3 py-1.5 text-sm", view === value ? "border-text-primary bg-bg-card" : "border-border text-text-secondary")}>
            {value === "alerts" ? "预警" : "作者池"}
          </button>
        ))}
      </div>

      {view === "alerts" && <>
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
      </>}

      {view === "creators" && <CreatorPool status={creatorStatus} onStatusChange={setCreatorStatus} />}
    </div>
  );
}

function CreatorPool({ status, onStatusChange }: { status: CreatorStatus; onStatusChange: (status: CreatorStatus) => void }) {
  const queryClient = useQueryClient();
  const creators = useQuery({
    queryKey: ["sector-sentiment", "creators", status],
    queryFn: () => getSectorSentimentCreators(status),
  });
  const moderation = useMutation({
    mutationFn: ({ creator, action }: { creator: FinanceCreator; action: "approve" | "reject" | "restore" }) =>
      moderateSectorSentimentCreator(creator.platform, creator.creator_id, action),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sector-sentiment", "creators"] }),
  });
  const error = moderation.error instanceof Error ? moderation.error.message : moderation.error ? "操作失败，请重试" : null;
  const emptyLabel: Record<CreatorStatus, string> = {
    candidate: "暂无候选作者",
    approved: "暂无已批准作者",
    rejected: "暂无已拒绝作者",
  };

  return (
    <section aria-label="作者池" className="space-y-4">
      <div className="flex gap-2" aria-label="作者状态">
        {(["candidate", "approved", "rejected"] as const).map((value) => (
          <button key={value} type="button" aria-pressed={status === value} onClick={() => onStatusChange(value)}
            className={cn("rounded-md border px-3 py-1.5 text-sm", status === value ? "border-text-primary bg-bg-card" : "border-border text-text-secondary")}>
            {{ candidate: "候选", approved: "已批准", rejected: "已拒绝" }[value]}
          </button>
        ))}
      </div>
      {error && <p role="alert" className="rounded-md border border-down/30 bg-down/5 p-3 text-sm text-down">{error}</p>}
      {creators.isLoading ? <p className="text-sm text-text-secondary">加载中…</p> : (creators.data?.creators ?? []).length === 0 ? (
        <Card><CardContent className="py-10 text-center text-sm text-text-secondary">{emptyLabel[status]}</CardContent></Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {(creators.data?.creators ?? []).map((creator) => (
            <Card key={`${creator.platform}-${creator.creator_id}`}>
              <CardContent className="space-y-3 pt-5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-medium">{creator.display_name || "未公开显示名"}</p>
                    <p className="mt-1 text-xs text-text-secondary">{creator.platform} · 财经内容 {(creator.financial_ratio * 100).toFixed(0)}% · 有效内容 {creator.valid_content_count}</p>
                  </div>
                  <span className="text-xs text-text-secondary">{creator.status === "candidate" ? "候选" : creator.status === "approved" ? "已批准" : "已拒绝"}</span>
                </div>
                <p className="text-xs text-text-secondary">板块：{creator.sector_ids.length ? creator.sector_ids.join(" · ") : "暂无"}</p>
                <p className="text-xs text-text-secondary">最近发现：{creator.last_discovered_at || "暂无"}</p>
                {creator.evidence[0]?.text && <blockquote className="border-l-2 border-border pl-2 text-xs text-text-secondary">{creator.evidence[0].text}</blockquote>}
                {creator.last_collection_error && <p role="alert" className="text-xs text-down">采集异常：{creator.last_collection_error}</p>}
                <CreatorControls creator={creator} pending={moderation.isPending} onModerate={(action) => moderation.mutate({ creator, action })} />
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

function CreatorControls({ creator, pending, onModerate }: { creator: FinanceCreator; pending: boolean; onModerate: (action: "approve" | "reject" | "restore") => void }) {
  const name = creator.display_name || "该作者";
  const action = (value: "approve" | "reject" | "restore", label: string) => (
    <button type="button" disabled={pending} onClick={() => onModerate(value)} aria-label={label}
      className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary disabled:opacity-50">{label.replace(name, "")}</button>
  );
  if (creator.status === "candidate") return <div className="flex gap-2">{action("approve", `批准${name}`)}{action("reject", `拒绝${name}`)}</div>;
  if (creator.status === "approved") return <div className="flex gap-2">{action("reject", `拒绝${name}`)}</div>;
  return <div className="flex gap-2">{action("restore", `恢复${name}为候选`)}</div>;
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
            <RetrievalFunnelSummary funnel={detail.data?.retrieval_funnel} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RetrievalFunnelSummary({ funnel }: { funnel: RetrievalFunnel | undefined }) {
  if (!funnel) return <p className="text-xs text-text-secondary">暂无检索漏斗数据</p>;
  return (
    <div>
      <p className="text-xs font-medium">检索漏斗</p>
      <div className="mt-1 grid grid-cols-2 gap-1 text-xs text-text-secondary">
        <p>原始召回 {funnel.raw_recalled}</p><p>金融相关 {funnel.financial_relevant}</p>
        <p>已过滤 {funnel.filtered}</p><p>搜索来源 {funnel.search_sources}</p>
        <p>作者来源 {funnel.creator_sources}</p>
      </div>
    </div>
  );
}

function Risk({ label, value }: { label: string; value: number }) {
  return <div className="rounded-md bg-bg-base p-3"><p className="text-xs text-text-secondary">{label}风险</p><p className={cn("num mt-1 text-xl font-semibold", value >= 75 ? "text-down" : value >= 55 ? "text-amber-600" : "text-flat")}>{label} {value.toFixed(0)}</p></div>;
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div><p>{label}</p><p className="num mt-1 text-sm text-text-primary">{(value * 100).toFixed(0)}</p></div>;
}
