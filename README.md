# Morgentogenes forsinkelser Roskilde → Holbæk

Scriptet henter tog med afgang fra Roskilde St. mod Holbæk St. mellem 07:00 og 09:00
fra Rejseplanen API 2.0 og gemmer dem i Supabase. Det bruger kun Pythons standardbibliotek.

## Opsætning på Raspberry Pi

1. Supabase (SQL Editor): ny tabel → kør `supabase_schema.sql`.
2. Kopiér filerne til Pi'en. Scriptet læser selv `.env` i sin egen mappe:
   ```
   mkdir -p ~/forsinkelser && cp forsinkelser.py ~/forsinkelser/
   cp .env.example ~/forsinkelser/.env
   nano ~/forsinkelser/.env               # udfyld nøglerne
   chmod 600 ~/forsinkelser/.env
   ```
3. Test uden at skrive til databasen:
   ```
   python3 ~/forsinkelser/forsinkelser.py --dry-run --debug
   ```
4. Slå timeren til (ret `User=` og stien i service-filen, hvis du ikke hedder `pi`):
   ```
   sudo cp forsinkelser.service forsinkelser.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now forsinkelser.timer
   systemctl list-timers forsinkelser.timer
   journalctl -u forsinkelser.service      # se loggen
   ```

## Værd at vide

- Pi'en skal stå i dansk tid (`sudo timedatectl set-timezone Europe/Copenhagen`).
- Tiderne gemmes som dansk lokaltid i `timestamp`-kolonnerne. `fetched_at` er UTC.
- Hvis toget kører til tiden, sender Rejseplanen ingen realtid, så faktisk tid = planlagt tid.
- Aflyste tog får `cancelled = true`, faktisk tid = planlagt tid og 0 minutters forsinkelse, fordi kolonnerne ikke må være tomme.
- Ca. 10 API-kald pr. morgen, langt under grænsen på 50.000 om måneden.
- Bagudrettet: `python3 forsinkelser.py --date 2026-11-19` (virker kun, så længe Rejseplanen har dagens data).
- Hvis tabellen hedder noget andet end `train_delays`, så sæt `SUPABASE_TABLE`.
