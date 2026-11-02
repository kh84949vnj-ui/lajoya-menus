# La Joya ISD menus → CSV

Every day, a GitHub Action downloads the High School menu PDFs (last, this and next month) from
https://lajoyaisdcns.com/index.php?page=menus&sid=0208131445498761
and updates [`output/menus.csv`](output/menus.csv) (`date, weekday, breakfast, lunch`).

Run it now: **Actions → Update menus → Run workflow**.
Run locally: `pip install -r requirements.txt && python lajoya_menus.py`
