import akshare as ak
import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from backtesting.test import SMA


class SmaCross(Strategy):
    n1 = 10
    n2 = 20

    def init(self):
        self.sma1 = self.I(SMA, self.data.Close, self.n1)
        self.sma2 = self.I(SMA, self.data.Close, self.n2)

    def next(self):
        if crossover(self.sma1, self.sma2):
            self.buy()
        elif crossover(self.sma2, self.sma1):
            self.sell()


# === 获取A股数据（平安银行举例）===
df = ak.stock_zh_a_hist(symbol="000001", period="daily", start_date="20260101")

# === 字段适配 ===
df = df.rename(columns={
    "开盘": "Open",
    "收盘": "Close",
    "最高": "High",
    "最低": "Low",
    "成交量": "Volume"
})

df.index = pd.to_datetime(df["日期"])
df = df[["Open", "High", "Low", "Close", "Volume"]]

# === 回测 ===
bt = Backtest(df, SmaCross, cash=1000000, commission=0.001)

stats = bt.run()
print(stats)

bt.plot()