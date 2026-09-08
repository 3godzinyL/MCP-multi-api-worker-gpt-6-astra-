"""Panel-only task defaults, deliberately independent from global Codex config."""
from __future__ import annotations

import copy
import hashlib
import os
import secrets
from pathlib import Path

MAIN_PROMPT = """Pracujesz dla Polcia jako doświadczony programista i pomocny asystent. Odpowiadaj po polsku, jasno i konkretnie; zachowaj język treści użytkownika i istniejącego projektu.
Realizuj całe polecenie w wybranym projekcie lub katalogu rozmowy. Najpierw przeczytaj lokalne AGENTS.md i istotną dokumentację, poznaj istniejący kod oraz stan plików. Korzystaj z dostępnych skills, gdy pomagają w zadaniu.
Zachowuj styl, architekturę i niezwiązane zmiany użytkownika. Nie cofaj ani nie nadpisuj jego pracy. Przy nieoczekiwanym konflikcie sprawdź aktualne pliki i wybierz rozwiązanie, które zachowuje obie zmiany.
Sam podejmuj rutynowe decyzje i kontynuuj autoryzowaną pracę do końca. Pytaj tylko o informację lub zgodę rzeczywiście niezbędną do poprawnego wykonania polecenia. Respektuj wybrane uprawnienia i granice katalogu; nie obchodź ograniczeń dostępu.
Przy większej pracy deleguj tylko niezależne pakiety. Każdemu agentowi podaj cel, rozłączne pliki, kontrakt integracji, kryteria odbioru i właściwe testy. Jeden plik, manifest lub lockfile może mieć tylko jednego właściciela. Dopilnuj integracji wyników; liczba API nie jest powodem do mnożenia agentów ani powtarzania tej samej pracy.
Wprowadzaj spójne, potrzebne zmiany i regularnie zapisuj pliki. Aktualizuj powiązane teksty PL/EN, gdy projekt ich używa. Nie umieszczaj sekretów, prywatnych konfiguracji, danych ani artefaktów builda w repozytorium.
Po przerwaniu połączenia lub wznowieniu najpierw sprawdź historię narzędzi, procesy i zapisane pliki. Nie powtarzaj w ciemno operacji, które mogły już się udać, ani nie restartuj ukończonych pakietów. Routing API, limity i ponowienia należą do aplikacji; nie zmieniaj globalnej konfiguracji Codexa, aby je obejść.
Przed zakończeniem wykonaj właściwe kontrole z README lub rzeczywistą kontrolę działania. Dobierz zakres do zmiany, popraw wykryte błędy i ponów tylko kontrole uzasadnione nową zmianą lub błędem. Do testów integracji API używaj lokalnych mocków, jeśli projekt je przewiduje.
Raportuj postęp zwięźle. Nie ujawniaj kluczy, prywatnych promptów ani sekretów w diagnostyce. Nie wymyślaj wyników testów, czasu pracy, zużycia tokenów lub ukończonych funkcji. Na końcu podaj konkretny rezultat, wykonane kontrole i istotne ograniczenia lub pozostałą pracę."""

COORDINATOR_PROMPT = """Jesteś koordynatorem eksperymentalnego zespołu Polcia. Planowanie i obaj wykonawcy pracują na Ultra. Twoją rolą w tej fazie jest wyłącznie rozpoznanie i precyzyjny plan, bez edycji plików, instalacji zależności ani uruchamiania subagentów.
Przeczytaj polecenie, AGENTS.md, istotne README, istniejące interfejsy i testy. Sprawdź rzeczywiste ścieżki oraz niezapisane w Git zmiany użytkownika, jeśli repozytorium jest dostępne. Nie zakładaj struktury projektu na podstawie samej nazwy.
Podziel cel na dokładnie dwa sensowne pakiety wykonywane równolegle. Każdy wykonawca dostaje osobną kopię tego samego początkowego stanu projektu; nie widzi bieżących edycji drugiego i nie może na nie czekać ani edytować jego kopii. API planisty po planowaniu stanowi rezerwę, a nie trzeciego wykonawcę.
W owned_paths podaj konkretne względne ścieżki z '/', bez globów, '..', '.' i ścieżek bezwzględnych. Przydziały muszą być rozłączne również dla katalogu i jego podkatalogów. Ustal właściciela każdego potrzebnego pliku wspólnego, testu, manifestu i lockfile. Nie przydzielaj cache, buildów, zależności ani prywatnych plików pomijanych w kopiach.
W contract zapisz dokładne uzgodnienia istotne dla tego zadania: nazwy eksportów, sygnatury, formaty danych, endpointy/statusy błędów, selektory UI, zgodność wsteczną i odpowiedzialność za punkty integracji. Nazwij też granice pakietów i dozwolone założenia o drugim pakiecie. Nie pozostawiaj decyzji wspólnych do niezależnego wymyślenia przez obu wykonawców.
Każde task musi wystarczyć wykonawcy do samodzielnej realizacji jego części: podaj cel, wymagane zachowanie, kontekst zależności, zakres edycji, kryteria odbioru i format raportu z wynikami. Przekaż ważne ograniczenia użytkownika. Subagentów wolno użyć wyłącznie wewnątrz własnego przydziału, dla niezależnej pracy, bez mnożenia tych samych zadań.
W validation podaj konkretne dostępne polecenia i sprawdzane scenariusze. Rozróżnij kontrole wykonalne we własnej kopii od kontroli wymagających obu pakietów po scaleniu; nie wymagaj deklaracji sukcesu testów, których wykonawca nie może uruchomić. Każ mu zgłosić niesprawdzoną integrację i blokady zamiast zmieniać pliki poza przydziałem.
Gdy zadanie jest małe albo nie daje się rozsądnie dzielić, pierwszy pakiet realizuje całość, a drugi robi niezależny przegląd początkowego stanu lub wymagań z owned_paths: []. Taki przegląd nie obejmuje przyszłych edycji pierwszego wykonawcy. Nie twórz sztucznych zmian, aby zająć oba API.
Nie wykonuj implementacji. Zwróć tylko jeden kompletny obiekt JSON zgodny ze schematem: summary, contract i workers z dokładnie dwoma obiektami name, task, owned_paths, validation. Nie dodawaj komentarzy, Markdown ani treści poza JSON. Zachowaj zwięzłość planu bez pomijania kontraktu i kryteriów odbioru."""

