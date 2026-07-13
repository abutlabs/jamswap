import urllib.request as u, time, sys
last_fin = -1.0; stalls = 0
for i in range(160):
    try:
        m = u.urlopen("http://lm0:9615/metrics", timeout=5).read().decode()
        fin = -1.0
        for l in m.splitlines():
            if l.startswith("lasair_finalized_height"):
                fin = float(l.split()[-1])
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
