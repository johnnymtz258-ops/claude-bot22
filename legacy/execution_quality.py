"""Read-only Jupiter execution and token-quality probes for v15.

The module intentionally has no wallet signing or transaction submission capability.
For Swap V2 it can request an *unsigned* /order only when a public taker address is
configured; the returned transaction is ignored.  Without a public taker it falls
back to the legacy Metis /swap/v1/quote endpoint strictly for route measurement.
Token V2 is used as an independent safety/discovery data source when a Jupiter API
key is configured.
"""
from __future__ import annotations

import os
import time

WSOL_MINT = "So11111111111111111111111111111111111111112"


def _f(v, default=0.0):
    try: return float(default if v is None else v)
    except Exception: return float(default)


def _looks_like_pubkey(v):
    v=str(v or "").strip()
    return 32 <= len(v) <= 50 and v.isalnum()


class JupiterExecutionProbe:
    def __init__(self):
        self.api_key=os.getenv("JUPITER_API_KEY","").strip()
        self.v2_base=os.getenv("JUPITER_SWAP_V2_BASE","https://api.jup.ag/swap/v2").rstrip("/")
        # Compatibility fallback is quote-only. It never builds or submits a transaction.
        self.legacy_base=os.getenv("JUPITER_LEGACY_QUOTE_BASE","https://api.jup.ag/swap/v1").rstrip("/")
        self.legacy_fallback=os.getenv("JUPITER_LEGACY_FALLBACK_BASE","https://lite-api.jup.ag/swap/v1").rstrip("/")
        self.tokens_base=os.getenv("JUPITER_TOKENS_BASE","https://api.jup.ag/tokens/v2").rstrip("/")
        self.taker=(os.getenv("PUBLIC_SOLANA_WALLET_ADDRESS","").strip()
                    or os.getenv("JUPITER_PREFLIGHT_TAKER","").strip())
        self.probe_sol=max(0.005,_f(os.getenv("JUPITER_PROBE_SOL",0.03)))
        self.frontend_bps=max(0.0,_f(os.getenv("PHANTOM_FEE_BPS_PER_SIDE",85)))
        self.max_total_bps=max(150.0,_f(os.getenv("EXECUTION_MAX_TOTAL_FRICTION_BPS",400)))
        self.cache_seconds=max(30.0,_f(os.getenv("EXECUTION_QUOTE_CACHE_SECONDS",90)))
        self.token_cache_seconds=max(60.0,_f(os.getenv("JUPITER_TOKEN_CACHE_SECONDS",600)))
        self.cache={}; self.token_cache={}; self.last_ok=0.0; self.last_error=""; self.last_token_ok=0.0

    def status(self):
        if self.last_ok:
            mode="Swap V2" if _looks_like_pubkey(self.taker) else "quote compatibility"
            return f"OK {mode} ({int(max(0,time.time()-self.last_ok))}s ago)"
        if self.last_error:
            return f"DEGRADED ({self.last_error[:80]})"
        return "UNTESTED"

    def _headers(self):
        h={"accept":"application/json"}
        if self.api_key: h["x-api-key"]=self.api_key
        return h

    async def _v2_order(self,http,input_mint,output_mint,amount):
        """GET an unsigned Swap V2 order. Never execute it and never return tx bytes."""
        if not _looks_like_pubkey(self.taker):
            return 0,{}
        status,data=await http.get(f"{self.v2_base}/order",headers=self._headers(),params={
            "inputMint":input_mint,"outputMint":output_mint,"amount":str(int(amount)),
            "taker":self.taker,
        },retries=1)
        if isinstance(data,dict):
            # Defensive: discard transaction material immediately. Only quote metrics
            # are allowed to leave this method.
            data=dict(data); data.pop("transaction",None); data.pop("requestId",None)
        return status,data

    async def _legacy_quote(self,http,base,input_mint,output_mint,amount):
        status,data=await http.get(f"{base}/quote",headers=self._headers(),params={
            "inputMint":input_mint,"outputMint":output_mint,"amount":str(int(amount)),
            "slippageBps":"100","restrictIntermediateTokens":"true"
        },retries=1)
        return status,data

    async def _one_quote(self,http,input_mint,output_mint,amount):
        # Prefer current Swap V2 whenever a PUBLIC taker address is available.  This
        # improves route realism but does not execute: /execute is never called.
        if _looks_like_pubkey(self.taker):
            st,data=await self._v2_order(http,input_mint,output_mint,amount)
            if st==200 and isinstance(data,dict) and data.get("outAmount"):
                return st,data,"swap-v2-order-readonly"
        last=(0,{})
        for base in [self.legacy_base,self.legacy_fallback]:
            if not base: continue
            st,data=await self._legacy_quote(http,base,input_mint,output_mint,amount)
            last=(st,data)
            if st==200 and isinstance(data,dict) and data.get("outAmount"):
                return st,data,"legacy-v1-quote"
        return last[0],last[1],"unavailable"

    async def token_intel(self,http,token):
        """Read Jupiter Tokens V2 metadata. Suspicion is a veto; score is context only."""
        token=str(token or "").strip(); now=time.time()
        cached=self.token_cache.get(token)
        if cached and now-cached[0] <= self.token_cache_seconds:
            return dict(cached[1])
        if not token or not self.api_key:
            return {"available":False,"hard_block":False,
                    "reason":"Jupiter Tokens V2 unavailable (API key not configured)"}
        status,data=await http.get(f"{self.tokens_base}/search",headers=self._headers(),params={"query":token},retries=1)
        rows=data if isinstance(data,list) else []
        item=next((x for x in rows if isinstance(x,dict) and str(x.get("id") or "")==token),None)
        if status!=200 or not item:
            result={"available":False,"hard_block":False,"reason":f"Jupiter token lookup HTTP {status}"}
            self.token_cache[token]=(now,result); return dict(result)
        audit=item.get("audit") if isinstance(item.get("audit"),dict) else {}
        verification=str(item.get("verification") or "").lower()
        tags=[str(x).lower() for x in (item.get("tags") or []) if x is not None]
        banned=(verification=="banned" or "banned" in tags)
        suspicious=bool(audit.get("isSus"))
        mint_disabled=audit.get("mintAuthorityDisabled")
        freeze_disabled=audit.get("freezeAuthorityDisabled")
        result={
            "available":True,"hard_block":bool(banned or suspicious),"is_sus":suspicious,"banned":banned,
            "verification":verification or ("verified" if item.get("isVerified") else "unverified"),
            "is_verified":bool(item.get("isVerified")),"organic_score":_f(item.get("organicScore"),-1),
            "organic_label":str(item.get("organicScoreLabel") or ""),
            "holder_count":int(_f(item.get("holderCount"))),
            "top_holders_pct":_f(audit.get("topHoldersPercentage"),-1),
            "dev_balance_pct":_f(audit.get("devBalancePercentage"),-1),
            "mint_authority_disabled":mint_disabled,"freeze_authority_disabled":freeze_disabled,
            "reason":("Jupiter marks token suspicious/banned" if (banned or suspicious)
                      else f"Jupiter token intel: {str(item.get('organicScoreLabel') or 'unrated')} organic, {verification or ('verified' if item.get('isVerified') else 'unverified')}")
        }
        self.last_token_ok=now; self.token_cache[token]=(now,result); return dict(result)

    async def probe(self,http,token):
        token=str(token or ""); now=time.time(); cached=self.cache.get(token)
        if cached and now-cached[0] <= self.cache_seconds:
            return dict(cached[1])
        if not token:
            return {"available":False,"tradable":False,"hard_block":True,"reason":"missing token mint"}
        start=max(1,int(self.probe_sol*1_000_000_000))
        st,buy,source=await self._one_quote(http,WSOL_MINT,token,start)
        if st==200 and isinstance(buy,dict) and buy.get("outAmount"):
            token_raw=int(buy.get("outAmount") or 0)
            if token_raw>0:
                st2,sell,source2=await self._one_quote(http,token,WSOL_MINT,token_raw)
                if st2==200 and isinstance(sell,dict) and sell.get("outAmount"):
                    final=max(0,int(sell.get("outAmount") or 0))
                    route_loss_bps=max(0.0,(1-final/max(start,1))*10000.0)
                    total=route_loss_bps+2*self.frontend_bps
                    buy_impact=abs(_f(buy.get("priceImpactPct")))*10000
                    sell_impact=abs(_f(sell.get("priceImpactPct")))*10000
                    result={"available":True,"tradable":True,"hard_block":total>self.max_total_bps,
                            "route_loss_bps":route_loss_bps,"frontend_bps":2*self.frontend_bps,
                            "total_friction_bps":total,"buy_impact_bps":buy_impact,"sell_impact_bps":sell_impact,
                            "probe_sol":self.probe_sol,"source":source if source==source2 else f"{source}/{source2}",
                            "reason":f"est. round-trip friction {total/100:.2f}% (route {route_loss_bps/100:.2f}% + frontend estimate {2*self.frontend_bps/100:.2f}%)"}
                    self.last_ok=now; self.last_error=""; self.cache[token]=(now,result); return dict(result)
        self.last_error=f"route quote HTTP {st}"
        result={"available":False,"tradable":False,"hard_block":False,"reason":self.last_error,
                "total_friction_bps":2*self.frontend_bps,"frontend_bps":2*self.frontend_bps,"source":source}
        self.cache[token]=(now,result); return dict(result)