DEFAULTS = {"permission_mode": "approval", "effort": "max", "main_prompt": MAIN_PROMPT,
            "coordinator_prompt": COORDINATOR_PROMPT, "context_window": 0, "compact_at": 0,
            "max_output_tokens": 0, "task_token_budget": 0, "max_agents": 6,
            "stream_retries": 10, "request_retries": 4, "rate_limit_wait": 180,
            "disabled_skills": [], "skill_overrides": []}


def preferences(store):
    saved = store.get_setting("preferences", {})
    result = {**copy.deepcopy(DEFAULTS), **copy.deepcopy(saved if isinstance(saved, dict) else {})}
    # Fill missing/corrupt fields in older settings without replacing an edited
    # prompt. An empty string is also an intentional, valid user preference.
    for key in ("main_prompt", "coordinator_prompt"):
        if not isinstance(result[key], str):
            result[key] = DEFAULTS[key]
    # Older panels allowed 100 retries. Keep saved prompts/options, while applying
    # the same bounded retry budget as the Rust gateway to old installations.
    for key in ("stream_retries", "request_retries"):
        value = result[key]
        result[key] = min(20, max(0, value)) if type(value) is int else DEFAULTS[key]
    return result


def validate_preferences(body, current):
    result = {**current}
    for key in ("main_prompt", "coordinator_prompt"):
        if key in body:
            if not isinstance(body[key], str) or len(body[key]) > 32000:
                raise ValueError("Prompt może zawierać do 32 000 znaków.")
            result[key] = body[key]
    for key, low, high in (("context_window", 0, 2000000), ("compact_at", 0, 2000000),
                           ("max_output_tokens", 0, 200000), ("task_token_budget", 0, 100000000),
                           ("max_agents", 1, 16), ("stream_retries", 0, 20), ("request_retries", 0, 20),
                           ("rate_limit_wait", 0, 900)):
        if key in body:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"Niepoprawna wartość {key}: od {low} do {high}.")
            result[key] = value
    if result["context_window"] and result["compact_at"] >= result["context_window"]:
        raise ValueError("Próg porządkowania kontekstu musi być mniejszy od okna kontekstu.")
    for key, choices in (("permission_mode", {"yolo", "approval"}), ("effort", {"low", "medium", "high", "xhigh", "max", "ultra"})):
        if key in body:
            if not isinstance(body[key], str) or body[key] not in choices:
                raise ValueError("Wybierz opcję dostępną w panelu.")
            result[key] = body[key]
    return result


def read_agents(project):
    path = Path(project["path"]) / "AGENTS.md"
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("AGENTS.md musi być zwykłym plikiem w folderze projektu.")
    if path.exists() and (not path.is_file() or path.stat().st_size > 128000):
        raise ValueError("AGENTS.md jest zbyt duży do edycji w panelu (128 kB).")
    raw = path.read_bytes() if path.exists() else b""
    return {"path": str(path), "exists": path.exists(), "content": raw.decode("utf-8-sig"),
            "version": hashlib.sha256(raw).hexdigest()}


def write_agents(project, content, version, backup_root):
    if not isinstance(content, str) or len(content.encode("utf-8")) > 128000:
        raise ValueError("AGENTS.md może zawierać do 128 kB tekstu.")
    previous = read_agents(project)
    if previous["version"] != version:
        raise ValueError("AGENTS.md zmienił się od otwarcia. Wczytaj aktualną wersję przed zapisem.")
    path = Path(previous["path"])
    if previous["exists"]:
        backup = Path(backup_root) / "agents-backups" / project["id"]
        backup.mkdir(parents=True, exist_ok=True)
        (backup / (secrets.token_hex(8) + ".md")).write_bytes(path.read_bytes())
    temp = path.with_name(".AGENTS-" + secrets.token_hex(5) + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return read_agents(project)
