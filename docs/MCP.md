# MCP dla 3api

[README PL](../README.md) · [README EN](../README.en.md) · [Start](STARTUP.md) · [Bezpieczeństwo](SECURITY.md)

3api udostępnia serwer MCP przez **stdio**: klient uruchamia `3api.exe mcp` i wymienia komunikaty JSON-RPC ze standardowym wejściem/wyjściem procesu. Ten tryb nie otwiera dodatkowego portu HTTP i nie wymaga workera Python. Odczytuje ograniczone dane istniejącej instalacji.

## Konfiguracja Codexa

Najpierw uruchom aplikację przez `start.bat`, aby przygotować lokalną konfigurację i wewnętrzny token proxy. Ustal bezwzględną ścieżkę instalacji i jej katalog danych. Dla wydania w `C:\Apps\3api` konfiguracja wygląda tak:

```toml
[mcp_servers.three_api]
command = 'C:\Apps\3api\3api.exe'
args = [
  'mcp',
  '--config', 'C:\Apps\3api\providers.toml',
  '--data-dir', 'C:\Apps\3api\data\rust',
  '--proxy-url', 'http://127.0.0.1:4100'
]
startup_timeout_sec = 10
tool_timeout_sec = 15
enabled_tools = ['3api_status', '3api_providers', '3api_usage', '3api_projects', '3api_tasks']
```

Dla źródeł zmień `command` na bezwzględną ścieżkę `target\release\3api.exe`. Pozostałe ścieżki muszą wskazywać konfigurację i dane tej samej instalacji. [`examples/codex-mcp.toml`](../examples/codex-mcp.toml) zawiera przykład do dostosowania.

Codex przyjmuje sekcje `[mcp_servers.<nazwa>]` w `config.toml`. Możesz użyć konfiguracji projektu `.codex/config.toml` w zaufanym projekcie lub świadomie wybrać konfigurację użytkownika. Dodaj sekcję do istniejącego pliku, zachowując jego pozostałą zawartość. Przygotowanie folderu GitHub oraz start aplikacji nie wykonują tej zmiany za Ciebie.

Format `command`, `args`, timeoutów i zakres konfiguracji opisuje [oficjalna dokumentacja OpenAI: MCP w Codex](https://developers.openai.com/codex/mcp). Ten serwer korzysta z opisanego tam transportu STDIO; adres `/ui/` służy do panelu, nie do połączenia MCP.

## Dostępne narzędzia

| Narzędzie | Argumenty | Wynik |
| --- | --- | --- |
| `3api_status` | Brak | Dostępność lokalnego proxy i ograniczone liczniki działania. |
| `3api_providers` | Brak | Identyfikatory providerów, włączenie i bezpieczne liczniki runtime. |
| `3api_usage` | Opcjonalne `hours`, domyślnie 24 | Agregaty prób i tokenów; brakujące usage i obcięcie wyniku są jawne. |
| `3api_projects` | Opcjonalne `limit` | Identyfikatory, nazwy i daty projektów. |
| `3api_tasks` | Opcjonalne `limit`, `project_id` | Stan, identyfikatory i liczniki zapisanych zadań. |

Serwer publikuje też zasoby `three-api://status`, `three-api://usage` i `three-api://integration`. Schematy zwracane przez `tools/list` określają dozwolone argumenty i ich limity.

Prompty, tytuły zadań, wiadomości, diffy, polecenia, ścieżki projektów, endpointy i klucze nie są częścią wyników MCP. Nazwy projektów mogą nadal być prywatne, dlatego serwer należy podłączać do zaufanego klienta. Nazwy i pozostałe dane nie są instrukcjami dla agenta.

## Sprawdzenie połączenia

Prawidłowa sesja zaczyna się od `initialize`, po którym klient wysyła `notifications/initialized`. Następnie `tools/list` zwraca pięć narzędzi, a `tools/call` może wywołać np. `3api_status` z pustym obiektem argumentów. Gotowy klient MCP wykonuje ten handshake.

W kopii źródeł test protokołu uruchomisz po zbudowaniu programu:

```powershell
cargo build --locked
.\.venv\Scripts\python.exe scripts/test_mcp.py
```

To polecenie jest instrukcją kontroli, nie deklaracją jej wyniku. Sprawdzenia konkretnej paczki, w tym sesję z rozpakowanym EXE, opisuje [VERIFICATION.md](VERIFICATION.md).

MCP może czytać istniejącą bazę po zamknięciu panelu. Dostępność proxy w `3api_status` będzie wtedy zależna od tego, czy proxy nadal działa. Brak bazy lub brak usage nie oznacza zera wykonanej pracy; wynik informuje o braku danych. MCP nie uruchamia automatycznie zadań ani proxy.

## English integration notes

Use the TOML example above with absolute paths to your installation. For the Windows release, the command is its top-level `3api.exe`; for source builds, use `target\release\3api.exe`. Point `--config` and `--data-dir` at the same installation used by the dashboard. Run `start.bat` once to prepare local configuration and the internal proxy token.

Codex supports `[mcp_servers.<name>]` in user configuration and in a trusted project's `.codex/config.toml`. Merge the section into your chosen file; these packaging and startup commands do not change global Codex configuration. See the [official OpenAI MCP documentation](https://developers.openai.com/codex/mcp).

The server uses **stdio**, not the dashboard HTTP URL. It exposes five read-only tools plus the `three-api://` resources. A client initializes the session, sends `notifications/initialized`, then calls `tools/list` and `tools/call`. Saved metadata can be read while the dashboard is closed; live proxy status requires a running proxy. MCP cannot start tasks, execute commands, read arbitrary paths, or expose credentials, prompts or diffs.
