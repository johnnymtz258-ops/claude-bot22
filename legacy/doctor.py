import os, asyncio, aiohttp, ssl, certifi
from pathlib import Path
from dotenv import load_dotenv
from live_execution import LiveExecutor

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/".env")

async def get(s,u,h=None,p=None):
    try:
        async with s.get(u,headers=h,params=p,timeout=12) as r:
            return r.status
    except:
        return 0

async def rpc_health(s,url):
    try:
        async with s.post(url,json={"jsonrpc":"2.0","id":1,"method":"getHealth"},timeout=12) as r:
            return r.status
    except:
        return 0

async def main():
    print("\n=== v16.2 QUALITY MEASUREMENT self-check ===")
    ssl_ctx=ssl.create_default_context(cafile=certifi.where())
    connector=aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as s:
        print("DexScreener:", await get(s,"https://api.dexscreener.com/token-profiles/latest/v1"))
        print("GeckoTerminal:", await get(s,"https://api.geckoterminal.com/api/v2/networks/new_pools",
                                          {"Accept":"application/json;version=20230203"}))
        print("RugCheck:", await get(s,"https://api.rugcheck.xyz/v1/tokens/So11111111111111111111111111111111111111112/report/summary"))

        be=os.getenv("BIRDEYE_API_KEY","").strip()
        if be:
            st=await get(s,"https://public-api.birdeye.so/defi/networks",
                         {"X-API-KEY":be,"accept":"application/json"})
            print("Birdeye core:",st, "(200 = key valid; advanced features are fault-isolated at runtime)")
        else:
            print("Birdeye: not configured (RugCheck fallback active)")

        helius=os.getenv("HELIUS_API_KEY","").strip()
        if helius:
            rpc=f"https://mainnet.helius-rpc.com/?api-key={helius}"
            print("Realtime Solana RPC:", await rpc_health(s,rpc), "(Helius)")
        else:
            rpc=os.getenv("SOLANA_RPC_HTTP","https://api.mainnet-beta.solana.com")
            print("Realtime Solana RPC:", await rpc_health(s,rpc), "(public fallback)")

        jup=os.getenv("JUPITER_API_KEY","").strip()
        if jup:
            jst=await get(s,"https://api.jup.ag/tokens/v2/recent",{"x-api-key":jup,"accept":"application/json"})
            print("Jupiter Tokens V2:",jst,"(discovery + independent safety intel)")
        else:
            print("Jupiter Tokens V2: API key not configured (optional; execution quote fallback still available)")
        print("X:","configured" if os.getenv("X_BEARER_TOKEN","").strip() else "not configured")
        print("PumpPortal discovery:","configured (runtime WebSocket will verify connection)" if os.getenv("PUMPPORTAL_API_KEY","").strip() else "not configured (optional)")
        public_wallet=os.getenv("PUBLIC_SOLANA_WALLET_ADDRESS","").strip()
        if public_wallet:
            try:
                async with s.post(rpc,json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[public_wallet,{"commitment":"confirmed"}]},timeout=12) as r:
                    data=await r.json(content_type=None)
                    ok=r.status==200 and isinstance(data,dict) and not data.get("error")
                    print("Read-only wallet sync:","READY" if ok else f"RPC check failed ({r.status})")
            except Exception:
                print("Read-only wallet sync: network/RPC error")
        else:
            print("Read-only wallet sync: not configured (optional)")
        ex=LiveExecutor()
        issues=ex.readiness()
        print("Real autopilot capability:", "READY (still requires Telegram arm/confirm)" if not issues else "OFF/not ready")
        tg=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
        chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
        if tg and chat:
            try:
                async with s.post(
                    f"https://api.telegram.org/bot{tg}/sendMessage",
                    json={"chat_id":chat,"text":"✅ v16.2 QUALITY MEASUREMENT self-check: Telegram connected."},
                    timeout=12
                ) as r:
                    print("Telegram:",r.status)
            except:
                print("Telegram: network error")
        else:
            print("Telegram: not configured")
    print("=====================\n")

asyncio.run(main())
