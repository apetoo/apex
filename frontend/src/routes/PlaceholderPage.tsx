import { Construction } from "lucide-react";
import { Card, CardContent } from "@/components/base";

/**
 * 占位页: PR1a 只立路由骨架, 后续 PR 填充。
 * 每个路由对应一个 Streamlit tab(1:1 平移)。
 */
export function PlaceholderPage({ title, pr }: { title: string; pr: string }) {
  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <h1 className="font-serif text-3xl font-semibold tracking-tight">
        {title}
      </h1>
      <Card className="mt-6">
        <CardContent className="flex flex-col items-center justify-center gap-3 py-16">
          <Construction className="h-8 w-8 text-flat" />
          <p className="text-sm text-text-secondary">
            {pr} 实现 · 待开发
          </p>
          <p className="text-xs text-flat">
            PR1a 地基已就绪, 此路由待对应 PR 填充
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
