"""One-off probe: save raw pages from township sites so collectors can be built against real markup."""
import os, requests
UA = {"User-Agent": "ZoningTrack/0.1 (+https://zoningtrack.com; public-records collector)"}
PAGES = {
  "forks_robots": "https://www.forkstownship.org/robots.txt",
  "forks_agendacenter": "https://www.forkstownship.org/AgendaCenter",
  "forks_agenda_rss": "https://www.forkstownship.org/RSSFeed.aspx?ModID=65&CID=All-0",
  "forks_home": "https://www.forkstownship.org/",
  "swt_robots": "https://www.southwhitehall.com/robots.txt",
  "swt_home": "https://www.southwhitehall.com/",
  "swt_pc": "https://www.southwhitehall.com/boards-and-commissions/planning-commission",
  "swt_zhb": "https://www.southwhitehall.com/boards-and-commissions/zoning-hearing-board",
  "swt_boards": "https://www.southwhitehall.com/boards-commissions",
  "swt_notices": "https://www.southwhitehall.com/Home/Components/News/News/622/15",
}
os.makedirs("data/_probe", exist_ok=True)
for name, url in PAGES.items():
    try:
        r = requests.get(url, headers=UA, timeout=30)
        body, status = r.text, r.status_code
    except Exception as e:
        body, status = repr(e), "ERR"
    open(f"data/_probe/{name}.html", "w").write(body)
    print(name, status, len(body))
