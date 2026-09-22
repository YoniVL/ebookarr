# EbookArr — projectcontext voor Claude Code

## Wat dit is
Een zelfgebouwde "Sonarr/Radarr voor ebooks": gebruiker zet boeken op een
wanted-lijst, de app zoekt via **meerdere downloadbronnen op volgorde**
(Gutenberg → Standard Ebooks → LibGen → Z-Library → torrents via Prowlarr),
pakt de eerste bruikbare epub, en kopieert die hernoemd naar een bibliotheekmap.
Directe bronnen downloaden het bestand via HTTP; alleen torrents gaan via
**qBittorrent**. Niet-epub (mobi/azw3) wordt bij import via Calibre omgezet.

Draait lokaal op **Windows, zonder Docker**. De gebruiker kan zelf niet
coderen — alle wijzigingen door Claude Code. De app moet blijven werken via
`start.bat` (dubbelklik) en kan op de achtergrond draaien via de Windows
Taakplanner (`install-task.bat`).

**Git/GitHub (sinds 2026-09-22)**: de map is een git-repo, remote `origin` ->
https://github.com/YoniVL/ebookarr (branch `main`). `.gitignore` sluit `venv/`,
`ebookarr.db` (bevat versleutelde credentials + de boekenlijst van de gebruiker),
`backups/` en `*.log*` uit — die horen nooit gecommit te worden. Gebruiker wil
dat wijzigingen **automatisch gecommit en gepusht** worden (geen aparte vraag
per keer nodig) — wel altijd even de staged diff checken op iets dat op een
geheim lijkt voor het committen.

## Visueel herontwerp (v47/v48) — "Nocturne"

De hele UI (`app/static/index.html`) is nagebouwd volgens de Claude Design-
handoff "EbookArr Herontwerp.dc.html" (basis: Nocturne — 14 artboards met
tokens, iconenblad, componentenblad en elk scherm). Alles zit nog steeds in
dat ene bestand: inline `<style>`, vanilla JS, geen build, geen externe bronnen.
v47 was de eerste, grovere pass; **v48 volgt de artboards exact**.

- **Tokens** in `:root`, korte namen behouden zodat alle inline-styles en JS-
  literals blijven werken: `--bg #161826 · --panel #1f2231 · --panel2 #191b28 ·
  --panel3 #262a3a · --border #2e3242 · --border-strong #3f424d`,
  `--text/#muted/#faint`, `--accent #9184d9` (blurple) + `-hi/-soft/-line/-hover`,
  en `--ok/--warn/--danger/--info` elk met `-soft` (vlak), `-line` (rand),
  `-tx` (tekst). Verder `--r-sm/md/lg`, `--sh-1/2/3`, `--cover-grad`.
- **Knoppen** = soft-fill (GEEN omlijnd meer): `.btn` primair = `--accent-soft`
  vlak + `--accent-line` rand + `--accent-hi` tekst; hover een tint op. `.secondary`
  = `--panel3`; `.danger` = `--danger-soft`. `.small` = 28px. `button.icon` = 32×32
  transparant → `--panel3` bij hover; `.icon.on` = warn-vlak (ster aan); `.icon.danger`.
- **Kop** is 2 rijen: merk (boek-icoon in accent-vlakje + "EbookArr") met de
  health-strip rechtsboven (bolletjes 8px met 3px halo; label kleurt mee bij fout),
  daaronder de tabs. Actieve tab = `box-shadow: inset 0 -2px 0 var(--accent)`.
- **Pills** (status): Gewenst neutraal, Zoeken=info, **Downloadt=accent**,
  Binnengehaald=ok, Mislukt=danger. **Badges** (formaat/bron): EPUB/torrent neutraal,
  gratis bronnen groen (`.free`), accountbronnen blauw (`.acct`), conversie amber
  (`.conv`, met pijl-icoon). Taal = `.langtag` (boxje, geen vlag).
- **Iconen**: `ICONS`-object (23 paden) + `icon(name,size,filled)` bouwt losse
  inline `<svg>` (viewBox 24, stroke 1.5, round; filled = `fill currentColor stroke none`).
- **Kaart-acties zijn nu icoon-only** (op wens gebruiker, minimalistischer):
  ster / zoeken (of herscannen bij 'failed') / kies-zelf (lijst) / bestand-koppelen
  (schakel) / verwijderen (kruis) — elk een `button.icon` met `title`.
- **Covers** 64×96, radius 4px, op `--cover-grad`; placeholder = boek-glyph op `#242838`.
- Sticky subnav in Instellingen = segmented control met scroll-spy (`.subnav a.spy`).
- Overlays sluiten met Esc of klik op de achtergrond. Animaties: `ebPulse` (bolletje,
  50%→scale .65), `ebFade`, `ebRise`. `prefers-reduced-motion` gerespecteerd.
