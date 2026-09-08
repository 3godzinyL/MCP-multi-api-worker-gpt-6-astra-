# Źródła, folder GitHub i wydanie Windows

[README PL](../README.md) · [README EN](../README.en.md) · [Start](STARTUP.md) · [Wyniki kontroli](VERIFICATION.md)

`GitHub` to zwykły folder **wewnątrz bieżącego checkoutu**, przeznaczony na przygotowany snapshot źródeł. Jest wykluczony w `.gitignore` i w allowliście pakowania, więc nie kopiuje się rekurencyjnie do siebie. Nie jest to ukryty katalog `.github` z konfiguracją workflow. Pakowanie odbywa się lokalnie i nie publikuje repozytorium ani nie zmienia konfiguracji Codexa.

Podstawowa dostawa zawiera źródła, dokumentację i zrzuty ekranu. Gotowy EXE jest osobnym, opcjonalnym artefaktem; samo przygotowanie folderu GitHub go nie buduje ani nie deklaruje jego sprawdzenia.

## Zawartość

```text
GitHub/
├── README.md, README.en.md
├── src/, dashboard/, proxy/, scripts/, tests/, docs/, examples/
├── Cargo.toml, Cargo.lock, requirements.txt, ...
├── providers.example.toml
├── SOURCE_MANIFEST.json
├── 3api-source.zip
├── SHA256SUMS
└── releases/                     # tylko po opcjonalnym pakowaniu wydania
    └── windows-x64/
        ├── 3api.exe
        ├── start.bat, bootstrap.bat, bootstrap.py, ...
        ├── scripts/start.ps1
        ├── dashboard/, proxy/, docs/, examples/
        ├── requirements.txt, providers.example.toml
        ├── RELEASE_MANIFEST.json
        ├── 3api-windows-x64.zip
        └── SHA256SUMS
```

Manifesty określają dokładną zawartość. Źródła zawierają pliki potrzebne do odtworzenia projektu, testów i dokumentacji. Release zawiera skompilowany program i pliki wymagane przez panel: prywatnego workera Python, jego zależności opisane w `requirements.txt` oraz bootstrap. Python 3.11+ musi być zainstalowany; pierwsze uruchomienie przygotowuje `.venv` i pobiera pakiety. Rust i Node.js nie są potrzebne do pracy gotowego wydania.

Archiwum Windows rozpakowuje się do katalogu `3api-windows-x64/`. Uruchamiaj `start.bat` w tym katalogu. `target/debug`, biblioteki kompilatora i `node_modules` nie należą do dystrybucji.

## Eksport finalnych źródeł

Przed pakowaniem zakończ uzgodnione zmiany w bieżącym checkoutcie i wykonaj właściwe kontrole. Źródłem jest aktualna zawartość plików, także gdy repozytorium nie ma pierwszego commita. Nie zastępuj snapshotu zawartością `HEAD` i nie zmieniaj indeksu użytkownika. Nie pakuj plików równocześnie edytowanych przez innych wykonawców.

