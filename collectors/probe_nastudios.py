"""One-off probe: save home + minutes pages of na studios township sites (robots-aware, paced)."""
import json, os, re, time
from urllib import robotparser
from urllib.parse import urljoin, urlparse
import requests
UA = "ZoningTrack/0.1 (+https://zoningtrack.com; public-records collector)"
KEYS = ['alburtis','coplay','macungie','hanover-lehigh','upper-milford','washington-lehigh','weisenberg','allen','bethlehem-twp','bushkill','lehigh-twp','lower-mount-bethel','moore','plainfield','washington-northampton']
det = {d['key']: d for d in json.load(open('registry/out/lehigh_valley_detected.json'))}
os.makedirs('data/_probe_na', exist_ok=True)
for k in KEYS:
    d = det[k]; base = d['final_url']; host = urlparse(base).netloc
    rp = robotparser.RobotFileParser(); 
    try:
        rr = requests.get(urljoin(base, '/robots.txt'), headers={'User-Agent': UA}, timeout=20)
        rp.parse(rr.text.splitlines() if rr.status_code == 200 else [])
    except Exception: rp.parse([])
    pages = [base] + [u for u in (d.get('links', {}).get('minutes', []) + d.get('links', {}).get('agendas', [])) if urlparse(u).netloc == host and not re.search(r'\.pdf', u, re.I)]
    for i, u in enumerate(dict.fromkeys(pages)):
        if not rp.can_fetch(UA, u): print(k, 'robots-skip', u); continue
        try:
            r = requests.get(u, headers={'User-Agent': UA}, timeout=30); time.sleep(5)
            open(f'data/_probe_na/{k}__{i}.html', 'w').write(f'<!-- {u} -->\n' + r.text); print(k, r.status_code, u)
        except Exception as e: print(k, 'ERR', u, e)
