# Portfolio Tracker

Osobní přehled akciového portfolia (XTB, Trading 212, ruční záznamy) s kurzy ke dni obchodu, časovým testem a odhadem daní.

- `index.html`, `app.js` – aplikace (GitHub Pages, instalovatelná na mobil)
- `fetch_market.py` + `.github/workflows/daily.yml` – každý pracovní den v 18:00 UTC stáhne ceny (Yahoo Finance), splity a kurzy ECB + ČNB do Gistu
- Data portfolia jsou v tajném Gistu, ne v tomto repozitáři. Potřebné secrets: `GIST_TOKEN`, `GIST_ID`.