W katalogu źródeł wykonaj:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_package_source.py
.\.venv\Scripts\python.exe scripts/package_source.py --output-dir ./GitHub
.\.venv\Scripts\python.exe scripts/scan_repository.py --root ./GitHub
.\.venv\Scripts\python.exe scripts/verify_release.py --root ./GitHub --source-only
```

Przygotowanie `.venv` i zależności testowych opisuje README. Nieznany plik w istniejącym katalogu docelowym zatrzymuje pakowanie przed nadpisaniem zawartości. Wybierz wtedy nowy, pusty katalog poza źródłami lub samodzielnie uporządkuj własny eksport. Skrypt niczego rekursywnie nie kasuje. `./GitHub` jest jedynym dozwolonym katalogiem eksportu źródeł wewnątrz checkoutu; dowiązania i junction na ścieżce wyjścia są odrzucane.

## Opcjonalne wydanie Windows

Po eksporcie finalnych źródeł można dodatkowo utworzyć pełną paczkę Windows:

```powershell
cargo build --locked --release
.\.venv\Scripts\python.exe scripts/package_release.py --output-dir ./GitHub/releases/windows-x64
.\.venv\Scripts\python.exe scripts/scan_repository.py --root ./GitHub
.\.venv\Scripts\python.exe scripts/verify_release.py --root ./GitHub
```

`3api.exe` musi powstać z tego samego finalnego snapshotu, który trafił do paczki źródeł. Każda poprawka po buildzie wymaga ponownego zbudowania i spakowania właściwych artefaktów oraz aktualizacji sum. Gdy eksport zawiera release, weryfikuj go bez `--source-only`, aby sprawdzić również zgodność źródeł z runtime.

## Jawna lista plików

Pakowarki dopuszczają określone pliki źródeł, konfiguracje przykładowe, testy, zasoby i dokumentację. `.gitignore` nie jest jedyną ochroną i nie wyznacza samodzielnie zawartości wydania.

Do paczek nie należą prywatne `providers.toml`, poświadczenia, katalogi danych i logów, snapshoty użytkownika, kopie robocze, środowiska Python, `node_modules`, cache, lokalny `target` ani raporty robocze. Wyjątkiem dla skompilowanego programu jest jawnie wybrany `target/release/3api.exe`, kopiowany jako `releases/windows-x64/3api.exe`.

Zrzuty ekranu pochodzą z rzeczywiście uruchomionego demo i przedstawiają wyłącznie dane demonstracyjne. Wykonaj `node tests/browser/capture-demo.mjs --output docs/images` po instalacji zależności Playwright i obejrzyj zapisane PNG. Możesz wskazać zainstalowany Edge przez `$env:PLAYWRIGHT_CHANNEL = 'msedge'`. Dwa pola `DISCORD_IMAGE_1_URL` i `DISCORD_IMAGE_2_URL` na górze obu README są miejscem na własne linki użytkownika; pakowarka nie pobiera zdjęć z internetu.

## Integralność i kontrola rozpakowanego wydania

`SHA256SUMS` zawiera sumy plików i archiwum właściwej paczki. `verify_release.py --root ./GitHub --source-only` sprawdza wyłącznie dokładny eksport źródeł, manifest, sumy, bajty ZIP i linki README. Bez `--source-only` wymaga również kompletnego wydania Windows i kontroluje jego pliki, format EXE oraz zgodność ze źródłami. Suma SHA-256 wykrywa zmianę zawartości; nie jest podpisem cyfrowym wydawcy.

Kontrola statyczna nie zastępuje uruchomienia. Do odbioru wydania użyj świeżo rozpakowanego katalogu, bez lokalnego `target`, `node_modules` i wcześniej przygotowanej `.venv`:

1. Uruchom bootstrap i panel z paczki.
2. Sprawdź automatyczną sesję `/ui/` oraz zadanie na lokalnym mocku.
3. Zatrzymaj i uruchom ponownie aplikację z tym samym katalogiem danych; sprawdź historię i wyniki.
4. Wykonaj sesję MCP: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`.
5. Sprawdź archiwum oraz sumy po końcowym spakowaniu.

Pomocniczy [`tests/browser/release_smoke.py`](../tests/browser/release_smoke.py) automatyzuje bootstrap, rzeczywiste HTTP Rust/Python, dwa czaty, dwa uruchomienia na lokalnym mocku, restart i sesję MCP. Uruchom go z kopii źródeł, wskazując świeżo rozpakowane wydanie oraz osobny pusty katalog roboczy poza eksportem:

```powershell
.\.venv\Scripts\python.exe scripts/verify_release.py --root ./GitHub --extract-to .artifacts/GitHub/smoke-unpacked
.\.venv\Scripts\python.exe -B tests/browser/release_smoke.py --root .artifacts/GitHub/smoke-unpacked/3api-windows-x64 --work-dir .artifacts/GitHub/smoke-work
```

Skrypt korzysta z biblioteki standardowej Pythona, podaje testowe poświadczenia przez środowisko i używa własnej atrapy Codex CLI. Nie wywołuje `manage.py init` ani konfiguracji globalnej. Zapisuje faktyczny wynik w `release-smoke-report.json`. Katalog `.artifacts/` nie należy do dystrybucji. Przeglądarka i prawdziwe konto providera wymagają osobnych kontroli.

Ta lista opisuje wymagany proces, a nie wynik testów konkretnej paczki. [VERIFICATION.md](VERIFICATION.md) zawiera wyłącznie faktycznie wykonane próby i pozostałe ograniczenia.

## English release notes

The ordinary `./GitHub` directory inside the checkout holds the source snapshot, documentation, screenshots, `3api-source.zip`, a source manifest and `SHA256SUMS`. It is excluded from Git and the packaging allowlist. This source export does not require a prebuilt EXE. Use `verify_release.py --root ./GitHub --source-only` to check its exact inventory and archive.

An optional `releases/windows-x64` contains `3api.exe`, the Python worker and bootstrap files, a release manifest, archive and checksums. Build from the same final sources with `cargo build --locked --release`, run `package_release.py`, then verify without `--source-only`. The Windows archive extracts into `3api-windows-x64/`. Packaging excludes private configuration, runtime data, logs, backups, environments, caches, `node_modules`, and build directories. Unknown destination files and links are rejected; no recursive deletion, Git index changes or publication occur.

The release requires an installed Python 3.11+ and access to Python packages on first startup; Rust and Node.js are not needed. Validate a fresh extraction, a mock task, restart persistence and a complete MCP handshake separately from static checks. Record actual results in [VERIFICATION.md](VERIFICATION.md). SHA-256 checksums establish file integrity, not publisher identity.
