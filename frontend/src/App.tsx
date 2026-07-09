import { NavLink, Route, Routes } from "react-router-dom";
import { LayoutDashboard, Wallet, Search, LineChart, ScanSearch, History, Target } from "lucide-react";
import { cn } from "@/lib/utils";
import { ChatPanel } from "@/components/base";
import { ChatContextProvider } from "@/hooks/useChatContext";
import { OverviewPage } from "@/routes/overview/OverviewPage";
import { WatchlistPage } from "@/routes/watchlist/WatchlistPage";
import { AnalyzePage } from "@/routes/analyze/AnalyzePage";
import { JournalPage } from "@/routes/journal/JournalPage";
import { BacktestPage } from "@/routes/backtest/BacktestPage";
import { ScreenerPage } from "@/routes/screener/ScreenerPage";
import { SystemPage } from "@/routes/system/SystemPage";

/**
 * 路由表(ED: 概览首页 + 四 Tab 1:1 平移 + 全局浮动 chat)
 *
 * /                概览(PR3)
 * /watchlist       持仓(PR1b)
 * /analyze         分析(PR2)
 * /journal         分析历史(跨股)
 * /backtest        回测(PR4)
 * /screener        筛选(PR5)
 *
 * ChatContextProvider 包整个 App, 让任意路由页能 setContext(ED2)
 * ChatPanel 全局浮动, 任意路由可呼出
 */
const NAV = [
  { to: "/", label: "概览", icon: LayoutDashboard, end: true },
  { to: "/watchlist", label: "持仓", icon: Wallet, end: false },
  { to: "/analyze", label: "分析", icon: Search, end: false },
  { to: "/journal", label: "历史", icon: History, end: false },
  { to: "/backtest", label: "回测", icon: LineChart, end: false },
  { to: "/screener", label: "筛选", icon: ScanSearch, end: false },
  { to: "/system", label: "我的系统", icon: Target, end: false },
];

function App() {
  return (
    <ChatContextProvider>
      <div className="min-h-screen bg-bg-base">
        <header className="sticky top-0 z-20 border-b border-border bg-bg-card/80 backdrop-blur-md">
          <nav className="mx-auto flex max-w-6xl items-center gap-1 px-6 py-3">
            <span className="mr-6 font-serif text-xl font-semibold tracking-tight">
              apex
            </span>
            {NAV.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    cn(
                      "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors",
                      isActive
                        ? "bg-bg-base font-medium text-text-primary"
                        : "text-text-secondary hover:bg-bg-base hover:text-text-primary",
                    )
                  }
                >
                  <Icon className="h-4 w-4" />
                  {item.label}
                </NavLink>
              );
            })}
          </nav>
        </header>

        <main>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/watchlist" element={<WatchlistPage />} />
            <Route path="/analyze" element={<AnalyzePage />} />
            <Route path="/journal" element={<JournalPage />} />
            <Route
              path="/backtest"
              element={<BacktestPage />}
            />
            <Route
              path="/screener"
              element={<ScreenerPage />}
            />
            <Route path="/system" element={<SystemPage />} />
          </Routes>
        </main>

        <ChatPanel />
      </div>
    </ChatContextProvider>
  );
}

export default App;
