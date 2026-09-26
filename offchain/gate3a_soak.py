"""Finality soak (Gate 3a): every 5 min for ~13 h, sample the chain's finalized head and the
DEX's cumulative volume + settle reverts, counting samples where finality did not advance.
Runs beside the dex (same env: chain.py picks the backend; the finalized head is a height
where the backend reports one, else a slot)."""
import urllib.request as u, time
import chain

CHAIN = chain.from_env()
last_fin = -1.0; stalls = 0
for i in range(160):
    try:
        f = CHAIN.finalized()
        fin = -1.0 if f is None else float(f.height if f.height is not None else f.slot)
        d = u.urlopen("http://localhost:8080/metrics", timeout=5).read().decode()
        cv = "?"; rev = 0.0
        for l in d.splitlines():
            if l.startswith('jamswap_cum_volume{market="1"}'):
                cv = l.split()[-1]
            if l.startswith("jamswap_settle_reverted_total"):
                try: rev += float(l.split()[-1])
                except: pass
        adv = fin > last_fin
        if not adv and last_fin >= 0: stalls += 1
        ts = time.strftime("%H:%M:%S")
        print("%s sample=%d finalized=%.0f advanced=%s cv=%s reverts=%.0f stalls=%d"
              % (ts, i, fin, adv, cv, rev, stalls), flush=True)
        last_fin = fin
    except Exception as e:
        print("%s sample=%d ERR %s" % (time.strftime("%H:%M:%S"), i, e), flush=True)
    time.sleep(300)
print("GATE3A_SOAK_DONE stalls=%d" % stalls, flush=True)
