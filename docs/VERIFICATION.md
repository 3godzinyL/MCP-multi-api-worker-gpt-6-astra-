# Weryfikacja — 8 września 2026

Raport dotyczy bieżącego checkoutu `nowe`. Zastępuje wcześniejsze wyniki z innej kopii projektu. Testy korzystają z lokalnych providerów i odrębnych danych; globalna konfiguracja Codexa oraz prywatna historia użytkownika pozostają bez zmian. Poniższe zestawy częściowo się pokrywają — ich liczby nie są sumą różnych testów.

## Wykonane kontrole

| Kontrola | Wynik |
| --- | --- |
| `python -m pytest -q --ignore=tests/test_codex_reconnect.py --ignore=tests/test_codex_tool_reconnect.py` | **427 passed, 1 skipped** w 133,34 s; dwa ostrzeżenia o przestarzałym API Starlette/httpx. |
| Końcowa regresja workera po idempotencji: `tests/test_worker_history.py`, `tests/test_history_persistence.py`, `tests/test_dashboard.py`, `tests/test_panel_upgrade.py` | **65 passed** w 32,01 s, w tym cztery nowe kontrole tworzenia zasobów. |
| Dwa moduły `test_codex_reconnect.py` i `test_codex_tool_reconnect.py` | **9 testów zaliczonych wcześniej** w większej grupie, na zainstalowanym Codexie i lokalnych mockach, z osobnym `CODEX_HOME`. Nie były uruchamiane ponownie w powyższym poleceniu. |
| Rzeczywisty publiczny HTTP Rust/Python — `python -m pytest -q tests/test_rust_workspace.py` | **1 passed** w 7,19 s na końcowej wersji. Dwa czaty, dwa uruchomienia i dwa wywołania API, per-run diff i usage, progi 1m/900k/950k, własne prompty po restarcie, temp/full, DELETE z CSRF/Origin, archiwizacja i przywrócenie bez kasowania plików, powtórzenia po reloadzie/restarcie i konflikty `409`. |
| `cargo fmt --all --check`, `cargo clippy --locked --all-targets -- -D warnings` | PASS. |
| `cargo test --locked` i `cargo build --locked` | **60 testów Rust PASS**, debug build PASS. Po poprawce przekazywania DELETE dodatkowo **8 testów granicy HTTP PASS**. |
| `python scripts/test_rust_proxy.py` | **41 scenariuszy potwierdzonych**. Jeden wymagał zamknięcia testowego uchwytu SQLite przed cleanup na Windows; po poprawce został powtórzony i przeszedł. Obejmuje cztery nowe scenariusze TPM. |
| `python scripts/test_mcp.py` | PASS; rzeczywisty protokół MCP lokalnej binarki. |
| Konfiguracja, telemetria i role API | **83 config, 9 telemetry, 6 provider roles PASS** w celowanych zestawach. |
| `npm run test:ui` | **16 passed** w 36,0 s, Chromium i lokalne demo. Obejmuje retry projektu/rozmowy, zakres dostępu, archiwizację, szkice ustawień, TPM, metryki wcześniejszego runu i status projektu z pustym czatem. Po dopasowaniu konfliktów `409` dodatkowo **3 celowane PASS** w 6,6 s. |
| `python -m pytest -q tests/test_package_source.py` | **28 passed, 1 skipped**. Pominięty test wymaga uprawnień do tworzenia symlinków; rzeczywiste testy junction na Windows przeszły. |
| `node tests/browser/capture-demo.mjs --output docs/images` z Edge | **8 PNG zapisanych i obejrzanych**. Widoki PL/EN, historia, prompty, limity, czat i mobile; brak nieobsłużonych błędów JavaScript. |
| Lokalne odnośniki finalnego eksportu | **16 plików Markdown, 120 celów, 0 uszkodzonych linków**. Przykład kontraktu API jest poprawnym JSON. `git diff --check` dla dokumentacji i pakowania PASS. |
| `python scripts/scan_repository.py --root ./GitHub` | **160 plików, 0 trafień**; sprawdzony cały eksport i wpisy archiwum. |
| `python scripts/verify_release.py --root ./GitHub --source-only` | PASS; dokładny manifest źródeł, SHA-256, rzeczywiste bajty ZIP i lokalne linki README. |

