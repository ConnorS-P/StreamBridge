# StreamBridge

**Kodi-native subscription streaming library integration & source-direct playback broker (v0.1.0)**

StreamBridge makes subscription streaming services behave like ordinary Kodi video sources. Kodi owns the library, metadata, artwork, search, collections, Trakt/TMDb Helper integration, and viewing experience. Provider adapters supply licensed catalog availability and resolve selected titles to authorized playback, while `StreamBridge.Player` (on Windows x64) and cross-platform fallbacks (on Linux, macOS, Android, and LibreELEC) route playback cleanly without scraping login forms or compromising user credentials.

---

## Key Features

- **Native Kodi Library Projection (`.strm` & `.nfo`)**:
  - Exports per-provider Movie (`Streaming/Netflix/Movies/<Title> (<Year>)/...`) and TV (`Streaming/Netflix/TV/<Show>/Season <SS>/...`) `.strm` files containing stable internal URIs (`plugin://plugin.video.streambridge/play/<title_id>`).
  - Generates optional `.nfo` sidecars with verified `<uniqueid type="tmdb" default="true">`, `<uniqueid type="imdb">`, and `<uniqueid type="netflix">` tags plus an ownership marker (`<!-- streambridge:owned:v1 -->`) so user-edited NFOs and local files are never overwritten or deleted.
- **Resilient 6-Step Transactional Reconciliation Engine**:
  - Backed by a WAL-mode SQLite store (`streambridge.db`) with single-writer transactional rollback guarantees.
  - **Failure & Deletion Policy**: Offline states, rate limits, expired API keys, or partial pagination never delete library entries; existing projections are retained and marked `stale`. Unexpected mass catalog drops trigger an automatic **quarantine circuit breaker**.
- **Kodi Ecosystem Integration**:
  - Built on `script.module.routing`, `script.module.requests`, and optional `script.module.inputstreamhelper`.
  - Populates Kodi 21+ `InfoTagVideo` unique IDs and `Window(10000)` properties (`tmdb_id`, `imdb_id`, `VideoPlayer.TMDB`, `streambridge.provider`) so **Trakt (`script.trakt`)**, **TMDb Helper (`plugin.video.themoviedb.helper`)**, **Up Next**, and modern skins work out of the box.
- **Browser-Grade Login Security (Zero Credential Exposure)**:
  - StreamBridge **never** asks for, handles, logs, or stores streaming service passwords or session cookies.
  - Users sign in directly inside the official browser or an isolated `WebView2` profile (`%LOCALAPPDATA%\StreamBridge\BrowserProfile`) where Chromium/Edge encrypts cookies at rest via **Windows DPAPI (`CryptProtectData`)** bound to the current OS user login.
  - Includes a one-click **Sign Out / Clear Isolated Browser Session** action in Kodi settings.
- **Windows HTPC Native Companion (`StreamBridge.Player.exe`) & Cross-Platform Fallbacks**:
  - **Windows x64**: C++20/Win32 companion communicating over a DACL-restricted Named Pipe (`\\.\pipe\StreamBridgePlayer`), featuring a WASAPI exclusive-mode endpoint probe (PCM 2.0, 5.1, 7.1, IEC 61937 AC-3, E-AC-3 Atmos JOC), Media Foundation / DXGI HDR10 / PlayReady SL3000 diagnostics (`--diagnose`), and Kodi focus/audio restoration.
  - **Linux / macOS / Android / LibreELEC**: Full library projection and catalog sync, with automatic playback fallback to installed native Kodi add-ons (`plugin.video.netflix`), Android `StartAndroidActivity` intents (`com.netflix.ninja`), or desktop browser app mode.

---

## Repository Layout

- `addons/repository.streambridge/`: Installable third-party Kodi 21+ add-on repository (`addons.xml`, `addons.xml.md5`, and SHA-256 verified zip packages).
- `addons/script.module.streambridge/`: Shared Python core library (`streambridge_core`).
- `addons/plugin.video.streambridge/`: Kodi video plugin for browsing, playing `.strm` items, syncing catalogs, and managing session security.
- `addons/service.streambridge/`: Kodi `xbmc.Monitor` service handling startup sync, scan/clean callbacks (with self-trigger loop guards), and state synchronization.
- `player/`: CMake C++20/Win32 project for `StreamBridge.Player.exe`.
- `scripts/build_kodi_repo.py`: Builds the installable Kodi repository archives and checksums in `dist/`.
- `tests/`: Automated Gate G1–G5 test suite (`pytest`).

---

## Quick Start

### 1. Run Tests & Build Kodi Repository
```powershell
pip install -e ".[dev]"
pytest
python scripts/build_kodi_repo.py
```

### 2. Build `StreamBridge.Player.exe` (Windows x64, Visual Studio 2022)
```powershell
cmake -S player -B player/build -G "Visual Studio 17 2022" -A x64
cmake --build player/build --config Release
.\player\build\Release\StreamBridge.Player.exe --diagnose
```

### 3. Install & Update in Kodi 21 (Omega)
- **Initial Repository Installation**: In Kodi, enable *Settings -> System -> Add-ons -> Unknown sources*, choose *Install from zip file*, and select `repository.streambridge.zip` (downloaded from [GitHub Releases](https://github.com/ConnorS-P/StreamBridge/releases)).
- **Installing & Updating StreamBridge**: Go to *Add-ons -> Install from repository -> StreamBridge Kodi Repository -> Video add-ons -> StreamBridge*. To pull a newly released version immediately, open the left side-blade menu in the Add-on Browser and click **Check for updates**.

---

## License

Released under the [MIT License](LICENSE).
