import asyncio, json, os, sys
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
CALLS=[("fs_signal_digest",{"days":2,"top":4}),("fs_signals",{"category":"insider","min_strength":60,"limit":4}),("fs_ticker_signals",{"ticker":"NVDA","days":30}),
       ("fs_signal_catalog",{}),("fs_signal_backtest",{"kind":"breakout_52w_high","horizon_days":5,"min_strength":50})]
async def main():
    t=StdioTransport(command=sys.executable,args=["-m","fin_sets.server"],env={**os.environ,"SEC_EDGAR_DB":sys.argv[1]})
    async with Client(t) as c:
        print("TOOLS",len(await c.list_tools()))
        for n,a in CALLS:
            r=await c.call_tool(n,a,raise_on_error=False); s=json.dumps(r.data if r.data is not None else r.content,default=str)
            print(f"\n== {n} err={r.is_error} len={len(s)}\n{s[:900]}")
asyncio.run(main())
