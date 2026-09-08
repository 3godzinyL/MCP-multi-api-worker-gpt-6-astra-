"""Two real Codex workers, isolated source copies and a conflict-checked merge."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import secrets
import shutil
import stat
from pathlib import Path, PurePosixPath

from dashboard.changes import EXCLUDED, PRIVATE_SUFFIXES, PANEL_DATA, compare, snapshot, save_baseline, load_baseline

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "contract", "workers"],
    "properties": {
        "summary": {"type": "string"}, "contract": {"type": "string"},
        "workers": {"type": "array", "minItems": 2, "maxItems": 2, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "task", "owned_paths", "validation"],
            "properties": {"name": {"type": "string"}, "task": {"type": "string"},
                           "owned_paths": {"type": "array", "items": {"type": "string"}},
                           "validation": {"type": "string"}}
        }}
    }
}


def digest(path):
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError("Nieprawidłowy plik w kopii roboczej: " + str(path))
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def safe_path(root, relative):
    root = Path(root).resolve()
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or not name.parts or ":" in relative or "\\" in relative:
        raise ValueError("Nieprawidłowa ścieżka: " + str(relative))
    path = root.joinpath(*name.parts)
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Dowiązanie poza kopię roboczą: " + relative)
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.exists() and getattr(parent.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Dowiązania nie są łączone automatycznie: " + relative)
    return path


def manifest(root):
    result, size = {}, 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        kept = []
        for name in sorted(dirs):
            path = Path(folder) / name
            if name.lower() in EXCLUDED or path == PANEL_DATA or path.is_symlink():
                continue
            if getattr(path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = Path(folder) / name
            if name.lower().startswith(".env") or path.suffix.lower() in PRIVATE_SUFFIXES:
                continue
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            size += path.stat().st_size
            if len(result) >= 20000 or size > 512 * 1024 * 1024:
                raise ValueError("Kopia eksperymentalna przekracza 20 000 plików lub 512 MB źródeł. Użyj zwykłego trybu dla tego projektu.")
            result[relative] = digest(path)
    return result


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("workers"), list) or len(plan["workers"]) != 2:
        raise ValueError("Planista nie zwrócił planu dla dwóch wykonawców.")
    if any(not isinstance(plan.get(key), str) or not plan[key].strip() for key in ("summary", "contract")):
        raise ValueError("Plan musi zawierać podsumowanie i wspólny kontrakt zespołu.")
    # Do not partly normalize the persisted plan if a later entry is invalid.
    plan = copy.deepcopy(plan)
    for worker in plan["workers"]:
        if not isinstance(worker, dict) or any(not isinstance(worker.get(k), str) or not worker[k].strip()
                                              for k in ("name", "task", "validation")):
            raise ValueError("Niepełny opis wykonawcy w planie.")
        paths = worker.get("owned_paths")
        if not isinstance(paths, list) or len(paths) > 1000:
            raise ValueError("Niepoprawny przydział plików w planie.")
        normalized = []
        for path in paths:
            if not isinstance(path, str) or not path or any(x in path for x in ("*", "?", ":", "\\")) or any(ord(c) < 32 for c in path):
                raise ValueError("Plan musi wskazywać konkretne pliki lub katalogi, bez wildcardów.")
            p = PurePosixPath(path.rstrip("/"))
            if p.is_absolute() or ".." in p.parts or not p.parts or any(part.lower() in EXCLUDED for part in p.parts):
                raise ValueError("Nieprawidłowy przydział ścieżki: " + path)
            if any(part.lower().startswith(".env") for part in p.parts) or p.suffix.lower() in PRIVATE_SUFFIXES:
                raise ValueError("Plan przydziela prywatny plik pomijany w kopiach roboczych.")
            normalized.append(p.as_posix())
        worker["owned_paths"] = normalized
    for a in plan["workers"][0]["owned_paths"]:
        for b in plan["workers"][1]["owned_paths"]:
            a, b = a.casefold(), b.casefold()
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                raise ValueError("Plan przydziela ten sam plik lub katalog dwóm wykonawcom: " + a)
    return plan


def owns(relative, paths):
    name = relative.casefold()
    return any(name == p.casefold() or name.startswith(p.casefold().rstrip("/") + "/") for p in paths)


def prepare_copies(source, destination):
    base = manifest(source)
    destination.mkdir(parents=True, exist_ok=True)
    for role in ("worker1", "worker2"):
        root = destination / role
        root.mkdir()
        for name, expected in base.items():
            target = safe_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_path(source, name), target)
            if digest(target) != expected:
                raise ValueError("Projekt zmienił się podczas tworzenia kopii. Uruchom zadanie ponownie.")
    (destination / "baseline.json").write_text(json.dumps(base), encoding="utf-8")
    save_baseline(destination / "snapshot.json.gz", snapshot(destination / "worker1"))
    return base


def merge_copies(source, destination, plan):
    """Validate every edit and stage all bytes before touching the user's project."""
    base = json.loads((destination / "baseline.json").read_text(encoding="utf-8"))
    staged, changed_by = {}, {}
    for i, worker in enumerate(plan["workers"], 1):
        root = destination / ("worker" + str(i))
        current = manifest(root)
        for name in base.keys() | current.keys():
            if base.get(name) == current.get(name):
                continue
            if not owns(name, worker["owned_paths"]):
                raise ValueError(f"{worker['name']} zmienił plik poza swoim przydziałem: {name}. Kopie zachowano w {destination}.")
            folded = name.casefold()
            if folded in changed_by:
                raise ValueError("Kolizja między wykonawcami: " + name)
            changed_by[folded] = i
            target = safe_path(source, name)
            if digest(target) != base.get(name):
                raise ValueError("Projekt został zmieniony poza zespołem: " + name + ". Twoje pliki nie zostały nadpisane; kopie zachowano.")
            staged[name] = safe_path(root, name).read_bytes() if name in current else None
    backup = destination / "merge-backup"
    backup.mkdir(exist_ok=True)
    applied = []
    try:
        for name, raw in staged.items():
            target = safe_path(source, name)
            if digest(target) != base.get(name):
                raise ValueError("Plik zmienił się podczas łączenia: " + name)
            old = target.read_bytes() if target.exists() else None
            if old is not None:
                saved = safe_path(backup, name)
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(old)
            if raw is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(".3api-" + secrets.token_hex(5) + ".tmp")
                try:
                    temp.write_bytes(raw)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
            applied.append((name, old, hashlib.sha256(raw).hexdigest() if raw is not None else None))
    except Exception:
        for name, old, written in reversed(applied):
            target = safe_path(source, name)
            if digest(target) != written:
                continue  # Never overwrite a concurrent user's edit during rollback.
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old)
        raise
    return list(staged)


