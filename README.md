# EbookArr

Een kleine, zelfgebouwde "Sonarr voor ebooks": je zoekt een boek op, zet het op
een wanted-lijst, en EbookArr zoekt zelf periodiek naar een **epub**. Het
probeert meerdere bronnen op volgorde — eerst gratis, legale bronnen
(Project Gutenberg, Standard Ebooks), dan LibGen en Z-Library, en pas als laatste
**torrents via Prowlarr/qBittorrent**. Directe downloads komen meteen in je
bibliotheekmap; alleen torrents lopen via qBittorrent. Je kunt ook auteurs en
reeksen volgen.

Draait lokaal op Windows, zonder Docker — net als je Sonarr/Radarr setup.

## Installatie (eenmalig)

1. **Python installeren** (als je dat nog niet hebt):
   Download van https://www.python.org/downloads/ en installeer.
   ⚠️ Vink tijdens installatie **"Add python.exe to PATH"** aan.

2. **Deze map** ergens op je schijf zetten, bijvoorbeeld `C:\EbookArr`.

3. **Dubbelklik op `start.bat`**. De eerste keer duurt dit iets langer
   (virtuele omgeving + packages). Daarna is het snel.

4. Open in je browser **http://localhost:8686**.

## Configureren

Ga naar **Instellingen**:

- **Prowlarr URL / API key** — de key staat in Prowlarr onder `Settings > General`.
- **qBittorrent URL** — de Web UI-URL. Kijk in qBittorrent onder
  `Tools > Opties > Web UI` welke **poort** daar staat en gebruik
  `http://localhost:<poort>`.
- **qBittorrent gebruikersnaam/wachtwoord** — je Web UI-login.
- **Automatisch zoeken – interval** — om de hoeveel uur EbookArr zelf zoekt.
- **Beste epub-release automatisch downloaden** — aan = volautomatisch.
- **Minimum aantal seeders** — releases met minder seeders worden genegeerd.
- **Bibliotheekmap** — hierheen wordt de epub gekopieerd (`Auteur - Titel.epub`);
  de torrent blijft in qBittorrent seeden.
- **Talen die je leest** (EN/NL) — boeken/releases in andere talen worden weggefilterd.

Wachtwoord en API-key worden versleuteld opgeslagen (machine-gebonden; een
gekopieerde database vraagt om opnieuw invoeren).

### Downloadbronnen

Onderaan Instellingen staat **Downloadbronnen**. Je kunt elke bron aan- of
uitzetten; EbookArr probeert ze in deze volgorde en stopt bij de eerste die een
bruikbare epub geeft:

1. **Project Gutenberg** — gratis, publiek domein, geen account.
2. **Standard Ebooks** — gratis, publiek domein, mooi opgemaakt.
3. **LibGen** — grote catalogus. Werkt een mirror niet, vul dan zelf een mirror
   in (bv. `libgen.is`); leeg = EbookArr probeert een lijst af.
4. **Z-Library** — vul je **e-mail + wachtwoord** in van je (gratis) Z-Library
   account. Zonder die twee blijft Z-Library uit. Een gratis account mag ± 10
   downloads per dag; daarna krijg je "downloadlimiet bereikt".
5. **Torrents (Prowlarr)** — de laatste stap, kan niet uitgezet worden.

*Anna's Archive* heeft alleen een veld voor een member-key (voor later); er wordt
nog niet mee gezocht.

> LibGen is nog niet tegen de echte site getest — als het zoeken daar niks
> oplevert terwijl het boek er wel staat, geef dat dan door, dan stem ik de bron af.
> (Z-Library werkt.)

## Gebruik

- **Wanted** → typ een titel; kies uit de treffers (cover/auteur/jaar via Google
  Books + Open Library), of druk Enter voor de volledige lijst. Bij een auteursnaam
  krijg je zijn hele bibliografie.
- **Nu zoeken** op een boek zoekt meteen (alle bronnen op volgorde) en pakt de
  beste epub. **Kies zelf** toont de resultaten van álle bronnen naast elkaar,
  met een label welke bron het is. De **ster** zet automatisch zoeken per boek aan/uit.
- **Activity** toont lopende downloads en recente gebeurtenissen.
- **Volgen** — volg een auteur of reeks; nieuwe boeken komen als *suggestie* (niet
  automatisch gedownload). Groen bolletje = er staan suggesties klaar.

## Op de achtergrond draaien (aanrader)

Dubbelklik op **`install-task.bat`**. EbookArr start dan automatisch bij het
aanmelden, **met een icoontje in je systeemtray** (naast de klok) — net als
Sonarr/Radarr/Prowlarr. Rechtsklik op het icoontje → **Afsluiten** om te stoppen.

Ongedaan maken: **`uninstall-task.bat`**. `start.bat` blijft altijd werken om de
app in een venster te starten (handig om foutmeldingen te zien).

## Logboek

Alles wat EbookArr doet komt in **`ebookarr.log`** in deze map. De database wordt
dagelijks gekopieerd naar de map **`backups/`** (laatste 7).

## Lezen op je telefoon of e-reader (OPDS)

Onder Instellingen → **Leesapps (OPDS)** staat een link (bv. `http://192.168.x.x:8686/opds`).
Voeg die toe in een leesapp die OPDS ondersteunt — **KOReader, Thorium, Moon+ Reader,
Librera, Marvin** — en je kunt door je hele bibliotheek bladeren (op titel, op auteur,
recent toegevoegd) en boeken rechtstreeks downloaden over je wifi.

Standaard is dit zonder wachtwoord (alleen bereikbaar op je eigen netwerk). Wil je het
afschermen, zet dan "Beveiligen met een wachtwoord" aan en vul in je leesapp dezelfde
gebruikersnaam en wachtwoord in.

## Metadata in het epub

Als je Calibre hebt, schrijft EbookArr bij het importeren de juiste titel, auteur, taal
en cover ín het epub-bestand, zodat elk boek er in je leesapp netjes uitziet. Aan/uit
onder Instellingen → Bibliotheek &amp; metadata.

## Kindle-formaten (optioneel)

Standaard zoekt EbookArr alleen naar **epub**. Wil je ook mobi/azw3 kunnen
binnenhalen, zet dan bij Instellingen "Ook Kindle-formaten accepteren" aan. Dat
werkt alleen als je **Calibre** hebt geïnstalleerd (calibre-ebook.com): EbookArr
gebruikt Calibre's `ebook-convert` om zo'n bestand bij het importeren naar epub
om te zetten. Epub blijft altijd de eerste keuze.

## Tips bij weinig zoekresultaten

- EbookArr zoekt standaard **alleen epub**. Releases zonder epub worden overgeslagen
  (tenzij je Kindle-formaten aanzet, zie hierboven).
- Publieke torrent-indexers vinden Engelstalige boeken veel makkelijker dan
  Nederlandstalige. Voor NL-boeken heb je indexers nodig die die aanbieden.
- Lange, exacte titels leveren op sommige indexers niets op; EbookArr probeert
  daarom automatisch ook kortere varianten van de zoekopdracht.