- Klassenamen/id's/teksten/JS-gedrag ongewijzigd — puur reskin. Dark-only.

## Structuur
```
ebookarr/
  app/
    main.py          FastAPI routes + lifespan (start/stopt scheduler)
    db.py             SQLite + mini-migratiesysteem (PRAGMA user_version) + secret-helpers
    obfuscate.py      Machine-gebonden lichte versleuteling voor wachtwoord/API-key
    prowlarr.py       ProwlarrClient — /api/v1/search, categorie-param, is_epub-detectie
    sources/          Downloadbronnen-pakket (zie "Downloadbronnen" hieronder):
                      _common.py, gutenberg.py, standardebooks.py, libgen.py,
                      zlibrary.py, prowlarr_src.py, __init__.py (REGISTRY + volgorde)
    opds.py           OPDS 1.2-catalogus voor leesapps (zie "OPDS-catalogus")
    qbittorrent.py    QBittorrentClient — login (Referer/Origin + verificatie), add met tag,
                      torrents/info, torrents/files
    metadata.py       Boek opzoeken via Google Books + Open Library (samengevoegd/ontdubbeld)
    services.py       Gedeelde logica: zoekvarianten, grab, auto_search_all, poll_downloads, import, health
    scheduler.py      APScheduler BackgroundScheduler: zoeken (interval), poll (1min),
                      follows-check + db-backup (1/dag)
    tray.py           Achtergrond-app: uvicorn in thread + pystray systray-icoon
                      (Open EbookArr / Afsluiten). `pythonw -m app.tray` = geen venster.
    logsetup.py       RotatingFileHandler -> ebookarr.log + console
    static/index.html Volledige frontend (vanilla, geen build-stap)
  requirements.txt    Geen exacte pins (Python 3.14!). + apscheduler, pystray, pillow
  _setup.bat          Gedeelde setup (venv + pip). Aangeroepen door start/install.
  start.bat           Foreground uvicorn (debug); killt oude instance op 8686
  install-task.bat    _setup + snelkoppeling in de Startup-map (pythonw -m app.tray) + nu starten
  uninstall-task.bat  Snelkoppeling weg + oude scheduled task weg + draaiende app killen
  README.md           NL setup-instructies
  ebookarr.db         SQLite (bevat versleutelde secrets)
  ebookarr.log        Logboek (roterend); backups/  = dagelijkse db-kopie (laatste 7)
```

**Achtergrond draaien:** `install-task.bat` zet een `.lnk` in de Windows Startup-map
(`%APPDATA%\...\Startup\EbookArr.lnk` -> `venv\Scripts\pythonw.exe -m app.tray`), net als
Sonarr/Radarr/Prowlarr bij deze gebruiker. Geen admin/schtasks nodig. Tray-icoon met
"Afsluiten". Python-installatie is de python.org "install manager"-build in
`AppData\Local\Python\pythoncore-3.14-64`; venv-`python.exe` is een redirector-stub
-> je ziet altijd 2 processen (stub + echte), dat is normaal.

## Belangrijke keuzes (afgesproken met gebruiker, 2026-08-28)
- **Epub bij voorkeur.** Standaard alleen epub. Instelling `accept_kindle` (+ `calibre_path`):
  accepteert ook mobi/azw3/azw en converteert die bij het importeren naar epub via Calibre's
  `ebook-convert` (`services.find_ebook_convert` zoekt in PATH + standaard install-paden).
  Zonder Calibre gevonden -> `kindle_enabled()` = False -> blijft epub-only.
  Sorteervolgorde: epub eerst, dan sane, dan taal, dan seeders. Import: epub > azw3 > mobi > azw.
- **Volautomatisch downloaden** van de beste epub-release (sortering op seeders,
  instelbaar minimum). Uit te zetten via instelling `auto_download` of per boek
  via de "monitored"-ster.
- Na download: epub **kopiëren** (niet verplaatsen) naar `library_folder` als
  `Auteur - Titel.epub`; torrent blijft seeden.
- Achtergrond: **Windows Taakplanner bij aanmelden**, verborgen, geen admin.
- Metadata: **Google Books + Open Library** samengevoegd. Geen API-keys, geen langRestrict.
  Instelling `reading_languages` (default `en,nl`, checkboxes in UI): edities in een niet-gelezen
  taal vallen weg bij het opzoeken (metadata.py `_apply_language_filter`) én worden bij de
  torrent-zoekresultaten omlaag gesorteerd (services `_language_rank` + `_LANG_MARKERS`).
- Secrets (`qbittorrent_password`, `prowlarr_api_key`, `zlib_email`, `zlib_password`,
  `annas_key`) **licht versleuteld** in db via `obfuscate.py` (machine-gebonden;
  gekopieerde db => opnieuw invoeren). `db.SECRET_KEYS`.
- **Geen** Discord/ntfy-meldingen.