class ExperimentMixin:
    async def _lane(self, task, lane, prompt, *, effort, read_only=False, schema=None):
        tid, waiter = None, None
        try:
            if task.get("_stop_requested"):
                lane["state"] = "interrupted"
                return {"status": "interrupted"}
            lane["state"] = "starting"
            self.touch(task, save=True)
            await self.ensure_started()
            route_id = await self.setup_route(task, task["run_id"] + "-" + lane["id"], lane["api_ids"])
            approval, sandbox, policy = self.permissions(task, read_only=read_only)
            config = self.task_config(task, route_id)
            if read_only:
                config["features.multi_agent"] = False
            params = {"cwd": lane["path"], "model": task["model"], "modelProvider": self.model_provider,
                      "sandbox": sandbox, "approvalPolicy": approval, "config": config,
                      "developerInstructions": task["preferences"]["main_prompt"] + "\n\n" + lane["instructions"]}
            if lane.get("thread_id"):
                result = await self.request("thread/resume", {**params, "threadId": lane["thread_id"], "excludeTurns": True})
            else:
                result = await self.request("thread/start", params)
            tid = lane["thread_id"] = result["thread"]["id"]
            if lane["id"] == "planner":
                task["thread_id"] = tid
                task["agents"] = [a for a in task["agents"] if a["id"] != "main"]
            self.thread_tasks[tid] = task["id"]
            agent = self.agent(task, tid, status="running", name=lane["name"], description=effort.title() + " · " + lane["id"])
            agent["route_id"] = route_id
            if route_id:
                await self.setup_route(task, route_id, lane["api_ids"], thread_id=tid)
            lane["state"] = "running"
            lane["last_output"] = ""
            waiter = self._turn_waiters[tid] = asyncio.get_running_loop().create_future()
            self.touch(task, save=True)
            if task.get("_stop_requested"):
                lane["state"] = "interrupted"
                self.agent(task, tid, status="interrupted")
                return {"status": "interrupted"}
            arguments = {"threadId": tid, "input": [{"type": "text", "text": prompt}], "effort": effort,
                         "approvalPolicy": approval, "sandboxPolicy": policy, "clientUserMessageId": secrets.token_hex(12)}
            if schema:
                arguments["outputSchema"] = schema
            start = await self.request("turn/start", arguments)
            task["turn_id"] = start["turn"]["id"] if lane["id"] == "planner" else task.get("turn_id")
            if task.get("_stop_requested"):
                await self.request("turn/interrupt", {"threadId": tid, "turnId": start["turn"]["id"]})
            terminal = await waiter
            lane["state"] = terminal.get("status", "failed")
            error = terminal.get("error")
            if error:
                detail = error.get("message", "Błąd wykonawcy.") if isinstance(error, dict) else str(error)
                self.message(task, "lane-error-" + lane["id"], "error", lane["name"] + ": " + self.clean(detail))
            self.touch(task, save=True)
            return terminal
        except asyncio.CancelledError:
            lane["state"] = "interrupted"
            if tid:
                self.agent(task, tid, status="interrupted")
            raise
        except Exception:
            lane["state"] = "interrupted" if task.get("_stop_requested") else "failed"
            if tid:
                self.agent(task, tid, status=lane["state"])
            raise
        finally:
            if tid and self._turn_waiters.get(tid) is waiter:
                self._turn_waiters.pop(tid, None)
            self.touch(task, save=True)

    async def _run_experiment(self, task, project, prompt, user_id):
        try:
            await self.prepare_baseline(task, project)
            previous = task.get("experiment", {})
            resume = previous.get("phase") in {"workers", "merge"}
            if resume:
                exp = previous
                if (not isinstance(exp.get("path"), str) or not Path(exp["path"]).is_dir()
                        or not isinstance(exp.get("lanes"), list) or len(exp["lanes"]) != 3
                        or not exp.get("plan")):
                    raise ValueError("Nie można wznowić zespołu: brakuje zapisanych kopii lub przydziału. Historia została zachowana. Przywróć kopie albo rozpocznij nowe zadanie w nowym czacie.")
                plan = validate_plan(exp["plan"])
                destination = Path(exp["path"])
                ids = task["api_ids"][:3]
                exp["lanes"][0]["api_ids"] = ids
                for i, lane in enumerate(exp["lanes"][1:], 1):
                    lane["api_ids"] = [ids[i], ids[0], ids[3-i]]
                self.message(task, "resume-" + user_id, "tool", "Wznawiam te same wątki i kopie robocze. Zakończeni wykonawcy nie są uruchamiani ponownie.", "Kontynuacja zespołu")
            else:
                destination = self.data_dir / "worktrees" / task["id"] / secrets.token_hex(6)
                ids = task["api_ids"][:3]
                planner = {"id": "planner", "name": "Planista / rezerwa", "path": project["path"], "api_ids": ids,
                           "instructions": task["preferences"]["coordinator_prompt"]}
                exp = task["experiment"] = {"phase": "planning", "path": str(destination), "lanes": [planner], "original_prompt": prompt}
                self.message(task, "exp-plan-" + user_id, "tool", "Ultra analizuje projekt i dzieli zadanie. Następnie dwa API pracują równolegle na Ultra, a API planisty jest ich rezerwą przy limitach.", "Zespół eksperymentalny")
                plan_prompt = prompt + "\n\nPrzygotuj plan dla dwóch wykonawców. owned_paths: konkretne ścieżki względne, bez globów, bez '.' i bez nakładających się katalogów. Pusta lista = przegląd bez edycji. Zwróć JSON."
                for attempt in range(2):
                    terminal = await self._lane(task, planner, plan_prompt, effort="ultra", read_only=True, schema=PLAN_SCHEMA)
                    if terminal.get("status") != "completed":
                        self.finish(task, "interrupted" if task.get("_stop_requested") else "failed")
                        return
                    try:
                        raw = planner["last_output"].strip()
                        if raw.startswith("```"):
                            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                        plan = validate_plan(json.loads(raw))
                        break
                    except (ValueError, IndexError) as exc:
                        if attempt:
                            raise ValueError("Nie udało się ustalić rozłącznego planu: " + str(exc)) from None
                        plan_prompt = "Popraw plan. Błąd walidacji: " + str(exc) + ". Zwróć pełny poprawiony JSON."
                exp["plan"] = plan
                if task.get("_stop_requested"):
                    self.finish(task, "interrupted")
                    return
                await asyncio.to_thread(prepare_copies, Path(project["path"]), destination)
                exp["phase"] = "workers"
                for i, assignment in enumerate(plan["workers"], 1):
                    instructions = ("Pracujesz wyłącznie we własnej kopii projektu: " + str(destination / ("worker" + str(i)))
                        + ". Nie edytuj oryginału ani kopii drugiego wykonawcy. Wprowadzaj zmiany wyłącznie w: "
                        + (", ".join(assignment["owned_paths"]) or "BRAK — wykonaj przegląd bez zapisywania plików")
                        + ". Subagenci podlegają temu samemu przydziałowi. Nie modyfikuj innych manifestów/lockfile. "
                        + "Drugi wykonawca pracuje niezależnie na początkowej kopii; nie czekaj na jego edycje ani nie zastępuj jego pakietu własną implementacją. "
                        + "Zależności, build, cache i prywatne pliki pominięto podczas kopiowania; w razie potrzeby odtwórz zależności w swojej kopii. "
                        + "Jeśli potrzebujesz zmiany poza przydziałem, zgłoś dokładną blokadę i wymagany kontrakt, bez edycji tego pliku. "
                        + "Sprawdź własne zmiany i zakres diffu. Oddziel wyniki wykonanych testów od kontroli wymagających obu pakietów po scaleniu. "
                        + "Raport końcowy: wynik, zmienione pliki, rzeczywiste testy i wyniki, niesprawdzona integracja oraz blokady. "
                        + "Nie deklaruj ukończenia brakującej części; nie wstawiaj pozornie działających stubów zamiast wymaganego zachowania. "
                        + "Kontrakt zespołu:\n" + plan["contract"])
                    if not assignment["owned_paths"]:
                        instructions += ("\nTen pakiet jest przeglądem tylko do odczytu. Nie instaluj zależności ani nie uruchamiaj narzędzi zapisujących pliki. "
                                         "Oceniasz stan początkowy i wymagania, nie przyszłe zmiany drugiego wykonawcy. Podaj konkretne ustalenia i ograniczenia przeglądu.")
                    exp["lanes"].append({"id": "worker" + str(i), "name": assignment["name"],
                        "path": str(destination / ("worker" + str(i))), "api_ids": [ids[i], ids[0], ids[3-i]], "instructions": instructions})
                self.message(task, "exp-contract-" + user_id, "tool", plan.get("summary", "") + "\n\n" + plan.get("contract", ""), "Ustalony podział i kontrakt")
            task["state"] = "running"
            exp["phase"] = "workers"
            self.touch(task, save=True)
            jobs = []
            for lane, assignment in zip(exp["lanes"][1:], plan["workers"]):
                if resume and lane.get("state") == "completed":
                    continue
                instruction = ("Zadanie użytkownika:\n" + exp["original_prompt"] + "\n\nTwój pakiet:\n" + assignment["task"]
                               + "\n\nWalidacja:\n" + assignment["validation"])
                if resume:
                    instruction = "Kontynuuj na podstawie historii i zapisanych plików, nie powtarzaj wykonanych operacji. Dodatkowa wiadomość użytkownika:\n" + prompt + "\n\n" + instruction
                jobs.append(self._lane(task, lane, instruction, effort="ultra", read_only=not assignment["owned_paths"]))
            results = await asyncio.gather(*jobs, return_exceptions=True)
            if task.get("_stop_requested"):
                self.finish(task, "interrupted")
                return
            if any(isinstance(r, Exception) or r.get("status") != "completed" for r in results):
                for result in results:
                    if isinstance(result, Exception):
                        self.message(task, "worker-failure-" + secrets.token_hex(4), "error", self.clean(str(result)))
                raise ValueError("Nie wszyscy wykonawcy ukończyli pracę. Zachowano kopie i wątki; wyślij kontynuację, aby wznowić brakującą część.")
            exp["phase"] = "merge"
            self.touch(task, save=True)
            files = await asyncio.to_thread(merge_copies, Path(project["path"]), destination, plan)
            exp["phase"] = "completed"
            exp["merged_files"] = files
            self.message(task, "exp-complete-" + user_id, "assistant", f"Połączono pracę dwóch wykonawców: {len(files)} plików. Kopie robocze i kopia plików sprzed łączenia: {destination}. Wyniki testów wykonawców są w przebiegu powyżej.")
            self.finish(task, "completed")
        except asyncio.CancelledError:
            self.finish(task, "interrupted")
            raise
        except Exception as exc:
            self.fail(task, self.clean(str(exc), 3000))

    async def experiment_progress(self, task):
        exp = task.get("experiment", {})
        if exp.get("phase") not in {"workers", "merge"}:
            return None
        source = await asyncio.to_thread(load_baseline, Path(exp["path"]) / "snapshot.json.gz")
        changes, skipped, incomplete = [], 0, []
        for lane in exp.get("lanes", [])[1:]:
            result = compare(source, await asyncio.to_thread(snapshot, Path(lane["path"])))
            skipped += result.get("scan_skipped", 0)
            incomplete.extend(result.get("scan_incomplete_paths", []))
            for item in result["changes"]:
                item["worker"] = lane["name"]
                changes.append(item)
        return {"changes": changes, "files": len({c["path"] for c in changes}), "added": sum(c["added"] for c in changes),
                "removed": sum(c["removed"] for c in changes), "working_copies": True,
                "scan_skipped": skipped, "scan_incomplete_paths": incomplete}
