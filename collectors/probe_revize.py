"""One-off probe: save board pages from the Revize township sites (paced)."""
import os, time, requests
UA = "ZoningTrack/0.1 (+https://zoningtrack.com; public-records collector)"
PAGES = {
 "north-whitehall": ["https://www.northwhitehall.org/", "https://www.northwhitehall.org/boards_commissions/agendas_and_minutes/index.php",
   "https://www.northwhitehall.org/boards_commissions/agendas_and_minutes.php", "https://www.northwhitehall.org/boards_commissions/agendas_and_minutes_.php",
   "https://www.northwhitehall.org/boards_commissions/agendas_an_minutes.php", "https://www.northwhitehall.org/boards_commissions/planning_commission.php",
   "https://www.northwhitehall.org/boards_commissions/zoning_hearing_board.php"],
 "east-allen": ["https://www.eatwp.org/government/township_meetings/agenda___minutes.php", "https://www.eatwp.org/government/boards_commissions/planning_commission/index.php",
   "https://www.eatwp.org/government/boards_commissions/zoning_hearing_board/index.php", "https://www.eatwp.org/government/township_meetings/public_notices.php"],
 "lower-nazareth": ["https://lowernazareth.com/boards_commissions/agendas___minutes.php", "https://lowernazareth.com/boards_commissions/pc_agendas___minutes.php",
   "https://lowernazareth.com/boards_commissions/zhb_agendas___minutes.php", "https://lowernazareth.com/boards_commissions/zoning_hearing_board.php"],
 "lower-saucon": ["https://www.lowersaucontownship.org/government/council/agendas_and_minutes.php", "https://www.lowersaucontownship.org/government/planning_commission/index.php",
   "https://www.lowersaucontownship.org/government/planning_commission/planning_and_development/index.php", "https://www.lowersaucontownship.org/government/zoning_hearing_board.php"],
 "upper-nazareth": ["https://www.uppernazarethtownship.org/", "https://www.uppernazarethtownship.org/boards___commissions/planning_commission.php",
   "https://www.uppernazarethtownship.org/boards___commissions/zoning_hearing_board.php"],
}
os.makedirs("data/_probe_rv", exist_ok=True)
for k, urls in PAGES.items():
    for i, u in enumerate(urls):
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=30)
            open(f"data/_probe_rv/{k}__{i}.html", "w").write(f"<!-- {u} -->\n" + r.text); print(k, r.status_code, u)
        except Exception as e:
            print(k, "ERR", u, e)
        time.sleep(5)