Pierwsze uruchomienie części testów startu trafiło na starszą binarkę debug. Pięć takich testów przeszło po przebudowaniu; powyższy pełny zestaw używa aktualnego programu. Launcher wykrywa teraz nowsze pliki Rust przy kolejnym starcie.

Testy przeglądarkowe sprawdziły wiele projektów i czatów, opóźnione odpowiedzi, odświeżenie, szkice, zapisane diffy, historię, globalne role i progi API, dostęp rozmów oraz archiwizację bez kasowania plików. Dane demo nie są pomiarem płatnego API.

Testy pakowania obejmują allowlistę, ochronę indeksu Git, wykluczenie prywatnych plików, odrzucanie dowiązań, powtarzalne ZIP-y, manifesty i SHA-256, wykrycie zmienionych plików oraz archiwów, dokładny eksport `./GitHub` bez kopiowania go do siebie i ochronę istniejących plików docelowych. Testy formatu EXE używają sztucznego nagłówka PE; nie uruchamiają wydania Windows.

## Końcowy eksport

Folder `./GitHub` zawiera **157 plików źródeł, zasobów i dokumentacji**, w tym osiem PNG z działającego demo. Dodatkowe trzy pliki to `SOURCE_MANIFEST.json`, `3api-source.zip` i `SHA256SUMS`. Skan wszystkich 160 plików zwrócił 0 trafień; kontrola manifestu, sum, bajtów ZIP i odnośników przeszła.

Sprawdzono obecność nowych modułów workspace i TPM, testu publicznego HTTP, testów workspace, przykładów kontraktu i licencji zasobów. Eksport nie zawiera prywatnego `providers.toml`, danych, logów, kopii, środowisk, `node_modules`, katalogu `target` ani binarek debug. Zwykły folder `./GitHub` jest wykluczony z Git tego checkoutu oraz z własnego procesu pakowania. Istniejący sąsiedni folder `../GitHub` pozostawiono bez zmian.

## Zakres i ograniczenia

Lokalny mock sprawdza protokół i zachowanie aplikacji, nie dostęp do płatnej usługi ani limit konta. Minutowy budżet nie widzi żądań spoza tego proxy; estymaty są odróżnione od raportowanego usage. Nie potwierdzono działania zewnętrznych providerów, GitHub Actions ani pozostałych systemów operacyjnych.

Zrzuty pochodzą z obecnego UI na deterministycznym demo. Test prawdziwego Rust/Python jest osobną kontrolą wymienioną w tabeli. Skan ścieżek i wzorców sekretów jest kontrolą pomocniczą, a nie dowodem braku wszystkich prywatnych danych.

Folder GitHub jest eksportem źródeł. Nie dostarczono gotowego release EXE i nie deklaruje się odbioru jego świeżo rozpakowanego bootstrapu. Opcjonalny proces wydania opisuje [RELEASE.md](RELEASE.md).

## English

This report records checks in the current `nowe` checkout. The Python suite passed **427 tests with 1 skipped**, excluding two unchanged real-Codex reconnect modules whose **9 tests had passed earlier** in a larger run against local mocks. Rust checks passed (**60 unit tests**, build, formatting and clippy); **8 HTTP boundary tests** also passed after the DELETE relay fix. All **41 proxy scenarios** were confirmed, including one rerun after a Windows test-handle cleanup fix. MCP passed.

The final real Rust/Python HTTP test passed (**1**, 7.19 s) and covers runs, settings, project archival, restart persistence, token routing, automatic login, creation retries and 409 conflicts. The final worker regression passed **65 tests**, including four new creation checks. Browser tests passed (**16**, 36.0 s, Chromium), followed by **3 targeted passes** after aligning conflict behavior. Packaging tests passed (**28, 1 skipped**); real Windows junction cases passed. Eight current Edge screenshots were captured and inspected. Documentation checks found **0 broken links across 120 local destinations in 16 Markdown files**.

The source export contains **157 source/asset/documentation files** plus its manifest, ZIP and checksums. All **160 files** passed the allowlist/pattern scan; source-inventory, archive-byte and SHA-256 verification passed. Private configuration, runtime data, environments, dependencies and build trees are excluded. Mocks do not confirm paid provider access or account quotas. This is a source delivery; a packaged EXE and its bootstrap smoke test are separate work.
