"""Live smoke over real stdio transport. Needs network and SEC_EDGAR_USER_AGENT.
Run: SEC_EDGAR_USER_AGENT="my-app you@example.com" python tests/smoke_live.py [tool ...]"""
import asyncio, json, os, sys
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

CALLS = [
    ("fs_find_company", {"query": "nvidia"}),
    ("fs_list_filings", {"company": "AAPL", "forms": ["10-K"], "limit": 2}),
    ("fs_financials", {"company": "AAPL", "metric": "revenue", "frequency": "quarterly", "periods": 5}),
    ("fs_company_snapshot", {"company": "MSFT"}),
    ("fs_compare", {"companies": ["AAPL", "MSFT", "GOOGL"]}),
    ("fs_read_filing_section", {"company": "AAPL", "item": "1A", "max_chars": 600}),
    ("fs_search_filing", {"company": "NVDA", "query": "export controls China", "limit": 2}),
    ("fs_insider_trades", {"company": "NVDA", "days": 120, "limit": 4}),
    ("fs_price_history", {"symbol": "AAPL", "range": "1y"}),
    ("fs_dcf", {"fcf_base": 100e9, "growth_rates": [0.08, 0.07, 0.06, 0.05, 0.04], "terminal_growth": 0.025, "discount_rate": 0.09, "net_debt": 0, "shares_outstanding": 15e9}),
]
async def main():
    only = sys.argv[1:] 
    t = StdioTransport(command=sys.executable, args=["-m", "fin_sets.server"], env=dict(os.environ))
    async with Client(t) as c:
        tools = await c.list_tools()
        print("TOOLS", len(tools), [x.name for x in tools])
        for name, args in CALLS:
            if only and name not in only: continue
            r = await c.call_tool(name, args, raise_on_error=False)
            d = r.data if r.data is not None else r.content
            s = json.dumps(d, default=str)
            print(f"\n== {name} err={r.is_error} len={len(s)}\n{s[:1400]}")
asyncio.run(main())