## OPDS-catalogus (v37) — `app/opds.py`
Leesapps (KOReader, Thorium, Moon+ Reader, Librera, Marvin…) kunnen via OPDS 1.2 in
de bibliotheek bladeren en epubs downloaden. Routes in main.py: `/opds` (nav),
`/opds/all`, `/opds/recent`, `/opds/authors`, `/opds/author/{name}` (acq),
`/opds/download/{id}` (FileResponse epub), `/opds/cover/{id}` (302 -> resolve_cover).
Alleen boeken met `status='downloaded'` + bestaand bestand. Optionele Basic-auth:
`_opds_guard` checkt `opds_user`/`opds_password` (secret) — leeg = open (alleen LAN).
`/api/settings` geeft `opds_url` (`http://<lan-ip>:8686/opds`, IP via UDP-socket-truc).
Instellingen-sectie "Leesapps" met kopieer-link + optioneel wachtwoord (checkbox
`opds_auth` wist het wachtwoord als 'ie uit staat).

## Metadata in het epub (v37)
`services._embed_metadata(book, epub_path)` draait na plaatsing/conversie Calibre's
**`ebook-meta`** (`services.find_ebook_meta()`, naast `ebook-convert`) om titel/auteur/
taal/cover ín het bestand te schrijven. Cover wordt eerst via `resolve_cover` gedownload
naar een temp-bestand. Instelling `embed_metadata` (default "1"; geen effect zonder Calibre).
`_find_calibre_tool(name)` is de gedeelde zoeker voor beide tools.

## Z-Library-statusbolletje (v45)
`zlibrary._health` (module-state) + `zlibrary.status()` -> `('ok'|'error'|'unconfigured'|'unknown', detail)`.
`_mark(ok, detail)` wordt gezet door elke echte `search()`/`quota()`/`_ensure_session()`-poging
(succes of fout). `status()` doet NOOIT een login (leunt op `_health` + een goedkope
`_session_valid`-probe als `_health` >5min oud is). `services.health()` -> `out["zlibrary"]`
("ok" / "error: <detail>" / "unconfigured" / "unknown"). Frontend: bolletje in de healthbalk
naast Prowlarr/qBittorrent — rood + tooltip bij fout, oranje bij "nog niet gecontroleerd",
niks als Z-Library niet is ingesteld. Lage-limiet-waarschuwing zit achter het bolletje gevouwen.

## Z-Library-daglimiet (v37)
`zlibrary.quota()` -> `{used, limit, left, resets_in}` via `/papi/user/dstats` (2 min cache).
`find_from_sources`: als Z-Library de winnende directe bron is maar `quota_left()==0`,
geeft het `(None, [], opmerking)` -> boek blijft 'wanted' en wordt opgehaald na de reset
(geen terugval op torrents). `/api/status` + `/api/settings` geven `zlib_quota`;
UI toont "x/10 vandaag" in Instellingen en een waarschuwing in de headerbalk bij ≤2 over.

## Zoekrelevantie (v36)
`metadata._rank_by_relevance(items, query)` sorteert de merge van Google+OL op
titel/auteur-match met de zoekterm en **gooit onzin weg** ("Go Ask Alice" bij "dune").
Score: aandeel zoekwoorden terug in titel+auteur, + bonus exacte/prefix-titel,
+ bonus als titel én auteur beide raak zijn ("titel auteur" gezocht).
`_COMPANION_RE` / `_POSSESSIVE_RE` markeren studiegidsen / "George Orwell's 1984";
zijn er echte treffers, dan vallen die helemaal weg. Alleen-stopwoord-queries en
"niets haalt het"-gevallen houden de API-volgorde.

## Covers (v36)
- `metadata._ol_cover(cover_id/isbn)` -> `covers.openlibrary.org/...-L.jpg?default=false`
  (`?default=false` => ontbrekende cover = 404 i.p.v. leeg plaatje, zodat de UI-placeholder komt).
- `GET /api/cover?isbn=&title=&author=&u=<opgeslagen cover_url>` -> `services.resolve_cover()`:
  probeert opgeslagen URL, dan OL-op-ISBN, dan OL-zoek-op-titel/auteur -> cover_i;
  302 naar de eerste die een echte afbeelding teruggeeft, anders 404 + 1px-PNG.
  Uitkomst 30 dagen in `kv_cache` (ook de negatieve, als "").
- Frontend: `coverSrc(o)` bouwt die URL; alle `<img>` gebruiken 'm + `loading="lazy"`.

## Metadata-cache (v35)
`metadata._disk_cached(prefix, key_parts, producer)` cachet de zware API-calls
(`_google`, `_openlibrary`, `_ol_by_author`, `_ol_by_series`) een week lang in de
DB-tabel `kv_cache` (`db.cache_get/cache_set`, migratie m009). Bij een fout valt het
terug op een langere "stale"-kopie (8×TTL). In-memory `_CACHE_TTL` = 1u.
Google 429-cooldown loopt nu op: 10 → 30 → 60 min i.p.v. altijd 5.

## Metadata: auteur-bibliografie (belangrijk)
`metadata.by_author(name)` haalt een zo volledig mogelijke bibliografie op:
gepagineerde Open Library (`search.json?author=`, mét taal per werk) + gepagineerde
Google Books (`inauthor:"..."`, `startIndex`). Niet-Latijnse titels eruit
(`_mostly_latin`). Daarna: als de auteur meer en- dan nl-edities heeft -> stamp 'en'
op ALLE boeken, anders 'nl' (matcht de "origineel Engels -> Engels"-regel).
`metadata.search(query, deep=True)` gebruikt ALLEEN `by_author` als de query op een
persoonsnaam lijkt (`_looks_like_person`: 2-4 woorden, geen cijfers, geen lidwoorden,
en GEEN bezittelijke vorm zoals "Barney's Version" — `_POSSESSIVE_RE` sluit dat expliciet
uit, anders werd zo'n titel als auteursnaam gezocht en kwam er niets uit) —
anders de gewone titel-zoektocht. De add-resultatenpagina (Enter) roept met `deep=1&limit=80`.
`_apply_language_filter` (v50): garandeert dat het #1-gerankte resultaat (uit
`_rank_by_relevance`, dus de beste titel/auteur-match) nooit wegvalt puur omdat een bron
(meestal Open Library) een verkeerd/niet-representatief taal-label geeft aan de editie in
de zoekresultaten — anders bleef er soms alleen een irrelevant toevalstreffer over.
`check_follows` (author) gebruikt `by_author` direct; (series) gebruikt `by_series`.
Max 25 nieuwe suggesties per follow per ronde (nieuwste jaar eerst), rest volgt bij de volgende check.

**`metadata.by_series(name, author)`**: Open Library heeft geen bruikbaar series-veld in
resultaten, maar `q=series:"X"` én `q=subject:"X"` als query werken wél (subject is veel
completer: Discworld series->24, subject->~70). Beide + naam-varianten (met/zonder "The")
samengevoegd, gefilterd op achternaam. Vindt ook delen zónder de reeksnaam in de titel
("Guards! Guards!", "Mort" in Discworld). Box-sets/omnibussen eruit (`_BOXSET_RE`), geen
taalfilter (weinig delen) maar wel de en/nl-stempel. Getest: Discworld 70, DCC 8, The Expanse 6.

## Downloadbronnen (v27 — nieuw)
`app/sources/` — elke bron is een module met `NAME`, `LABEL`, `enabled()`,
`search(book) -> [candidate]`; directe bronnen ook `download(cand, dest_dir) -> Path`.
`sources.REGISTRY` bepaalt de volgorde:

1. **Gutenberg** (`gutenberg.py`) — gutendex API, publiek domein, gratis, geen account. Getest & werkend.
2. **Standard Ebooks** (`standardebooks.py`) — scraped de publieke zoekpagina + boekpagina voor de `.epub`-link (OPDS vereist nu account). **Getest & werkend (v34):** de download-link geeft eerst een "Your download has started"-tussenpagina → `?source=download` toevoegen slaat die over (fallback: meta-refresh volgen).
3. **LibGen** (`libgen.py`) — best-effort HTML-scrape over een mirror-lijst. **Standaard UIT** (`src_libgen` default "0"): op de verbinding van de user (en veel BE-ISP's) worden alle LibGen-domeinen naar een DNS-sinkhole (`193.191.210.104`) geleid of getimeout. Werkt alleen met VPN. `_SEARCH_BUDGET=12s`. Nooit tegen een echte LibGen getest.
4. **Z-Library** (`zlibrary.py`) — `/eapi/`-JSON met cookie-login (`zlib_email` + `zlib_password`, versleuteld opgeslagen). Domeinen wisselen (`DOMAINS`), vastzetten via `zlib_domain` (user gebruikt `articles.sk`). Standaard **uit** (`src_zlibrary`), pas actief als e-mail+wachtwoord ingevuld zijn. Gratis account ≈ 10 downloads/dag → 403/429 = "downloadlimiet bereikt". **Getest & werkend (v28)** — zoeken + download van "Shadows Upon Time" (4.5 MB epub). Twee valkuilen die opgelost zijn:
   - **Zoeken = POST met form-data** naar `/eapi/book/search` (`data={message,limit,page}`). GET of JSON-body geeft de *populaire lijst* i.p.v. zoekresultaten (verwarrend: `success:1` maar totaal andere boeken).
   - De `/dl/...`-route (soms ook `/eapi/user/login`) zit achter een **JavaScript proof-of-work** ("Checking your browser ..."): zoek `i` zodat `sha1(seed+i)[n1]`/`[n1+1]` bepaalde bytes zijn, zet `c_token`-cookie + `c_time`, herlaad. `_solve_challenge()` parset seed/n1/target uit de HTML en bruteforcet (~65k sha1, <1s). `_request_solving()` doet dit automatisch bij 403/503 + "Checking your browser" en **probeert het meerdere keren** (de check accepteert de oplossing niet altijd meteen; `time.sleep(1.2)` zodat `c_time` aannemelijk is).
   - **Sessie wordt bewaard** in `zlib_session` (secret; JSON `{domain, cookies}`) zodat we niet bij elke zoekopdracht opnieuw inloggen (login is flaky + kan gelimiteerd worden). `_ensure_session`: geheugen → DB-sessie (met `/eapi/user/profile`-check) → verse login (3 pogingen per domein). `_reset_session` en een wijziging van `zlib_email/password/domain` in Instellingen wissen `zlib_session`.
   - `search()` gooit nu een `RuntimeError` bij een fout (i.p.v. stil `[]`) zodat `find_from_sources` weet dat de bron hikte en géén torrent pakt.
   - `search()` scoort + sorteert zijn eigen resultaten (exacte titel, taal, epub, verstandige grootte, `qualityScore`) en **ontdubbelt** identieke edities.
   - `_raw_request()` herprobeert bij `ConnectionResetError`/timeout (Z-Library reset de connectie vaak); DNS-fouten niet.
   - **De `dl` uit een zoekresultaat kan onzin zijn** (bv. de letterlijke string `"exactEnd"`). `_abs()` accepteert alleen `/pad` of `http...`; anders valt `download()` terug op het `/eapi/book/{id}/{hash}`-detail (geeft de juiste `/dl/...`) en daarna de klassieke `/dl/{id}/{hash}`-route.
5. **Torrents via Prowlarr** (`prowlarr_src.py`, `IS_TORRENT=True`) — verpakt de bestaande `services.search_book` als bron. Kan niet uitgezet worden (val-terug). `download()` niet gebruikt: gaat via de torrent/qBittorrent-flow.

**Anna's Archive**: alleen een key-veld in Instellingen (`annas_key`, versleuteld).
Geen module — de user heeft geen lidmaatschap. Stub voor later.

`sources/*.py` importeren `services`/`db` **lazy** (in de functie) om een
circulaire import te vermijden; `services` importeert `sources` bovenaan.

**Orchestratie in `services.py`:**
- `find_from_sources(book)` — **directe bronnen (Gutenberg/SE/Z-Library) parallel** (ThreadPoolExecutor), daarna op prioriteit (REGISTRY-volgorde) de winnaar kiezen; alleen als geen directe bron een treffer heeft → torrents (Prowlarr). Geeft `(bronnaam, bruikbare kandidaten, opmerking)`. **Als een directe bron een fout gaf én alleen torrents treffers hebben → pakt niets** (`(None, [], opmerking)`); boek blijft 'wanted', volgende zoekronde probeert opnieuw. "Nu zoeken" op een niet-publiek-domein boek: ~2-5s (was ~42s). Gebruikt door `grab_best()` en de auto-scheduler.
- `_rank_candidates(cands, pref, book)` — sorteert op: epub · juiste taal · **exacte titel** · **verstandige bestandsgrootte** (`_sane_size_score`: 0,12-4 MB best, >8 MB verdacht) · seeders (torrents) · grootte (gecapt op 4 MB). Niet meer "grootste bestand wint".
- `find_all_candidates(book)` — vraagt ÁLLE bronnen **parallel** (ThreadPoolExecutor, 50s totaal-deadline; trage bron wordt overgeslagen), voor het "Kies zelf"-scherm. Sorteert: bruikbaar eerst, dan bronvolgorde.
- `_candidate_ok()` — formaat geaccepteerd (epub, of kindle+Calibre), grootte 15KB–80MB, torrent: sane + genoeg seeders + niet al gepakt, directe bron: taalcheck.
- `grab_candidate(book, cand)` — torrent → `grab_release` (qBittorrent); direct → `_download_direct` (status 'downloading' → tempdir download → `_place_in_library` → import + history).
- `_place_in_library(book, src, ext)` — epub → kopie; anders `_convert_to_epub` via Calibre.
- `books.source` (kolom via m008) onthoudt via welke bron een boek binnenkwam.

**Routes:** `GET /api/books/{id}/search` → `find_all_candidates`;
`POST /api/books/{id}/grab` (body = volledige candidate) → `grab_candidate`.
Instellingen: `src_gutenberg/src_standardebooks/src_libgen/src_zlibrary`,
`libgen_mirror`, `zlib_email/zlib_password/zlib_domain`, `annas_key`.
`/api/settings` geeft `sources_enabled` (actieve volgorde) terug.

## Zoek-strategie (belangrijk!)
Publieke torrent-indexers (Pirate Bay, LimeTorrents, Nyaa, …) doen strikte
AND-matching. Lange titel+auteur queries geven vaak 0 resultaten omdat
torrent-namen zelden de volledige ondertitel bevatten. `services._build_queries`
probeert daarom kórte varianten (kernwoorden titel + achternaam), max 3 stuks,
en stopt bij de eerste variant met resultaten.

**De auteur zit in ELKE variant** (feedback gebruiker 2026-08-29): nooit terugval
op alleen-titel, want bij korte titels als "Mort" krijg je dan totaal verkeerde
boeken. Levert geen enkele auteur-variant iets op -> lege lijst (eerlijk), niet
opeens 50 verkeerde resultaten.

Queries mét "epub" erin gaan ZONDER categorie-filter naar Prowlarr (het woord +
`is_epub`-filter doet het narrow-werk; publieke indexers categoriseren ebooks
slecht). Queries zonder "epub" gebruiken wél de boekcategorieën (tegen film-ruis).

Prowlarr-zoekopdrachten over ~7 publieke indexers duren ~30-90s elk (timeout 90s
in prowlarr.py). Een indexer die hapert wordt per variant overgeslagen. Een
auto-zoekronde met veel (onvindbare) boeken kan lang duren — draait in de
achtergrond-scheduler, `max_instances=1`.

## Activity-tab
`GET /api/activity` -> `services.activity()`: lopende downloads (`status` in
downloading/failed) verrijkt met live qBittorrent-data (progress, dlspeed, eta,
seeds) + laatste 25 history-events + `searching`. Frontend ververst elke 3s zolang de tab open is.

## Zoek-voortgang (v33)
`services._search_progress` = in-memory dict `{book_id: {phase, since, phase_since, trigger}}`
onder `_progress_lock`. `find_from_sources` zet per bron `"zoekt bij <label> (i/n)"`;
`grab_best` (trigger "nu zoeken") en `auto_search_all` (trigger "automatisch") wrappen
met de contextmanager `_searching(book_id, trigger)` en zetten daarna `"downloaden via <bron>"`.
`services.search_progress()` verrijkt met de boektitel. Zichtbaar via `/api/status`
(`searching`) en `/api/activity`. Frontend: `searchingById` map (uit beide endpoints),
pulserende regel op de boekkaart + "Nu zoeken"-knop wordt "Zoeken…" (disabled),
chip in de health-balk, "Nu aan het zoeken"-sectie op de Activity-tab. Zolang er iets
loopt pollt de UI elke 2s (`fastPoll`/`syncPollRate`), anders 8s (books) / 30s (status).
Het handmatige "Kies zelf" (`find_all_candidates`, parallel) heeft geen per-bron-voortgang.

## Diverse (v22)
- **Cover-placeholder**: JS `NO_COVER` (data-URI SVG boek-icoon), `onerror="coverErr(this)"` overal.
- **Favicon**: 📚 als data-URI SVG in `<head>`.
- **Companion-filter**: frontend `COMPANION_RE` + checkbox "Companions verbergen" (default aan) op
  Volgen-tab; filtert dagboeken/atlassen/quiz-/kleurboeken/kalenders uit de suggestieweergave
  (geen backend-wijziging, direct togglebaar).
- **Betere ontdubbeling**: `metadata.main_title()` strip ondertitel (na `:` / `(` / `- ... Novel`);
  `_dedupe` en `db.suggestion_exists` vergelijken op hoofdtitel. "Death's Domain" == "Death's
  Domain: A Discworld Mapp", "Mort" == "Mort (Discworld #4)", maar "Science of Discworld" != "II".
- **"Nu zoeken"** knop op wanted/failed boeken -> `POST /api/books/{id}/grab-best` ->
  `services.grab_best()`: zoekt + pakt meteen de beste bruikbare epub (zelfde criteria als
  auto_search_all). De oude "Zoeken"-knop heet nu "Kies zelf".

## Sanity-checks / duplicaten / bibliotheek / auteurs / backup
- `services.release_check(release, book)` -> (ok, reden): grootte 15KB–80MB + achternaam auteur
  moet in de release-titel staan. Auto-grab slaat niet-ok resultaten over; manuele zoekresultaten
  krijgen `sane`/`sane_reason` en worden gedimd getoond.
- `services.create_book(data, allow_duplicate=False)`: duplicaat-check (titel+auteur / ISBN) ->
  `DuplicateBook` (HTTP 409). Scant ook `library_folder` (`find_in_library`) — match => meteen
  status 'downloaded'. `POST /api/library/rescan` doet dit voor alle 'wanted' boeken.
- `db.backup(keep=7)` -> `backups/ebookarr-YYYYMMDD.db`. Scheduler-job 1×/dag.
- **`rescan_library()` (v38) werkt twee kanten op**: 'wanted' -> 'downloaded' als het bestand
  er staat; **'downloaded' -> 'wanted' als het bestand verdwenen is** (of `library_path`
  bijwerken als het onder een andere naam terug te vinden is). Geeft `{updated, lost, added,
  relinked}`.
  LET OP: `db.add_history` NOOIT binnen een open `with db.get_conn()`-schrijftransactie
  aanroepen (opent een 2e connectie -> "database is locked").
- **Onbekende bestanden automatisch importeren bij herscan (v51)**: user vroeg erom nadat
  bleek dat een handmatig in de map gezet bestand (`Barney's Version`) nergens in de app
  zichtbaar was. `rescan_library()` loopt nu na de bestaande wanted/downloaded-stappen ook
  alle bestanden in `library_folder` langs die aan géén enkel boek hangen
  (`_resolved_library_paths` + `_import_orphan_files`):
  - `_parse_epub_meta(path)` leest titel/auteur/taal/jaar/isbn rechtstreeks uit de
    opf-metadata (zip + Dublin Core XML via de standaardbibliotheek — **geen Calibre nodig**,
    werkt dus ook zonder). Val terug op `_title_author_from_filename` ("Auteur - Titel.ext",
    onze eigen naamgeving) voor mobi/azw3/pdf of een onleesbaar epub.
  - Bestaat er al een boek met exact deze titel/auteur zonder `library_path`? -> daaraan
    koppelen (`relinked`) i.p.v. een dubbel toevoegen. Anders: nieuw boek, status
    'downloaded', `source='handmatig'` (`added`).
  - `metadata._norm_lang` pakt nu ook de primaire subtag van BCP-47-achtige codes
    ("en-US", "nl_BE") — epub's dc:language gebruikt dat vaak, en zonder deze fix bleef
    het boek op de verkeerde taal/leestaal-inschatting staan.
  - Getest met een synthetisch epub + de echte (tot dan toe ongetrackte) "Barney's Version"
    van de gebruiker zelf: beide correct herkend, idempotent (nogmaals herscannen voegt niks
    dubbel toe), en netjes weer opgeruimd na het testen.
- **`find_in_library()` (v46)**: titel-substring-match + **losse auteur-woord-match**
  (`_author_words`, min. 3 tekens, `_AUTHOR_STOP`). Fix: het auteurveld van OL bevat vaak
  auteur + illustrator + vertaler aan elkaar ("Terry Pratchett Paul Kidby") waardoor de oude
  "hele genormaliseerde auteur moet substring van de bestandsnaam zijn"-check faalde. Nu
  volstaat één woord (achternaam) dat terugkomt.
- **Handmatig koppelen (v46)**: `GET /api/library/files` -> `list_library_files()` (alle ebooks
  + welk boek eraan hangt); `POST /api/books/{id}/link {path}` -> `link_book_to_file()`
  (pad moet binnen `library_folder`; zet 'downloaded'; maakt een bestaande koppeling van een
  ander boek los -> dat boek terug op 'wanted'; draait `_embed_metadata`). Frontend: knop
  "Bestand koppelen" op elke niet-downloadende boekkaart -> dialoog met de bestandenlijst.
- **Dubbele `_norm`-definitie verwijderd** (stond per abuis 2× in services.py).
- **`reveal_in_explorer(path)` (v40)**: `explorer /select,` bleek onbetrouwbaar (opende
  de verkeerde map). Nu: shell-API `SHOpenFolderAndSelectItems` via ctypes (PIDL uit het
  exacte pad — kan de map niet missen), `AllowSetForegroundWindow(ASFW_ANY)` vooraf, en
  een daemon-thread `_force_explorer_foreground(folder)` die na 0,4s het `CabinetWClass`-
  venster met de mapnaam in de titel opzoekt en met `AttachThreadInput`+`SetForegroundWindow`
  naar voren haalt (omzeilt de Windows foreground-lock). Alle ctypes-functies hebben
  `argtypes`/`restype` gezet — anders access violation op 64-bit (HWND/PIDL-truncatie).
  Fallback `os.startfile(folder)`. Route `POST /api/books/{id}/reveal`.
- **Volgen** (auteurs + reeksen): tabel `follows` (type author|series, name, author), tabel
  `suggestions` (state pending|added|dismissed). `GET/POST/DELETE /api/follows` +
  `POST /api/follows/check`. `services.check_follows()` (scheduler 1×/dag) zoekt per follow via
  metadata, filtert op achternaam + (voor reeksen) reeksnaam-in-titel, en zet nieuwe vondsten
  als **suggestie** (NIET automatisch als boek). `suggestion_exists` voorkomt herhaling (alle states).
  `GET /api/suggestions?state=pending|dismissed|added`, `POST /api/suggestions/{id}/accept|dismiss|restore`,
  `.../accept-all|dismiss-all`. `/api/status` geeft `suggestions_pending` -> groen bolletje op "Volgen"-tab.
  UI heeft een inklapbare "Genegeerd (N)"-sectie met Toevoegen / Terugzetten per item.
  Suggesties + genegeerd worden **gegroepeerd per follow** (`found_via`), elke groep inklapbaar
  met **20 per pagina** (client-side pager) en per-groep "Alles toevoegen/negeren".
  Sorteerkeuze (v42, frontend + localStorage `sugSort`): Titel A–Z / Jaar oud→nieuw / Jaar nieuw→oud
  (`sortSuggestions()` in renderGroups). Jaar-oplopend = leesvolgorde van een reeks.
  `POST /api/suggestions/bulk` `{ids, action: accept|dismiss|restore}`. check_follows heeft geen
  25-cap meer (plafond 200/follow/ronde).
  `by_author` gooit vertaalde edities eruit (taal != en/nl, "(German Edition)"-markers, titel==auteursnaam).
- **Voorkeurstaal per boek** (`books.pref_language`, kolom via m007): `services.preferred_language(book)`
  = handmatige keuze, anders "en" als `book.language=='en'` anders "nl" (regel gebruiker: origineel
  Engels -> Engels, al de rest incl. NL -> Nederlands). `search_book` gebruikt dit als enige want_lang.
  Toggle op de boekkaart ("zoekt: EN/NL (auto)"), `POST /api/books/{id}/pref-language`.
- Versienummer: `app/__init__.py` `__version__`, zichtbaar rechtsboven in UI + `/api/status` + log.
  Verhogen bij elke wijziging. `start.bat` killt een oude instance op 8686 vóór het starten.

## Wanted-tab: filter / sorteren / bulk-verwijderen
- `GET /api/books` sorteert op `title COLLATE NOCASE`; frontend her-sorteert ook (localeCompare 'nl').
- `loadBooks()` haalt op + bewaart in `_allBooks`; `renderBooks()` tekent. Statusknoppen
  én het **tekstfilter** (`#book-filter`, titel/auteur-substring) her-tekenen zonder refetch.
- Boekkaart: cover via `/api/cover`, bibliotheekpad ingekort tot "📁 in bibliotheek" +
  knop "map openen" (`POST /api/books/{id}/reveal` -> `services.reveal_in_explorer`,
  `explorer /select,`). "Nu zoeken"-uitslag blijft ~45s als ✓/✗-regel op de kaart
  (`grabResults` in de frontend), i.p.v. een vluchtig toast.
- Instellingen: sticky sub-nav (`.subnav`, anchors naar `#set-*`) + sticky opslaan-balk
  (`.savebar`) met het opgeslagen-bericht ernaast.
- Filterknoppen (frontend-only): Alles / Gewenst / Downloadt / Binnengehaald — filtert op `status`.
- "Selecteren"-modus: checkboxes per kaart + "Alles"-checkbox. `POST /api/books/delete`
  `{ids:[...], delete_files:bool}` -> `services.delete_books()`. `delete_files` verwijdert
  het bestand op `books.library_path` (alleen als het een bestaand bestand is).
- `DELETE /api/books/{id}?delete_file=bool` gaat nu ook via `services.delete_books`.
- Verwijder-dialog vraagt de bestand-optie alleen als minstens één geselecteerd boek
  een `library_path` heeft. qBittorrent-torrents worden NIET aangeraakt.

## Bekende valkuil
Python **3.14** — geen exacte version-pins in requirements.txt (anders probeert
pip oude pakketten via Rust te compileren). `>=` gebruiken.

qBittorrent-login: vereist `Referer`/`Origin` headers (CSRF) én verificatie via
`GET /api/v2/app/version` — vertrouw niet op body == "Ok." (gebruiker kreeg
HTTP 204 zonder body). Zit al in `qbittorrent.py`.

## Mogelijke verbeteringen (nog niet gedaan)
- LibGen werkt niet op deze verbinding (DNS-sinkhole); alleen relevant met VPN.
- `find_from_sources` wacht op de traagste directe bron voor de prioriteitskeuze
  (max ~3s). Zou nog iets sneller kunnen door te stoppen zodra bron 1 klaar is.
- Nederlandstalige boeken: valt of staat met welke indexers in Prowlarr staan;
  de huidige set is Engels-georiënteerd.
- In-process cache voor metadata-zoekopdrachten (Google Books kan 429 geven).
- Meerdere epubs uit één torrent importeren (nu alleen de grootste).
- Handmatige "volgende release proberen"-knop bij een vastgelopen download
  (gebeurt nu automatisch in de volgende zoekronde).
- Series/auteur monitoren i.p.v. losse titels.

## Belangrijk om te onthouden
- Gebruiker kan niet coderen — leg keuzes kort uit, geen code laten lezen
- Alles blijft werken via `start.bat`; achtergrond via `install-task.bat`
- Nederlandstalige UI/README/logmeldingen
