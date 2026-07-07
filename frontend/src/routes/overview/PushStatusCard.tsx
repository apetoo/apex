import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Send, RefreshCw } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle } from "@/components/base";
import { getPushStatus, triggerFullPush, type PushResult } from "@/api/push";
import { qk } from "@/api/query-keys";
import { cn } from "@/lib/utils";

/**
 * 持仓推送状态卡（概览页底部）
 *
 * 展示推送开关 / 接收地址 / 末次推送结果 / seq，并提供「全量推送」手动触发。
 * 增量推送由后端持仓变更时自动发，这里只触发全量对账。
 */
export function PushStatusCard() {
  const status = useQuery({
    queryKey: qk.pushStatus,
    queryFn: getPushStatus,
    refetchInterval: 15000,
  });

  const [resultMsg, setResultMsg] = useState<string | null>(null);
  const [resultOk, setResultOk] = useState<boolean | null>(null);

  const full = useMutation({
    mutationFn: triggerFullPush,
    onSuccess: (r: PushResult) => {
      if (r.status === "success") {
        setResultOk(true);
        setResultMsg(
          `推送成功 · HTTP ${r.http_status} · ${r.attempts} 次 · ${r.latency_ms ?? "?"}ms`,
        );
      } else {
        setResultOk(false);
        setResultMsg(`推送失败 · ${r.error ?? r.status}`);
      }
      void status.refetch();
    },
    onError: (e: Error) => {
      setResultOk(false);
      setResultMsg(`请求失败 · ${e.message}`);
    },
  });

  const s = status.data;
  const enabled = s?.enabled ?? false;
  const baseUrl = s?.base_url || "未配置";

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-normal text-text-secondary">
          <Send className="mr-1 inline h-3.5 w-3.5" />
          持仓推送
        </CardTitle>
        <span
          className={cn(
            "text-[10px]",
            enabled ? "text-up" : "text-flat",
          )}
        >
          {enabled ? "● 已启用" : "○ 未启用"}
        </span>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1 text-xs text-text-secondary">
          <div className="flex justify-between gap-2">
            <span className="shrink-0">接收地址</span>
            <span
              className="num truncate max-w-[260px] text-text-primary"
              title={baseUrl}
            >
              {baseUrl}
            </span>
          </div>
          <div className="flex justify-between">
            <span>末次推送</span>
            <span className="num text-text-primary">
              {s?.last_push_at
                ? `${s.last_push_at} · ${s.last_status ?? "-"}`
                : "—"}
            </span>
          </div>
          <div className="flex justify-between">
            <span>序列号</span>
            <span className="num text-text-primary">{s?.last_seq ?? 0}</span>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            disabled={full.isPending || !enabled}
            onClick={() => {
              setResultMsg(null);
              full.mutate();
            }}
          >
            {full.isPending ? (
              <>
                <RefreshCw className="mr-1 h-3.5 w-3.5 animate-spin" />
                推送中…
              </>
            ) : (
              <>
                <Send className="mr-1 h-3.5 w-3.5" />
                全量推送
              </>
            )}
          </Button>
          {resultMsg && (
            <span
              className={cn(
                "text-[11px]",
                resultOk ? "text-up" : "text-down",
              )}
            >
              {resultMsg}
            </span>
          )}
        </div>

        {!enabled && (
          <p className="text-[10px] text-flat">
            config.yaml 的 <code>push.enabled</code> 为 false 或 base_url 未配置。
          </p>
        )}
      </CardContent>
    </Card>
  );
}
