#!/usr/bin/env python3
"""Jira Toolkit - локальный помощник над Jira (прототип).

Запуск:   python jira_toolkit.py
Откроется страница http://127.0.0.1:8765 в браузере.

Модули:
  * Массовые изменения: вставьте список задач -> предпросмотр "было -> станет" -> запись -> журнал и откат.
  * Дашборд: настраиваемые карточки-метрики (JQL) с порогами, готовые наборы под роли.
  * Связанные задачи: выгрузка всех связанных с задачей запросов (дата, статус, поля вроде «Наименование клиента», КУС по ссылке из описания) с выгрузкой в CSV.
  * Структура эпика: дерево задач эпика, подзадач и связей со статусом, датами начала/завершения и временем в работе; выгрузка в CSV;
    раскладка по папкам (проект / тип / исполнитель / статус) и кнопка «Создать структуру в Jira» (плагин Structure, REST API) с теми же папками.
  * Задачи из анализа: страница системного анализа в Confluence -> черновики задач (анализ СТ и источников, реализация в DWH,
    доп. места доработки, одна задача на тестирование) по шаблону команды -> предпросмотр -> создание в Jira (по умолчанию AMLM).

Безопасность:
  * приложение слушает ТОЛЬКО 127.0.0.1 (доступно только с этого компьютера);
  * токен вводится в поле на странице, хранится только в памяти запущенного приложения,
    не пишется на диск, не логируется и никуда, кроме вашей Jira, не отправляется;
  * все запросы страницы защищены одноразовым ключом сессии и проверкой Host;
  * на диск (папка jira_toolkit_data) пишутся только настройки дашборда и журналы изменений - без токена.
Только стандартная библиотека Python.
"""
import argparse, json, os, secrets, ssl, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid, webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "jira_toolkit_data")
os.makedirs(DATA, exist_ok=True)
SECRET = secrets.token_urlsafe(24)
STATE = {"base": "", "token": "", "insecure": True, "user": "", "cbase": "", "ctoken": "", "cuser": ""}
RATE = {"extra": 0.0}
JOBS = {}
PC_CACHE = {}
PORT = [8765]

OPS = {
    "component_add": "Добавить компонент",
    "component_if_empty": "Поставить компонент, только если компонентов нет",
    "label_add": "Добавить метку",
    "label_remove": "Убрать метку",
    "comment_add": "Добавить комментарий",
}

# ---------------------------------------------------------------- Jira
class JiraError(Exception):
    def __init__(self, code, text):
        super().__init__(f"HTTP {code}: {str(text)[:300]}")
        self.code, self.text = code, str(text)

def jira(method, path, body=None, tries=6):
    if not STATE["token"]:
        raise JiraError(401, "Нет подключения к Jira")
    url = STATE["base"].rstrip("/") + path
    ctx = None
    if STATE["insecure"]:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", "Bearer " + STATE["token"])
        req.add_header("Accept", "application/json")
        req.add_header("X-Atlassian-Token", "no-check")
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode("utf-8")
        try:
            with urllib.request.urlopen(req, data=data, context=ctx, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if e.code == 429 and i < tries - 1:
                try: wait = float(e.headers.get("Retry-After") or 0)
                except ValueError: wait = 0
                wait = max(wait, min(5 * (2 ** i), 60))
                RATE["extra"] = min(RATE["extra"] + 0.5, 3.0)
                print(f"[429] лимит запросов Jira, жду {wait:.0f} с")
                last = JiraError(e.code, txt); time.sleep(wait); continue
            if e.code >= 500 and i < tries - 1:
                last = JiraError(e.code, txt); time.sleep(2 * (i + 1)); continue
            raise JiraError(e.code, txt)
        except urllib.error.URLError as e:
            if i < tries - 1:
                last = JiraError(0, str(e)); time.sleep(2 * (i + 1)); continue
            raise JiraError(0, str(e))
    raise last

def search(jql, fields, max_results=50, start_at=0):
    return jira("POST", "/rest/api/2/search", {"jql": jql, "fields": fields, "maxResults": max_results, "startAt": start_at})

def fetch_issues(keys):
    """Текущие значения для списка задач. Возвращает {KEY: {...}}; отсутствующих задач в словаре нет."""
    out = {}
    fields = ["summary", "status", "components", "labels"]
    def put(it):
        f = it["fields"]
        out[it["key"].upper()] = {"summary": f.get("summary", ""), "status": (f.get("status") or {}).get("name", ""),
                                  "components": [c["name"] for c in f.get("components") or []], "labels": list(f.get("labels") or [])}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            data = search("key in (" + ",".join(chunk) + ")", fields, 100)
            for it in data.get("issues", []): put(it)
        except JiraError as e:
            if e.code != 400: raise
            for k in chunk:            # в пачке есть несуществующий ключ - читаем по одному
                try: put(jira("GET", f"/rest/api/2/issue/{k}?fields=" + ",".join(fields)))
                except JiraError as e2:
                    if e2.code not in (400, 404): raise
        time.sleep(0.2)
    return out

def project_components(pk):
    if pk not in PC_CACHE:
        try: PC_CACHE[pk] = {c["name"] for c in jira("GET", f"/rest/api/2/project/{pk}/components")}
        except JiraError: PC_CACHE[pk] = None
    return PC_CACHE[pk]

# ---------------------------------------------------------------- Массовые изменения
def plan_rows(rows):
    keys = []
    for r in rows:
        r["key"] = str(r.get("key", "")).strip().upper()
        if r["key"] and r["key"] not in keys: keys.append(r["key"])
    cur = fetch_issues(keys) if keys else {}
    out = []
    for r in rows:
        key, op, val = r["key"], r.get("op", ""), str(r.get("value", "")).strip()
        item = {"key": key, "op": op, "value": val, "summary": "", "current": "", "action": "skip", "reason": ""}
        if op not in OPS: item.update(action="error", reason="неизвестная операция")
        elif not key: item.update(action="error", reason="пустой ключ")
        elif not val and op != "component_add" or not val:
            item.update(action="error", reason="не указано значение")
        elif key not in cur: item.update(action="error", reason="задача не найдена или нет доступа")
        else:
            c = cur[key]; item["summary"] = c["summary"]
            if op.startswith("component"):
                item["current"] = ", ".join(c["components"]) or "(нет)"
                known = project_components(key.split("-")[0])
                if known is not None and val not in known:
                    item.update(action="error", reason="такого компонента нет в проекте")
                elif op == "component_add" and val in c["components"]:
                    item.update(reason="уже есть")
                elif op == "component_if_empty" and c["components"]:
                    item.update(reason="компонент уже стоит: " + ", ".join(c["components"]))
                else: item.update(action="apply", reason="будет записано")
            elif op.startswith("label"):
                item["current"] = ", ".join(c["labels"]) or "(нет)"
                if op == "label_add" and val in c["labels"]: item.update(reason="метка уже есть")
                elif op == "label_remove" and val not in c["labels"]: item.update(reason="такой метки нет")
                else: item.update(action="apply", reason="будет записано")
            else:
                item.update(action="apply", reason="будет добавлен комментарий")
        out.append(item)
    return out

def new_job(kind, total):
    jid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    JOBS[jid] = {"id": jid, "kind": kind, "total": total, "done": 0, "failed": 0, "status": "running", "cancel": False, "results": [], "error": ""}
    return JOBS[jid]

def do_change(row):
    key, op, val = row["key"], row["op"], row["value"]
    if op.startswith("component"):
        jira("PUT", f"/rest/api/2/issue/{key}", {"update": {"components": [{"add": {"name": val}}]}}); return {}
    if op == "label_add":
        jira("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [{"add": val}]}}); return {}
    if op == "label_remove":
        jira("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [{"remove": val}]}}); return {}
    if op == "comment_add":
        c = jira("POST", f"/rest/api/2/issue/{key}/comment", {"body": val}); return {"comment_id": c.get("id")}

def undo_change(e):
    key, op, val = e["key"], e["op"], e["value"]
    if op.startswith("component"):
        jira("PUT", f"/rest/api/2/issue/{key}", {"update": {"components": [{"remove": {"name": val}}]}})
    elif op == "label_add":
        jira("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [{"remove": val}]}})
    elif op == "label_remove":
        jira("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [{"add": val}]}})
    elif op == "comment_add" and e.get("comment_id"):
        jira("DELETE", f"/rest/api/2/issue/{key}/comment/{e['comment_id']}")

def run_apply(job, rows, pause):
    try:
        plan = plan_rows(rows)                 # всегда пересчитываем: данным страницы не доверяем
        todo = [p for p in plan if p["action"] == "apply"]
        for p in plan:
            if p["action"] != "apply":
                job["results"].append({**p, "result": "пропущено" if p["action"] == "skip" else "ошибка", "details": p["reason"]})
                if p["action"] == "error": job["failed"] += 1
        job["total"] = len(plan); job["done"] = len(plan) - len(todo)
        written = []
        for p in todo:
            if job["cancel"]:
                job["status"] = "cancelled"; break
            try:
                extra = do_change(p)
                written.append({**p, **extra})
            except JiraError as e:
                job["failed"] += 1; job["results"].append({**p, "result": "ошибка", "details": str(e)})
            job["done"] += 1
            time.sleep(pause + RATE["extra"])
        chk = {}
        vk = [w["key"] for w in written if w["op"] != "comment_add"]
        if vk:
            try: chk = fetch_issues(list(dict.fromkeys(vk)))
            except JiraError: chk = None
        journal = []
        for w in written:
            ok, note = True, "записано"
            if w["op"] != "comment_add" and chk is not None:
                c = chk.get(w["key"], {"components": [], "labels": []})
                if w["op"].startswith("component"): ok = w["value"] in c["components"]
                elif w["op"] == "label_add": ok = w["value"] in c["labels"]
                else: ok = w["value"] not in c["labels"]
                note = "записано, проверено" if ok else "запрос прошёл, но изменения нет"
            if not ok: job["failed"] += 1
            job["results"].append({**w, "result": "записано" if ok else "ошибка", "details": note})
            if ok: journal.append({k: w.get(k) for k in ("key", "op", "value", "comment_id")})
        with open(os.path.join(DATA, f"journal_{job['id']}.json"), "w", encoding="utf-8") as f:
            json.dump({"id": job["id"], "created": time.strftime("%Y-%m-%d %H:%M:%S"), "entries": journal}, f, ensure_ascii=False, indent=1)
        if job["status"] == "running": job["status"] = "done"
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)

def run_rollback(job, jid):
    try:
        with open(os.path.join(DATA, f"journal_{jid}.json"), encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        job["total"] = len(entries)
        for e in entries:
            if job["cancel"]:
                job["status"] = "cancelled"; break
            try:
                undo_change(e); job["results"].append({**e, "result": "откат выполнен", "details": ""})
            except JiraError as ex:
                job["failed"] += 1; job["results"].append({**e, "result": "ошибка", "details": str(ex)})
            job["done"] += 1; time.sleep(0.3 + RATE["extra"])
        if job["status"] == "running": job["status"] = "done"
    except Exception as ex:
        job["status"] = "error"; job["error"] = str(ex)

# ---------------------------------------------------------------- Дашборд
def default_dash():
    c = lambda n, j, w=0, k=0: {"name": n, "jql": j, "warn": w, "crit": k}
    return {"role": "Владелец продукта", "project": "", "presets": {
        "Владелец продукта": [
            c("Мои открытые задачи", "assignee = currentUser() AND resolution = Unresolved"),
            c("Без исполнителя (проект)", "project = {PROJECT} AND assignee is EMPTY AND resolution = Unresolved", 5, 15),
            c("Просрочено", "project = {PROJECT} AND due < now() AND resolution = Unresolved", 1, 5),
            c("Без компонента за 30 дней", "project = {PROJECT} AND component is EMPTY AND created >= -30d", 5, 20),
            c("Создано за 7 дней", "project = {PROJECT} AND created >= -7d"),
            c("Закрыто за 7 дней", "project = {PROJECT} AND resolved >= -7d")],
        "Аналитик": [
            c("Мои открытые задачи", "assignee = currentUser() AND resolution = Unresolved"),
            c("Я автор, не решены", "reporter = currentUser() AND resolution = Unresolved"),
            c("Обновлены за 2 дня", "project = {PROJECT} AND assignee = currentUser() AND updated >= -2d"),
            c("Без описания", "project = {PROJECT} AND description is EMPTY AND resolution = Unresolved", 3, 10)],
        "Тестировщик": [
            c("Открытые баги", "project = {PROJECT} AND issuetype = Bug AND resolution = Unresolved", 10, 30),
            c("Баги за 7 дней", "project = {PROJECT} AND issuetype = Bug AND created >= -7d", 5, 15),
            c("Мои открытые задачи", "assignee = currentUser() AND resolution = Unresolved"),
            c("Просрочено", "project = {PROJECT} AND due < now() AND resolution = Unresolved", 1, 5)]}}

def dash_path(): return os.path.join(DATA, "dashboards.json")

def load_dash():
    try:
        with open(dash_path(), encoding="utf-8") as f: return json.load(f)
    except Exception: return default_dash()

def run_cards(cards, project):
    out = []
    for c in cards:
        jql = str(c.get("jql", "")).strip()
        res = {"name": c.get("name", ""), "jql": jql, "total": None, "error": "", "link": ""}
        if "{PROJECT}" in jql:
            if not project: res["error"] = "укажите ключ проекта"; out.append(res); continue
            jql = jql.replace("{PROJECT}", project)
        res["jql"] = jql
        try:
            res["total"] = search(jql, ["key"], 0).get("total", 0)
            res["link"] = STATE["base"].rstrip("/") + "/issues/?jql=" + urllib.parse.quote(jql)
        except JiraError as e: res["error"] = str(e)[:160]
        out.append(res); time.sleep(0.2 + RATE["extra"])
    return out

# ---------------------------------------------------------------- Структура эпика
CAT_RU = {"new": "К выполнению", "indeterminate": "В работе", "done": "Готово"}
TREE_FIELDS = ["summary", "project", "status", "issuetype", "assignee", "created", "resolutiondate", "duedate", "issuelinks", "subtasks"]
STATUS_CAT = {}
STATUS_ID_CAT = {}
HIST_WORK = set()
EPIC_FORM = {"jql": None}
KEY_RE = None

def parse_dt(s):
    if not s: return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try: return datetime.strptime(s, fmt)
        except ValueError: pass
    try: return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError: return None

def load_status_cats():
    if STATUS_CAT: return
    try:
        for s in jira("GET", "/rest/api/2/status", tries=2):
            STATUS_CAT[s["name"]] = (s.get("statusCategory") or {}).get("key", "new")
            if s.get("id") is not None: STATUS_ID_CAT[str(s["id"])] = STATUS_CAT[s["name"]]
    except Exception: pass

def structure_days_formula():
    """Формула для столбца Structure «дней в работе». Начало: первый переход в статус категории «В работе»;
    если задачу сразу создали в таком статусе (перехода в истории нет), то дата создания. Конец: решение или сегодня."""
    names = sorted({n for n, c in STATUS_CAT.items() if c == "indeterminate"} | HIST_WORK)
    if not names: return ""
    cond = " OR ".join('s = "' + n.replace('"', '\\"') + '"' for n in names)
    return ("WITH isWork(s) = " + cond + ":\n"
            "WITH moved = history.changes.FILTER($.field = \"status\" AND isWork($.to)).FIRST().changeGroup.time:\n"
            "WITH start = IF moved : moved ELSE IF isWork(status) : created:\n"
            "WITH finish = IF resolved : resolved ELSE NOW():\n"
            "IF start : DAYS_BETWEEN(MIN(start, finish), finish)")

def fetch_tree_issues(keys):
    out = {}
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        try:
            data = jira("POST", "/rest/api/2/search", {"jql": "key in (" + ",".join(chunk) + ")", "fields": TREE_FIELDS, "expand": ["changelog"], "maxResults": 50})
            for it in data.get("issues", []): out[it["key"].upper()] = it
        except JiraError as e:
            if e.code != 400: raise
            for k in chunk:
                try: out[k.upper()] = jira("GET", f"/rest/api/2/issue/{k}?expand=changelog&fields=" + ",".join(TREE_FIELDS))
                except JiraError as e2:
                    if e2.code not in (400, 404): raise
        time.sleep(0.2 + RATE["extra"])
    return out

def epic_children(epic):
    forms = [EPIC_FORM["jql"]] if EPIC_FORM["jql"] else ['"Epic Link" = {K}', 'parentEpic = {K}', 'parent = {K}']
    for form in forms:
        keys, start, ok = [], 0, True
        while True:
            try: d = jira("POST", "/rest/api/2/search", {"jql": form.replace("{K}", epic), "fields": ["key"], "maxResults": 100, "startAt": start})
            except JiraError as e:
                if e.code == 400: ok = False; break
                raise
            keys += [i["key"].upper() for i in d.get("issues", [])]
            start += 100
            if start >= d.get("total", 0): break
        if ok:
            EPIC_FORM["jql"] = form; return keys
    return []

def make_node(it, rel):
    f = it["fields"]; st = f.get("status") or {}
    cat = (st.get("statusCategory") or {}).get("key") or STATUS_CAT.get(st.get("name"), "new")
    created, resolved = parse_dt(f.get("created")), parse_dt(f.get("resolutiondate"))
    floor = datetime.min.replace(tzinfo=timezone.utc)
    hist = sorted(((parse_dt(h.get("created")) or floor, h) for h in (it.get("changelog") or {}).get("histories", [])), key=lambda x: x[0])
    start, approx = None, False
    for ts, h in hist:
        for ch in h.get("items", []):
            if ch.get("field") != "status": continue
            tocat = STATUS_ID_CAT.get(str(ch.get("to") or "")) or STATUS_CAT.get(ch.get("toString"))
            if tocat == "indeterminate" and ch.get("toString"): HIST_WORK.add(ch["toString"])
            if (STATUS_CAT and tocat == "indeterminate") or (not STATUS_CAT and start is None):
                start = start or ts
    if start is None and cat != "new" and created: start, approx = created, True
    end = (resolved or (hist[-1][0] if hist else None)) if cat == "done" else None
    days = None
    if start:
        days = max(0.0, round(((end or datetime.now(timezone.utc)) - start).total_seconds() / 86400, 1))
    due = f.get("duedate")
    overdue = bool(due and cat != "done" and due < time.strftime("%Y-%m-%d"))
    d = lambda x: x.strftime("%Y-%m-%d") if x else ""
    pr = f.get("project") or {}
    return {"key": it["key"].upper(), "id": str(it.get("id", "")), "summary": f.get("summary", ""), "type": (f.get("issuetype") or {}).get("name", ""),
            "project": pr.get("key") or it["key"].upper().split("-")[0], "project_name": pr.get("name", ""),
            "status": st.get("name", ""), "cat": cat, "assignee": (f.get("assignee") or {}).get("displayName", ""),
            "created": d(created), "start": d(start), "start_approx": approx, "end": d(end), "days": days, "due": due or "",
            "overdue": overdue, "rel": rel}

def run_tree(job, root, depth, include_links, max_issues, exclude=frozenset()):
    try:
        load_status_cats()
        job["msg"] = "Читаю корневую задачу"
        raw = fetch_tree_issues([root])
        if root not in raw: raise JiraError(404, "Задача не найдена или нет доступа")
        nodes = {root: make_node(raw[root], "")}
        children = {root: []}; frontier = [root]; truncated = False
        for level in range(1, depth + 1):
            if job["cancel"]: job["status"] = "cancelled"; return
            job["msg"] = f"Уровень {level}: ищу связанные задачи"
            edges = []
            for pk in frontier:
                f = raw[pk]["fields"]
                for s in f.get("subtasks") or []: edges.append((pk, s["key"].upper(), "подзадача"))
                if include_links:
                    for l in f.get("issuelinks") or []:
                        t = l.get("type", {})
                        if "outwardIssue" in l: edges.append((pk, l["outwardIssue"]["key"].upper(), t.get("outward") or t.get("name") or "связана"))
                        elif "inwardIssue" in l: edges.append((pk, l["inwardIssue"]["key"].upper(), t.get("inward") or t.get("name") or "связана"))
                if (f.get("issuetype") or {}).get("name", "").lower() in ("epic", "эпик") or pk == root:
                    for ck in epic_children(pk): edges.append((pk, ck, "в эпике"))
            seen, fresh = set(), []
            for pk, ck, rel in edges:
                if ck in nodes or ck in seen or ck in exclude: continue
                if len(nodes) + len(fresh) >= max_issues: truncated = True; continue
                seen.add(ck); fresh.append((pk, ck, rel))
            if not fresh: break
            job["msg"] = f"Уровень {level}: читаю {len(fresh)} задач"
            got = fetch_tree_issues([ck for _, ck, _ in fresh])
            frontier = []
            for pk, ck, rel in fresh:
                if ck in got:
                    raw[ck] = got[ck]; nodes[ck] = make_node(got[ck], rel); frontier.append(ck)
                else:
                    nodes[ck] = {"key": ck, "id": "", "summary": "(нет доступа или не найдена)", "type": "", "project": ck.split("-")[0], "project_name": "", "status": "нет доступа", "cat": "none", "assignee": "",
                                 "created": "", "start": "", "start_approx": False, "end": "", "days": None, "due": "", "overdue": False, "rel": rel}
                children.setdefault(pk, []).append(ck); children.setdefault(ck, [])
            job["done"] = len(nodes)
        flat = []
        def walk(k, lvl, pid):
            idx = len(flat); n = dict(nodes[k]); n["level"] = lvl; n["pid"] = pid; n["has_children"] = bool(children.get(k)); flat.append(n)
            for c in children.get(k, []): walk(c, lvl + 1, idx)
        walk(root, 0, -1)
        real = [n for n in flat if n["cat"] != "none"]
        cnt = {c: sum(1 for n in real if n["cat"] == c) for c in ("new", "indeterminate", "done")}
        starts = [n["start"] for n in real if n["start"]]; ends = [n["end"] for n in real if n["end"]]
        done_days = [n["days"] for n in real if n["cat"] == "done" and n["days"] is not None]
        job["tree"] = {"base": STATE["base"], "root": root, "nodes": flat, "truncated": truncated, "depth": depth,
                       "summary": {"total": len(flat), "new": cnt["new"], "indeterminate": cnt["indeterminate"], "done": cnt["done"],
                                   "first_start": min(starts) if starts else "", "last_end": max(ends) if (ends and cnt["done"] == len(real)) else "",
                                   "overdue": sum(1 for n in real if n["overdue"]), "avg_done_days": round(sum(done_days) / len(done_days), 1) if done_days else None,
                                   "no_access": len(flat) - len(real), "epic_query": EPIC_FORM["jql"]},
                       "work_formula": structure_days_formula()}
        job["done"] = len(flat); job["status"] = "done"
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)

# ---------------------------------------------------------------- Связанные задачи (выгрузка с КУС)
import re as _re_links
FIELD_CACHE = {}
CUS_RE = _re_links.compile(r"(?<![A-Za-z0-9_])([A-Z0-9]{6})(?![A-Za-z0-9_])")
ROLE_RE = _re_links.compile(r"(?P<partner>партн[её]р)|(?P<link>неактуальн|некорректн|ссылк|ссылоч)|"
                            r"(?P<pos>(?<![а-яё])пос(?:т)?(?![а-яё])|(?<![a-z])pos(?![a-z])|пластик|карт|актуальн|активац|активцаи|действующ|корректн)")
DASH_ROLE_RE = _re_links.compile(r"^\s*[-–—:]\s*(?:тут\s+)?(\S+)")

def all_fields():
    if not FIELD_CACHE:
        for f in jira("GET", "/rest/api/2/field"):
            FIELD_CACHE[f["id"]] = f.get("name", f["id"])
    return FIELD_CACHE

def resolve_fields(names):
    """Имена или id полей -> [(id, имя)], [не найдено]."""
    fl = all_fields(); by_name = {}
    for fid, nm in fl.items(): by_name.setdefault(nm.strip().lower(), fid)
    out, missing = [], []
    for n in names:
        n = n.strip()
        if not n: continue
        if n in fl: out.append((n, fl[n]))
        elif n.lower() in by_name: fid = by_name[n.lower()]; out.append((fid, fl[fid]))
        else: missing.append(n)
    return out, missing

def field_text(v):
    if v is None: return ""
    if isinstance(v, list): return ", ".join(x for x in (field_text(i) for i in v) if x)
    if isinstance(v, dict):
        for k in ("value", "displayName", "name", "key"):
            if v.get(k) is not None: return str(v[k])
        return json.dumps(v, ensure_ascii=False)
    return str(v)

def cus_tokens(text):
    return [m for m in CUS_RE.findall(text or "") if any(ch.isalpha() for ch in m)]

def _role(seg, last):
    ms = list(ROLE_RE.finditer(seg.lower()))
    if not ms: return ""
    return (ms[-1] if last else ms[0]).lastgroup

def parse_link_cus(desc, known):
    """КУС по ссылке из свободного текста описания. known — КУС клиента из поля (исключаются).
    Возвращает (кус, уверенно: bool, все найденные кандидаты)."""
    known = {k.upper() for k in known}
    cands = []
    for line in (desc or "").replace("{{", " ").replace("}}", " ").replace("*", " ").replace("`", " ").splitlines():
        toks = [(m.start(1), m.end(1), m.group(1)) for m in CUS_RE.finditer(line) if any(ch.isalpha() for ch in m.group(1))]
        for i, (a, b, t) in enumerate(toks):
            pre = line[toks[i - 1][1] if i else 0:a]
            post = line[b:toks[i + 1][0] if i + 1 < len(toks) else len(line)]
            m = DASH_ROLE_RE.match(post)
            role = _role(m.group(1), False) if m else ""
            role = role or _role(pre, True) or _role(post, False)
            cands.append((t, role))
    other = []
    for t, r in cands:
        if t in known or r == "partner": continue
        if t not in [o[0] for o in other]: other.append((t, r))
        elif r == "link": other = [(o[0], "link" if o[0] == t else o[1]) for o in other]
    links = [t for t, r in other if r == "link"]
    if len(links) == 1: return links[0], True, [t for t, _ in other]
    if links: return links[0], False, [t for t, _ in other]
    if other: return other[0][0], False, [t for t, _ in other]
    return "", False, []

def run_links(job, root, jql_extra, field_names, parse_desc, max_issues):
    try:
        job["msg"] = "Читаю поля Jira"
        fields, missing = resolve_fields(field_names)
        job["msg"] = "Читаю исходную задачу"
        rt = jira("GET", f"/rest/api/2/issue/{root}?fields=summary,issuelinks")
        rel = {}
        for l in rt["fields"].get("issuelinks") or []:
            t = l.get("type", {})
            if "outwardIssue" in l: rel[l["outwardIssue"]["key"].upper()] = t.get("outward") or t.get("name") or ""
            elif "inwardIssue" in l: rel[l["inwardIssue"]["key"].upper()] = t.get("inward") or t.get("name") or ""
        jql = f'issue in linkedIssues("{root}")'
        if jql_extra: jql += f" AND ({jql_extra})"
        jql += " ORDER BY created ASC"
        want = ["summary", "status", "created", "issuetype", "assignee", "description"] + [f for f, _ in fields]
        issues, start, total = [], 0, None
        while True:
            if job["cancel"]: job["status"] = "cancelled"; return
            d = jira("POST", "/rest/api/2/search", {"jql": jql, "fields": want, "maxResults": 100, "startAt": start})
            total = d.get("total", 0); job["total"] = min(total, max_issues)
            issues += d.get("issues", []); start += 100; job["done"] = len(issues)
            job["msg"] = f"Читаю задачи: {len(issues)} из {total}"
            if start >= total or len(issues) >= max_issues: break
            time.sleep(0.2 + RATE["extra"])
        truncated = total > len(issues[:max_issues]); issues = issues[:max_issues]
        rows = []
        for it in issues:
            f = it["fields"]; st = f.get("status") or {}
            vals = [field_text(f.get(fid)) for fid, _ in fields]
            known = [t for v in vals for t in cus_tokens(v)]
            lc, sure, cands = parse_link_cus(f.get("description") or "", known) if parse_desc else ("", True, [])
            c = parse_dt(f.get("created"))
            rows.append({"key": it["key"].upper(), "summary": f.get("summary", ""), "type": (f.get("issuetype") or {}).get("name", ""),
                         "status": st.get("name", ""), "cat": (st.get("statusCategory") or {}).get("key", "new"),
                         "assignee": (f.get("assignee") or {}).get("displayName", ""),
                         "created": c.astimezone().strftime("%Y-%m-%d %H:%M") if c else "", "rel": rel.get(it["key"].upper(), ""),
                         "fields": vals, "link_cus": lc, "check": bool(parse_desc and lc and not sure), "cands": cands,
                         "description": (f.get("description") or "")[:4000]})
        job["links"] = {"base": STATE["base"], "root": root, "root_summary": rt["fields"].get("summary", ""), "jql": jql,
                        "fields": [nm for _, nm in fields], "missing": missing, "parse_desc": parse_desc,
                        "rows": rows, "total": total, "truncated": truncated}
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"; job["error"] = str(e)

# ---------------------------------------------------------------- Создание структуры в Jira (плагин Structure)
STRUCT_PREFIX = {}

def struct_call(kind, method, suffix, body=None):
    """Вызов REST плагина Structure. Путь зависит от версии плагина, поэтому пробуем оба известных варианта."""
    cands = [STRUCT_PREFIX[kind]] if kind in STRUCT_PREFIX else ["/rest/structure/2.0", "/rest/plugins/structure/2.0"]
    last = None
    for pre in cands:
        try:
            r = jira(method, pre + suffix, body, tries=3)
            STRUCT_PREFIX[kind] = pre
            return r
        except JiraError as e:
            if e.code == 404 and len(cands) > 1:
                last = e; continue
            raise
    raise last

def forest_latest(sid):
    spec = urllib.parse.quote(json.dumps({"structureId": sid}, separators=(",", ":")))
    return struct_call("forest", "GET", "/forest/latest?s=" + spec)

def parse_forest(formula):
    """Строки леса Structure: [(rowId, глубина, элемент)]. Элемент - id задачи или «тип/id» (в типе бывает двоеточие)."""
    out = []
    for part in (formula or "").split(","):
        p = part.strip().split(":", 2)
        if len(p) == 3:
            try: out.append((int(p[0]), int(p[1]), p[2]))
            except ValueError: pass
    return out

def parse_formula(formula):
    return [(it, d) for _, d, it in parse_forest(formula)]

FOLDER_TYPE = "com.almworks.jira.structure:type-folder"

def is_folder_item(item):
    return not str(item).isdigit() and "folder" in str(item).lower()

def layout_from_tree(tree, exclude):
    """Старый режим (без папок): дерево из сборки минус убранные пользователем задачи."""
    nodes = tree["nodes"]; drop, out = [], []
    for i, n in enumerate(nodes):
        drop.append(n["pid"] >= 0 and (n["key"] in exclude or drop[n["pid"]]))
    for i, n in enumerate(nodes):
        if not drop[i]: out.append({"k": n["key"], "l": n["level"]})
    return out

def drop_subtrees(ent, bad):
    """Убирает строки с индексами из bad вместе со всем, что под ними."""
    out, cut = [], None
    for i, e in enumerate(ent):
        if cut is not None and e["l"] > cut: continue
        cut = None
        if i in bad: cut = e["l"]; continue
        out.append(e)
    return out

def normalize_layout(layout, tree):
    """Проверяет раскладку от страницы: только задачи из сборки, корректные уровни, без пустых папок."""
    byk = {n["key"]: n for n in tree["nodes"]}
    if not isinstance(layout, list) or not layout: raise JiraError(400, "Пустая раскладка структуры")
    if len(layout) > 3000: raise JiraError(400, "Слишком много строк в раскладке")
    ent, skipped, prev = [], [], -1
    for x in layout:
        if not isinstance(x, dict): raise JiraError(400, "Некорректная раскладка структуры")
        try: lvl = int(x.get("l"))
        except (TypeError, ValueError): raise JiraError(400, "Некорректный уровень в раскладке")
        if lvl < 0 or lvl > 30 or lvl > prev + 1: raise JiraError(400, "Некорректная вложенность в раскладке")
        prev = lvl
        if "f" in x:
            nm = " ".join(str(x.get("f") or "").split())[:200]
            if not nm: raise JiraError(400, "Пустое название папки")
            ent.append({"kind": "folder", "name": nm, "l": lvl})
        else:
            k = str(x.get("k") or "").strip().upper()
            if k not in byk: raise JiraError(400, f"Задачи {k} нет в собранной структуре")
            ent.append({"kind": "issue", "key": k, "id": str(byk[k].get("id", "")), "l": lvl})
    if ent[0]["l"] != 0: raise JiraError(400, "Раскладка должна начинаться с верхнего уровня")
    bad = {i for i, e in enumerate(ent) if e["kind"] == "issue" and not e["id"].isdigit()}
    skipped = [ent[i]["key"] for i in sorted(bad)]
    ent = drop_subtrees(ent, bad)
    while True:   # папки, под которыми не осталось ни одной задачи
        empty = set()
        for i, e in enumerate(ent):
            if e["kind"] != "folder": continue
            j, has = i + 1, False
            while j < len(ent) and ent[j]["l"] > e["l"]:
                if ent[j]["kind"] == "issue": has = True; break
                j += 1
            if not has: empty.add(i)
        if not empty: break
        ent = drop_subtrees(ent, empty)
    return ent, skipped

def flatten_folders(ent):
    """Запасной вариант, если папки создать нельзя: убираем папки, их содержимое поднимаем на уровень выше."""
    out, stack = [], []
    for e in ent:
        while stack and stack[-1] >= e["l"]: stack.pop()
        if e["kind"] == "folder": stack.append(e["l"]); continue
        c = dict(e); c["l"] = e["l"] - len(stack); out.append(c)
    return out

def create_folder_row(sid, name, under, after, version):
    """Создаёт папку в структуре (так же, как кнопка «+ Папка» в интерфейсе Structure)."""
    base = {"item": {"type": FOLDER_TYPE, "values": {"summary": name}},
            "forest": {"spec": {"structureId": sid}, "version": version},
            "rowId": -1, "under": under, "after": after, "before": 0, "parameters": {}}
    v1 = dict(base); v1["items"] = {"version": {"signature": 0, "version": 0}}
    last = None
    for body in (v1, base):
        try: return struct_call("structure", "POST", "/item/create", body)
        except JiraError as e:
            last = e
            if e.code not in (400, 404, 409, 415, 500): raise
    raise last

def build_forest(sid, ent, result):
    """Добавляет строки по порядку (обход дерева сверху вниз): каждая новая строка оказывается в конце леса,
    поэтому её rowId - строка с тем же номером в актуальном лесе. Подряд идущие задачи добавляются одним запросом."""
    rows = {}; i = 0; folders_ok = None
    def place(i):
        L = ent[i]["l"]; parent, after = 0, 0
        for j in range(i - 1, -1, -1):
            if ent[j]["l"] < L: parent = rows[j]; break
            if ent[j]["l"] == L and not after: after = rows[j]
        return parent, after
    def sync(upto):
        cur = forest_latest(sid); got = parse_forest(cur.get("formula"))
        if len(got) < upto: raise JiraError(500, f"после добавления в структуре {len(got)} строк вместо {upto}")
        for j in range(upto):
            if got[j][1] != ent[j]["l"]: raise JiraError(500, f"строка {j + 1}: уровень {got[j][1]} вместо {ent[j]['l']}")
            rows[j] = got[j][0]
        return cur
    cur = forest_latest(sid)
    while i < len(ent):
        e = ent[i]; parent, after = place(i)
        if e["kind"] == "folder":
            try:
                create_folder_row(sid, e["name"], parent, after, cur.get("version"))
                cur = sync(i + 1)
                if not is_folder_item(parse_forest(cur.get("formula"))[i][2]):
                    raise JiraError(500, "вместо папки появился другой элемент")
                folders_ok = True; i += 1; continue
            except JiraError as err:
                if folders_ok: raise          # первая папка создалась, следующая нет - это уже не «не поддерживается»
                result["warnings"].append("Папки создать не удалось (" + str(err)[:200] + "). Задачи добавлены без папок, "
                                          "но сгруппированы подряд. Пришлите этот текст - поправим под вашу версию Structure.")
                # до первой папки строки уже в Jira и папок среди них нет; остальное «сплющиваем» и продолжаем
                ent[i:] = flatten_folders(ent[i:])
                folders_ok = False
                cur = forest_latest(sid)
                continue
        L = e["l"]; j = i
        while j < len(ent) and ent[j]["kind"] == "issue" and ent[j]["l"] >= L: j += 1
        formula = ",".join(f"{-(100 + k)}:{ent[k]['l'] - L}:{ent[k]['id']}" for k in range(i, j))
        act = {"action": "add", "under": parent, "after": after, "forest": formula}
        struct_call("forest", "POST", "/forest/update", {"spec": {"structureId": sid}, "version": cur.get("version"), "actions": [act]})
        cur = sync(j); i = j
    return ent

def create_structure(job_id, name, exclude, layout=None):
    job = JOBS.get(job_id)
    tree = job.get("tree") if job else None
    if not tree: raise JiraError(400, "Сначала соберите структуру (результат сборки не найден)")
    name = str(name or "").strip()[:200]
    if not name: raise JiraError(400, "Укажите название структуры")
    ent, skipped = normalize_layout(layout if layout else layout_from_tree(tree, exclude), tree)
    with_folders = any(e["kind"] == "folder" for e in ent)
    rows = [(e["key"], e["id"], e["l"]) for e in ent if e["kind"] == "issue"]
    if not rows: raise JiraError(400, "Нет задач для добавления в структуру")
    # 1. разведка: плагин есть и у пользователя есть доступ (ничего не создаётся)
    try:
        struct_call("structure", "GET", "/structure")
    except JiraError as e:
        if e.code == 404: raise JiraError(404, "REST плагина «Структура» не найден: плагин не установлен или у него другая версия API")
        raise
    # 2. создаём структуру
    desc = f"Создано Jira Toolkit из {tree['root']}, {time.strftime('%d.%m.%Y %H:%M')}. Задач: {len(rows)}"
    created = struct_call("structure", "POST", "/structure", {"name": name, "description": desc})
    sid = created.get("id")
    if sid is None: raise JiraError(500, "Jira не вернула номер созданной структуры: " + json.dumps(created, ensure_ascii=False)[:200])
    result = {"id": sid, "url": STATE["base"].rstrip("/") + f"/secure/StructureBoard.jspa?s={sid}", "expected": len(ent),
              "skipped": skipped, "warnings": [], "verified": False, "found": 0, "folders": 0}
    try:
        with open(os.path.join(DATA, f"structure_{sid}.json"), "w", encoding="utf-8") as f:
            json.dump({"id": sid, "name": name, "root": tree["root"], "created": time.strftime("%Y-%m-%d %H:%M:%S"), "issues": [r[0] for r in rows],
                       "layout": [{"f": e["name"], "l": e["l"]} if e["kind"] == "folder" else {"k": e["key"], "l": e["l"]} for e in ent]}, f, ensure_ascii=False, indent=1)
    except OSError: pass
    if with_folders:
        # 3a. с папками: строки добавляются по порядку, папки создаются через REST Structure
        try:
            ent = build_forest(sid, ent, result)
        except JiraError as e:
            result["warnings"].append("структура заполнена не полностью: " + str(e)[:300])
        try:
            got = parse_forest(forest_latest(sid).get("formula"))
            result["found"] = len(got); result["expected"] = len(ent)
            result["folders"] = sum(1 for g in got if is_folder_item(g[2]))
            exp = [("folder" if e["kind"] == "folder" else e["id"], e["l"]) for e in ent]
            act = [("folder" if is_folder_item(g[2]) else g[2], g[1]) for g in got]
            result["verified"] = exp == act
            if got and not result["verified"] and not result["warnings"]:
                result["warnings"].append(f"состав структуры отличается от ожидаемого: ожидалось {len(ent)} строк, найдено {len(got)}")
        except JiraError as e:
            result["warnings"].append("не удалось проверить результат: " + str(e)[:160])
        if not result["found"]:
            result["warnings"].append("строки не добавлены. Удалите пустую структуру в «Управление структурами» и пришлите текст ошибки")
        return result
    # 3. добавляем строки одним запросом
    formula = ",".join(f"{-(100 + i)}:{lvl}:{iid}" for i, (_, iid, lvl) in enumerate(rows))
    variants = [{"action": "add", "under": 0, "forest": formula},
                {"action": "add", "under": 0, "after": 0, "forest": formula}]
    last_err, done = None, False
    for act in variants:
        try:
            cur = forest_latest(sid)
            if parse_formula(cur.get("formula")): done = True; break      # строки уже есть - не дублируем
            struct_call("forest", "POST", "/forest/update", {"spec": {"structureId": sid}, "version": cur.get("version"), "actions": [act]})
            done = True; break
        except JiraError as e:
            last_err = e
    # 4. проверка по факту
    try:
        got = parse_formula(forest_latest(sid).get("formula"))
        result["found"] = len(got)
        result["verified"] = [g[0] for g in got] == [r[1] for r in rows] and [g[1] for g in got] == [r[2] for r in rows]
        if got and not result["verified"]: result["warnings"].append(f"состав структуры отличается от ожидаемого: ожидалось {len(rows)} строк, найдено {len(got)}")
    except JiraError as e:
        result["warnings"].append("не удалось проверить результат: " + str(e)[:160])
    if not result["found"]:
        msg = str(last_err)[:300] if last_err else "строки не появились"
        result["warnings"].append("структура создана, но строки не добавлены (" + msg + "). Удалите пустую структуру в «Управление структурами» и пришлите текст ошибки")
    return result

# ---------------------------------------------------------------- Задачи из системного анализа (Confluence -> AMLM)
from html.parser import HTMLParser as _HTMLParser
import re as _re_sa
import html as _html_sa

def conf(method, path, body=None, tries=3):
    """Запрос к Confluence (отдельный адрес и токен; токен Jira к Confluence не подходит)."""
    if not STATE.get("ctoken"): raise JiraError(401, "Нет подключения к Confluence")
    url = STATE["cbase"].rstrip("/") + path
    ctx = None
    if STATE["insecure"]:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", "Bearer " + STATE["ctoken"]); req.add_header("Accept", "application/json")
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json"); data = json.dumps(body).encode("utf-8")
        try:
            with urllib.request.urlopen(req, data=data, context=ctx, timeout=60) as r:
                raw = r.read(); return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if (e.code == 429 or e.code >= 500) and i < tries - 1:
                last = JiraError(e.code, txt); time.sleep(2 * (i + 1)); continue
            raise JiraError(e.code, txt)
        except urllib.error.URLError as e:
            if i < tries - 1: last = JiraError(0, str(e)); time.sleep(2 * (i + 1)); continue
            raise JiraError(0, str(e))
    raise last

class _StorageParser(_HTMLParser):
    """Storage-формат Confluence -> плоский список блоков {tag, text}, таблицы -> строки с ячейками.
    Jira-макрос превращается в ключ задачи, ссылка на страницу - в её название."""
    BLOCK = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []; self.buf = []; self.tag = None
        self.macro = []          # стек имён макросов
        self.param = None        # имя текущего ac:parameter
        self.row = None; self.cell = None; self.table_id = 0; self.tables = []
        self.link_has_body = []
    def _flush(self):
        t = " ".join("".join(self.buf).split())
        if t and self.cell is None: self.blocks.append({"tag": self.tag or "p", "text": t, "table": None})
        self.buf = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "ac:structured-macro": self.macro.append(a.get("ac:name", "")); return
        if tag == "ac:parameter": self.param = a.get("ac:name", ""); return
        if tag == "ac:link": self.link_has_body.append(False); return
        if tag in ("ac:link-body", "ac:plain-text-link-body"):
            if self.link_has_body: self.link_has_body[-1] = True
            return
        if tag == "ri:page" and self.link_has_body:
            self._pending_title = a.get("ri:content-title", ""); return
        if tag == "table":
            self._flush(); self.table_id += 1; self.tables.append([]); return
        if tag == "tr" and self.tables: self.row = []; return
        if tag in ("td", "th") and self.row is not None:
            self._flush(); self.cell = []; return
        if tag == "br": self.buf.append(" "); return
        if tag in self.BLOCK:
            if self.cell is not None: self.cell.append(" "); return
            self._flush(); self.tag = tag
    def handle_endtag(self, tag):
        if tag == "ac:structured-macro":
            if self.macro: self.macro.pop()
            return
        if tag == "ac:parameter": self.param = None; return
        if tag == "ac:link":
            has = self.link_has_body.pop() if self.link_has_body else True
            t = getattr(self, "_pending_title", "")
            if not has and t: self._text(t)
            self._pending_title = ""; return
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split())); self.cell = None; return
        if tag == "tr" and self.row is not None:
            if self.tables and any(self.row): self.tables[-1].append(self.row)
            self.row = None; return
        if tag == "table":
            if self.tables and self.tables[-1]:
                self.blocks.append({"tag": "table", "text": "", "table": self.tables[-1]})
            return
        if tag in self.BLOCK and self.cell is None: self._flush(); self.tag = None
    def _text(self, t):
        (self.cell if self.cell is not None else self.buf).append(t)
    def handle_data(self, data):
        if self.param is not None:
            if self.macro and self.macro[-1] == "jira" and self.param == "key": self._text(" " + data.strip() + " ")
            return
        if self.macro and self.macro[-1] in ("toc", "children", "jira"): return
        self._text(data)
    def close(self):
        super().close(); self._flush()

SEC_RE = _re_sa.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")
KEY_RE_SA = _re_sa.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")
TABLE_RE_SA = _re_sa.compile(r"\b([A-Z][A-Z0-9_$]*\.[A-Z][A-Z0-9_$]+)\b")
DATE_RE_SA = _re_sa.compile(r"\b[cс]\s*(\d{1,2}\.\d{1,2}\.\d{2,4})")

def sa_page_id(url):
    u = str(url or "").strip()
    if u.isdigit(): return u
    m = _re_sa.search(r"pageId=(\d+)", u) or _re_sa.search(r"/pages/(\d+)", u)
    if m: return m.group(1)
    m = _re_sa.search(r"/display/([^/]+)/([^?#]+)", u)
    if m:
        space, title = m.group(1), urllib.parse.unquote_plus(m.group(2))
        r = conf("GET", "/rest/api/content?spaceKey=" + urllib.parse.quote(space) + "&title=" + urllib.parse.quote(title))
        res = r.get("results") or []
        if res: return str(res[0]["id"])
    raise JiraError(400, "Не понял ссылку на страницу: нужна ссылка вида .../pages/123456/... или ...pageId=123456")

def sa_parse_storage(title, storage):
    """Разбор страницы СА по типовому шаблону: ссылки, разделы 3.x, источники из 3.1, примеры сценариев."""
    p = _StorageParser(); p.feed(storage or ""); p.close()
    blocks = p.blocks
    info = {"title": title, "intro": "", "links": [], "sections": [], "sa_sections": [], "sources": [], "examples": [], "is_update": False}
    m = _re_sa.sub(r"^\s*Мотивация\s+", "", title or "")
    m = _re_sa.sub(r"\s*\(\s*[cс]\s*\d{1,2}\.\d{1,2}\.\d{2,4}\s*\)\s*$", "", m)
    info["product"] = m.strip() or (title or "").strip()
    d = DATE_RE_SA.search(title or ""); info["date"] = d.group(1) if d else ""
    top, cur_h1, in_dev, in_sa, in_src, in_ex = None, "", False, False, False, False
    for b in blocks:
        t, tag = b["text"], b["tag"]
        if tag == "h1":
            cur_h1 = t; low = t.lower()
            in_dev = t.startswith("3.") or "требования на разработку" in low
            in_sa = t.startswith("2.") or "системный анализ" in low
            in_ex = "пример" in low; in_src = False
            continue
        if not cur_h1 and tag != "table" and t and not info["intro"]:
            info["intro"] = t
        if "предыдущей верси" in t.lower(): info["is_update"] = True
        if cur_h1.lower().startswith("ссылки") and tag != "table":
            k = KEY_RE_SA.search(t)
            if k:
                label = _re_sa.split(r"\s*[-–—:]\s*" + _re_sa.escape(k.group(1)), t)[0].strip(" -–—:")
                info["links"].append({"label": label or k.group(1), "key": k.group(1)})
            continue
        if in_sa and tag in ("h2", "h3"): info["sa_sections"].append(t)
        if in_dev and tag != "table":
            s = SEC_RE.match(t)
            if s and s.group(1).count(".") >= 1 and (tag.startswith("h") or len(t) < 160):
                num = s.group(1); name = s.group(2).strip()
                info["sections"].append({"num": num, "name": name})
                in_src = "источник" in name.lower()
                continue
            if tag.startswith("h"): in_src = "источник" in t.lower()
        if in_src and tag != "table":           # источники абзацами: «Модуль переходов CIDS_MLM_PII.LINKS_STORE__TRANSITION»
            for f in TABLE_RE_SA.findall(t):
                sch, _, tb = f.partition(".")
                if len(sch) >= 3 and len(tb) >= 4 and f not in info["sources"]: info["sources"].append(f)   # отсекаем DD.MM и т.п.
        if in_src and tag == "table":
            rows = b["table"]
            for r in rows[1:] if rows and "источник" in (rows[0][0] if rows[0] else "").lower() else rows:
                if not r: continue
                found = TABLE_RE_SA.findall(r[0])
                for f in (found or [r[0][:120]]):
                    if f not in info["sources"]: info["sources"].append(f)
        if in_ex and tag != "table":
            e = _re_sa.match(r"^Пример\s+\d+\s*[.:]\s*(.+)$", t)
            if e: info["examples"].append(t.split("*")[0].strip())
    info["sections"] = [s for s in info["sections"] if s["num"].startswith("3")] or info["sections"]
    return info

def sa_epic_from_links(links, project):
    for l in links:
        if "dwh" in l["label"].lower() and l["key"].startswith(project + "-"): return l["key"]
    for l in links:
        if "эпик" in l["label"].lower() and l["key"].startswith(project + "-"): return l["key"]
    return ""

def wiki_esc(s):
    """Текст со страницы -> безопасный для wiki-разметки Jira: {N} иначе ломает списки, | ломает таблицы."""
    return str(s or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("[", "\\[").replace("]", "\\]")

def wiki_cell(s):
    return str(s or "").replace("|", "/").strip() or " "

def wiki_table(rows):
    return "\n".join(f"|{wiki_cell(k)}|{wiki_cell(v)}|" for k, v in rows)

TODO = "⚠ заполнить"

def sa_drafts(info, opts):
    """Черновики задач: анализ СТ + источников, реализация в DWH, по задаче на каждое доп. место доработки, одна тест-задача."""
    P = (opts.get("product") or info["product"]).strip()
    url = info.get("url", "")
    kind = opts.get("template") or ("rework" if info.get("is_update") else "new")
    date = info.get("date", "")
    secs = [s for s in info["sections"] if s["num"] != "3" and "источник" not in s["name"].lower()]
    sec_lines = "\n".join(f" - {s['num']}. {wiki_esc(s['name'])}" for s in secs) or " - см. раздел 3 страницы"
    src_lines = "\n".join(f" - {wiki_esc(s)}" for s in info["sources"]) or " - см. раздел 3.1 страницы"
    sa_lines = "\n".join(f" - {wiki_esc(s)}" for s in info["sa_sections"])
    intro = wiki_esc(info.get("intro", ""))
    observe = f"с {date} - дата запуска" if date else TODO
    out = []
    # 1. анализ
    desc = wiki_table([
        ("Область доработки" if kind == "rework" else "Область разработки", P),
        ("Описание задачи", "Проанализировать системные требования и источники данных, подтвердить или уточнить алгоритм расчёта до начала разработки.\n"
                            + (f"{intro}\n" if intro else "") + "Источники (3.1):\n" + src_lines
                            + ("\nСистемный анализ (раздел 2):\n" + sa_lines if sa_lines else "")),
        ("Ссылка на confluence", f"[{url}]" if url else TODO),
        ("Результат", "Подтверждённые источники и алгоритм; вопросы к СА закрыты или зафиксированы в комментариях"),
    ])
    out.append({"role": "analysis", "summary": f"{P}: анализ СТ и источников", "description": desc})
    # 2. реализация
    body = (f"{intro}\n" if intro else "") + "Реализовать расчёт в DWH по требованиям раздела 3:\n" + sec_lines
    if kind == "rework":
        rows = [("Область доработки", P + "\nИсточники:\n" + src_lines), ("Период перерасчета", opts.get("recalc") or "-"),
                ("Статус решения", opts.get("status") or "Целевое"), ("Описание задачи", body),
                ("Ссылка на confluence", f"[{url}]" if url else TODO),
                ("Влияние доработки на процессы", opts.get("impact") or TODO),
                ("Влияние на процессы, если доработка не будет внедрена", opts.get("noimpact") or TODO),
                ("Период наблюдения за процессом", observe), ("Требуется ли тестирование бизнес-логики доработки", "да")]
    else:
        rows = [("Область разработки", P + "\nИсточники:\n" + src_lines), ("Зачем нужна разработка", opts.get("impact") or TODO),
                ("Статус решения", opts.get("status") or "Целевое"), ("Описание задачи", body),
                ("Ссылка на confluence", f"[{url}]" if url else TODO), ("Период наблюдения за процессом", observe),
                ("Требуется ли тестирование бизнес-логики доработки", "да" + (", " + opts["tester"] if opts.get("tester") else "")),
                ("Требуется ли загрузка исторических данных", opts.get("history") or TODO)]
    out.append({"role": "dev", "summary": f"{P}: реализация расчёта в DWH", "description": wiki_table(rows)})
    # 2a. доп. места доработки (например, бандл)
    for x in opts.get("extra") or []:
        nm = " ".join(str(x.get("name") or "").split())[:120]
        if not nm: continue
        link = str(x.get("link") or "").strip()
        rows = [("Область доработки", nm), ("Период перерасчета", opts.get("recalc") or "-"), ("Статус решения", opts.get("status") or "Целевое"),
                ("Описание задачи", f"Доработка «{nm}» с учётом изменений «{P}»" + (f" (см. {link})" if link else "")),
                ("Ссылка на confluence", f"[{link}]" if link else (f"[{url}]" if url else TODO)),
                ("Влияние доработки на процессы", TODO), ("Влияние на процессы, если доработка не будет внедрена", TODO),
                ("Период наблюдения за процессом", observe), ("Требуется ли тестирование бизнес-логики доработки", "да")]
        out.append({"role": "extra", "summary": f"{P}: учёт в {nm}", "description": wiki_table(rows)})
    # 3. тестирование (одна задача)
    ex = "\n".join(f" - {wiki_esc(e)}" for e in info["examples"]) or " - по примерам сценариев со страницы"
    places = ", ".join([P] + [" ".join(str(x.get("name") or "").split()) for x in opts.get("extra") or [] if x.get("name")])
    desc = (f"Подготовить тест-кейсы и протестировать: {places}.\n\nСценарии со страницы СА:\n{ex}\n\n"
            + (f"Описание: [{url}]" if url else ""))
    out.append({"role": "test", "summary": f"{P}: тест-кейсы и тестирование", "description": desc})
    for t in out: t["todo"] = t["description"].count(TODO)
    return out

def sa_required_fields(project, itype):
    """Обязательные поля при создании, которые программа не заполняет (Jira 9+ и старый createmeta)."""
    std = {"project", "issuetype", "summary", "reporter", "description", "components", "labels", "assignee", "priority"}
    try:
        its = jira("GET", f"/rest/api/2/issue/createmeta/{urllib.parse.quote(project)}/issuetypes", tries=2)
        vals = its.get("values") or its.get("issueTypes") or []
        it = next((x for x in vals if x.get("name") == itype), None)
        if not it: return None, f"в проекте {project} нет типа задачи «{itype}»"
        f = jira("GET", f"/rest/api/2/issue/createmeta/{urllib.parse.quote(project)}/issuetypes/{it['id']}?maxResults=200", tries=2)
        fl = f.get("values") or f.get("fields") or []
        req = [x.get("name") or x.get("fieldId") for x in fl if x.get("required") and not x.get("hasDefaultValue") and (x.get("fieldId") or x.get("key")) not in std]
        return req, ""
    except JiraError as e:
        if e.code not in (404, 400): return None, str(e)[:160]
    try:
        m = jira("GET", "/rest/api/2/issue/createmeta?projectKeys=" + urllib.parse.quote(project) + "&issuetypeNames=" + urllib.parse.quote(itype) + "&expand=projects.issuetypes.fields", tries=2)
        for pr in m.get("projects", []):
            for it in pr.get("issuetypes", []):
                return [v.get("name") or k for k, v in (it.get("fields") or {}).items() if v.get("required") and not v.get("hasDefaultValue") and k not in std], ""
        return None, f"в проекте {project} нет типа задачи «{itype}»"
    except JiraError as e:
        return None, str(e)[:160]

def sa_epic_field():
    fl = all_fields()
    for fid, nm in fl.items():
        if nm.strip().lower() in ("epic link", "ссылка на эпик", "эпик"): return fid
    return ""

SA_PAGES = {}

def sa_parse(url, project, itype):
    pid = sa_page_id(url)
    pg = conf("GET", f"/rest/api/content/{pid}?expand=body.storage,version,space")
    title = pg.get("title", "")
    info = sa_parse_storage(title, ((pg.get("body") or {}).get("storage") or {}).get("value", ""))
    info["id"] = pid; info["version"] = (pg.get("version") or {}).get("number")
    info["url"] = STATE["cbase"].rstrip("/") + f"/pages/viewpage.action?pageId={pid}"
    info["space"] = (pg.get("space") or {}).get("key", "")
    warnings = []
    epic = sa_epic_from_links(info["links"], project)
    info["epic"] = epic; info["epic_info"] = None
    if epic:
        try:
            e = jira("GET", f"/rest/api/2/issue/{epic}?fields=summary,status,issuetype", tries=2)["fields"]
            st = e.get("status") or {}
            info["epic_info"] = {"summary": e.get("summary", ""), "status": st.get("name", ""), "done": (st.get("statusCategory") or {}).get("key") == "done",
                                 "type": (e.get("issuetype") or {}).get("name", "")}
            if info["epic_info"]["done"]: warnings.append(f"Эпик {epic} со страницы уже закрыт ({st.get('name')}). Укажите другой эпик или оставьте поле пустым.")
        except JiraError as e:
            warnings.append(f"Эпик {epic} не прочитан: {str(e)[:120]}")
    else:
        warnings.append(f"На странице не найден FEATURE-эпик DWH из проекта {project}: укажите эпик вручную.")
    # уже созданные задачи по этой странице
    dups = []
    for q in (f'project = {project} AND text ~ "{pid}"', f'project = {project} AND summary ~ "{info["product"].replace(chr(34), "")}" AND created >= -180d'):
        try:
            r = jira("POST", "/rest/api/2/search", {"jql": q + " ORDER BY created DESC", "fields": ["summary", "status", "created"], "maxResults": 20}, tries=2)
            for it in r.get("issues", []):
                if it["key"] not in [d["key"] for d in dups]:
                    dups.append({"key": it["key"], "summary": it["fields"].get("summary", ""), "status": (it["fields"].get("status") or {}).get("name", ""),
                                 "created": (it["fields"].get("created") or "")[:10]})
        except JiraError: pass
    req, err = sa_required_fields(project, itype)
    if err: warnings.append("Не удалось проверить обязательные поля: " + err)
    elif req: warnings.append("В Jira есть обязательные поля, которые программа не заполняет: " + ", ".join(req) + ". Создание может не пройти.")
    if not info["sections"]: warnings.append("На странице не найдены пункты раздела 3 «Требования на разработку» — проверьте описание задач вручную.")
    if not info["sources"]: warnings.append("Не найдена таблица источников в 3.1 — список источников не заполнен.")
    tok = secrets.token_urlsafe(8); SA_PAGES[tok] = info
    return {"page": tok, "info": info, "duplicates": dups, "warnings": warnings, "required": req or []}

def sa_create(body):
    info = SA_PAGES.get(str(body.get("page", "")))
    if not info: raise JiraError(400, "Сначала разберите страницу")
    project = str(body.get("project") or "").strip().upper()
    if not _re_sa.match(r"^[A-Z][A-Z0-9_]*$", project): raise JiraError(400, "Некорректный ключ проекта")
    itype = str(body.get("issuetype") or "Задача").strip()
    epic = str(body.get("epic") or "").strip().upper()
    if epic and not KEY_RE_SA.fullmatch(epic): raise JiraError(400, "Некорректный ключ эпика")
    tasks = [t for t in (body.get("tasks") or []) if isinstance(t, dict)][:30]
    if not tasks: raise JiraError(400, "Нет задач для создания")
    efield = sa_epic_field() if epic else ""
    res, created = [], []
    for t in tasks:
        summary = " ".join(str(t.get("summary") or "").split())[:250]
        if not summary: res.append({"summary": "", "error": "пустое название"}); continue
        f = {"project": {"key": project}, "issuetype": {"name": itype}, "summary": summary, "description": str(t.get("description") or "")[:30000]}
        a = str(t.get("assignee") or "").strip()
        if a: f["assignee"] = {"name": a}
        if efield: f[efield] = epic
        note = ""
        try:
            r = jira("POST", "/rest/api/2/issue", {"fields": f}, tries=2)
        except JiraError as e:
            if efield and efield in e.text:          # поле эпика не на экране создания - создаём без него и связываем ссылкой
                f.pop(efield, None)
                r = None
                try:
                    r = jira("POST", "/rest/api/2/issue", {"fields": f}, tries=2)
                    jira("POST", "/rest/api/2/issueLink", {"type": {"name": "Relates"}, "inwardIssue": {"key": r["key"]}, "outwardIssue": {"key": epic}}, tries=2)
                    note = "эпик привязан связью «Relates»: поле «Epic Link» недоступно при создании"
                except JiraError as e2:
                    if not r: res.append({"summary": summary, "error": str(e2)[:300]}); continue
                    note = "эпик не привязан: " + str(e2)[:120]
            else:
                res.append({"summary": summary, "error": str(e)[:300]}); continue
        created.append(r["key"]); res.append({"summary": summary, "key": r["key"], "note": note})
        time.sleep(0.3 + RATE["extra"])
    jid = time.strftime("%Y%m%d_%H%M%S")
    try:
        with open(os.path.join(DATA, f"created_{jid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"created": time.strftime("%Y-%m-%d %H:%M:%S"), "page": info.get("url"), "project": project, "epic": epic, "issues": created, "results": res}, fh, ensure_ascii=False, indent=1)
    except OSError: pass
    return {"base": STATE["base"], "results": res, "created": len(created), "journal": f"created_{jid}.json"}


# ---------------------------------------------------------------- Рабочие доски в Jira (фильтры + дашборды + гаджеты)
import re as _re_dg
GADGET_BASE = "rest/gadgets/1.0/g/com.atlassian.jira.gadgets:"
G_FILTER = GADGET_BASE + "filter-results-gadget/gadgets/filter-results-gadget.xml"
G_PIE = GADGET_BASE + "pie-chart-gadget/gadgets/piechart-gadget.xml"
G_CVR = GADGET_BASE + "created-vs-resolved-issues-chart-gadget/gadgets/createdvsresolved-gadget.xml"
G_TWO = GADGET_BASE + "two-dimensional-stats-gadget/gadgets/two-dimensional-stats-gadget.xml"
G_RES = GADGET_BASE + "resolution-time-gadget/gadgets/resolution-time-gadget.xml"

# ключ фильтра: (название, условие, сортировка)
DG_FILTERS = {
    "open":     ("Не закрытые", " AND statusCategory != Done", " ORDER BY priority DESC, created ASC"),
    "noassign": ("Без исполнителя", " AND statusCategory != Done AND assignee is EMPTY", " ORDER BY created ASC"),
    "overdue":  ("Просроченные", " AND statusCategory != Done AND duedate < now()", " ORDER BY duedate ASC"),
    "all":      ("Все задачи", "", " ORDER BY created DESC"),
    "closed":   ("Закрытые", " AND resolution is not EMPTY", " ORDER BY resolutiondate DESC"),
}
# (id, дашборд, название, фильтр, гаджет-URL, тип словами, настройки(days) -> dict или None для ручного добавления)
def _p_list(fid, d): return {"isConfigured": "true", "filterId": fid, "num": "20", "columnNames": "issuetype|issuekey|summary|assignee|priority|status", "refresh": "false"}
def _p_pie(stat):
    return lambda fid, d: {"isConfigured": "true", "projectOrFilterId": "filter-" + fid, "statType": stat, "refresh": "false"}
def _p_cvr(cum, period):
    return lambda fid, d: {"isConfigured": "true", "projectOrFilterId": "filter-" + fid, "daysprevious": str(d), "periodName": period, "isCumulative": "true" if cum else "false", "showUnresolvedTrend": "false", "versionLabel": "major", "refresh": "false"}
def _p_two(x, y):
    return lambda fid, d: {"isConfigured": "true", "filterId": "filter-" + fid, "xstattype": x, "ystattype": y, "sortBy": "natural", "sortDirection": "asc", "numberToShow": "20", "refresh": "false"}
def _p_res(fid, d): return {"isConfigured": "true", "projectOrFilterId": "filter-" + fid, "daysprevious": str(d), "periodName": "weekly", "refresh": "false"}
DG_GADGETS = [
    ("w1", "work", "Не закрытые задачи (список)", "open", G_FILTER, "Результаты фильтра", _p_list),
    ("w2", "work", "Не закрытые по статусам", "open", G_PIE, "Круговая диаграмма: статус", _p_pie("statuses")),
    ("w3", "work", "Не закрытые по исполнителям", "open", G_PIE, "Круговая диаграмма: исполнитель", _p_pie("assignees")),
    ("w4", "work", "Создано и закрыто (последние 30 дней)", "all", G_CVR, "Создано и решено", _p_cvr(False, "daily")),
    ("w5", "work", "Без исполнителя (список)", "noassign", G_FILTER, "Результаты фильтра", _p_list),
    ("w6", "work", "Просроченные (список)", "overdue", G_FILTER, "Результаты фильтра", _p_list),
    ("w7", "work", "Не закрытые по типам задач", "open", G_PIE, "Круговая диаграмма: тип задачи", _p_pie("issuetype")),
    ("f1", "flow", "Создано и закрыто (период и группировку выбирают в гаджете)", "all", G_CVR, "Создано и решено", _p_cvr(False, "weekly")),
    ("f3", "flow", "Время в статусе (Issue Status Duration)", "closed", None, "Issue Status Duration (гаджет Альфа-Банка)", None),
    ("f4", "flow", "Компоненты × результат решения", "all", G_TWO, "Статистика по двум полям", _p_two("components", "resolution")),
    ("f5", "flow", "Закрытые по результату (resolution)", "closed", G_PIE, "Круговая диаграмма: resolution", _p_pie("resolution")),
    ("f6", "flow", "Накопительно: создано и закрыто", "all", G_CVR, "Создано и решено (накопительно)", _p_cvr(True, "weekly")),
    ("f7", "flow", "Компоненты × статус", "all", G_TWO, "Статистика по двум полям", _p_two("components", "statuses")),
]
DG_DASH = {"work": "Рабочий", "flow": "Статистика разбора потока"}

def jq_str(s): return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def dg_clean_list(v):
    out = []
    for x in (v or []):
        x = str(x).strip()
        if not x: continue
        if len(x) > 80 or any(ord(c) < 32 for c in x): raise JiraError(400, "Недопустимое значение в списке типов или компонентов")
        out.append(x)
    return out

def dg_plan(body):
    projects = [p.strip().upper() for p in str(body.get("projects", "")).replace(";", ",").split(",") if p.strip()]
    if not projects: raise JiraError(400, "Укажите хотя бы один проект")
    for p in projects:
        if not _re_dg.match(r"^[A-Z][A-Z0-9_]{1,19}$", p): raise JiraError(400, f"Некорректный ключ проекта: {p}")
    if len(projects) > 10: raise JiraError(400, "Не больше 10 проектов за раз")
    types, comps = dg_clean_list(body.get("types")), dg_clean_list(body.get("components"))
    days = min(max(int(body.get("days") or 30), 1), 365); days_flow = min(max(int(body.get("days_flow") or 90), 7), 730)
    where = "project in (" + ", ".join(projects) + ")"
    if types: where += " AND issuetype in (" + ", ".join(jq_str(t) for t in types) + ")"
    if comps: where += " AND component in (" + ", ".join(jq_str(t) for t in comps) + ")"
    label = ", ".join(projects)
    flt = {}
    for k, (nm, cond, order) in DG_FILTERS.items():
        flt[k] = {"key": k, "name": f"[Toolkit] {label}: {nm}", "jql": where + cond + order}
    dnames = {k: f"{v}: {label}" for k, v in DG_DASH.items()}
    for k in dnames:
        custom = str((body.get("dash_names") or {}).get(k, "")).strip()
        if custom: dnames[k] = custom[:120]
    gad = []
    for gid, dash, title, fk, url, kind, pf in DG_GADGETS:
        gad.append({"id": gid, "dash": dash, "title": title, "filter": fk, "kind": kind, "auto": bool(url and pf), "jql": flt[fk]["jql"]})
    return {"projects": projects, "filters": flt, "dash_names": dnames, "gadgets": gad, "days": days, "days_flow": days_flow}

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

def jira_form(method, path, fields):
    """Форменный запрос (как делает сам интерфейс Jira). Возвращает (код, заголовки, текст); редиректы не выполняет."""
    ctx = None
    if STATE["insecure"]:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(STATE["base"].rstrip("/") + path, data=urllib.parse.urlencode(fields).encode("utf-8"), method=method)
    req.add_header("Authorization", "Bearer " + STATE["token"]); req.add_header("X-Atlassian-Token", "no-check")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"); req.add_header("Accept", "application/json, text/html")
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise JiraError(0, str(e))

def dg_existing_filters():
    out = {}
    try:
        for f in jira("GET", "/rest/api/2/filter/favourite", tries=2) or []:
            out[f.get("name")] = f
    except JiraError: pass
    return out

def dg_list_dashboards():
    """Все дашборды пользователя (мои и избранные) с постраничным чтением: {id: название}."""
    out = {}
    for flt in ("my", "favourite"):
        start = 0
        for _ in range(40):
            try: r = jira("GET", f"/rest/api/2/dashboard?filter={flt}&startAt={start}&maxResults=100", tries=2)
            except JiraError: break
            items = r.get("dashboards") or []
            for d in items: out[str(d.get("id"))] = d.get("name")
            start += len(items)
            if not items or start >= int(r.get("total") or 0): break
    return out

def dg_existing_dashboards():
    out = {}
    for i, n in dg_list_dashboards().items(): out[n] = i
    return out

def dg_gadget_ids(dash_id):
    """Номера гаджетов, которые Jira показывает на доске (для сравнения до и после добавления)."""
    ids = set()
    try: r = jira("GET", f"/rest/dashboards/1.0/{dash_id}", tries=2)
    except JiraError: return ids
    def walk(x):
        if isinstance(x, dict):
            if x.get("id") is not None and any(k in x for k in ("gadgetUrl", "url", "dashboardItemId", "title", "color", "type")) and "layout" not in x:
                ids.add(str(x["id"]))
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(r.get("gadgets", r) if isinstance(r, dict) else r)
    return ids

def dg_add_gadget(dash_id, spec_url, column, before):
    """Добавление гаджета так, как это делает интерфейс Jira 10: POST /rest/dashboards/1.0/<id>.json"""
    body = {"dashboardItemId": spec_url, "type": "gadget", "columnIndex": str(column)}
    last = None
    for path in (f"/rest/dashboards/1.0/{dash_id}.json", f"/rest/dashboards/1.0/{dash_id}"):
        try:
            r = jira("POST", path, body, tries=2); break
        except JiraError as e:
            last = e
            if e.code != 404: raise
    else:
        raise last
    raw = json.dumps(r, ensure_ascii=False)
    gid = ""
    if isinstance(r, dict) and r.get("id") is not None and "gadgets" not in r: gid = str(r["id"])
    if not gid:
        new = dg_gadget_ids(dash_id) - before
        if len(new) == 1: gid = new.pop()
    return gid, raw

def dg_set_prefs(dash_id, gadget_id, prefs):
    """Настройки гаджета в формате самого интерфейса Jira 10: PUT .../gadget/<id>/prefs, ключи с префиксом up_, isConfigured - булево."""
    body = {"up_" + k: (True if (k == "isConfigured" and v == "true") else v) for k, v in prefs.items()}
    jira("PUT", f"/rest/dashboards/1.0/{dash_id}/gadget/{gadget_id}/prefs", body, tries=2)

def dg_probe(dash_id):
    """Диагностика: какие адреса работы с гаджетами отвечают в этой Jira. Ничего не создаёт и не меняет (только GET)."""
    dash_id = dg_parse_dash_id(dash_id)
    if not dash_id: raise JiraError(400, "Вставьте ссылку на доску или её номер")
    cands = [f"/rest/dashboards/1.0/{dash_id}", f"/rest/dashboards/1.0/{dash_id}/gadget", f"/rest/dashboards/1.0/{dash_id}/gadgets",
             f"/rest/dashboards/1.0/dashboard/{dash_id}", f"/rest/api/2/dashboard/{dash_id}", f"/rest/api/2/dashboard/{dash_id}/gadget",
             f"/rest/gadgets/1.0/dashboard/{dash_id}", "/rest/config/1.0/directory", "/rest/config/1.0/directory.json", "/rest/gadgets/1.0/directory"]
    out, specs = [], set()
    for c in cands:
        try:
            r = jira("GET", c, tries=1)
            txt = json.dumps(r, ensure_ascii=False)
            out.append({"path": c, "code": 200, "note": txt[:1500] if c.endswith("/" + dash_id) and "dashboards/1.0" in c else txt[:160]})
            for m in _re_dg.findall(r'[^"\s]*gadgets?[^"\s]*\.xml', txt): specs.add(m)
        except JiraError as e:
            out.append({"path": c, "code": e.code, "note": str(e.text)[:120]})
    return {"dash_id": dash_id, "probes": out, "specs": sorted(specs)[:80],
            "hint": "404 = адреса нет; 405 или 200 = адрес есть. Пришлите этот результат."}

def dg_preview(body):
    plan = dg_plan(body)
    ef = dg_existing_filters() if STATE["token"] else {}
    ed = dg_existing_dashboards() if STATE["token"] else {}
    for f in plan["filters"].values():
        ex = ef.get(f["name"])
        f["exists"] = bool(ex); f["same_jql"] = bool(ex and (ex.get("jql") or "").strip() == f["jql"])
    plan["dash_exists"] = {k: n in ed for k, n in plan["dash_names"].items()}
    return plan

def dg_create_dashboard(name, desc):
    """Создание пустого дашборда формой AddPortalPage (REST для этого в Jira Server нет). Номер находим по ответу или по списку до/после."""
    notes = []
    before = dg_list_dashboards()
    code, hdr, txt = jira_form("POST", "/secure/AddPortalPage.jspa", {"portalPageName": name, "portalPageDescription": desc, "favourite": "true", "inline": "true", "decorator": "dialog", "Create": "Create"})
    loc = hdr.get("Location") or hdr.get("location") or ""
    notes.append(f"AddPortalPage: HTTP {code}, Location={loc[:150]!r}")
    m = _re_dg.search(r"selectPageId=(\d+)", loc + " " + txt)
    if m: return m.group(1), notes
    after = dg_list_dashboards()
    new = {i: n for i, n in after.items() if i not in before}
    by_name = [i for i, n in new.items() if n == name]
    if len(by_name) == 1: return by_name[0], notes
    if len(new) == 1: return next(iter(new)), notes
    same = [i for i, n in after.items() if n == name]
    if len(same) == 1: return same[0], notes
    notes.append(f"новых дашбордов: {len(new)}, с таким названием: {len(same)}; тело ответа: {txt[:150]!r}")
    raise JiraError(code or 500, "Jira не вернула номер дашборда (" + "; ".join(notes) + ")")

def dg_parse_dash_id(v):
    v = str(v or "").strip()
    if not v: return ""
    m = _re_dg.search(r"selectPageId=(\d+)", v) or _re_dg.fullmatch(r"(\d{1,12})", v)
    if not m: raise JiraError(400, "Не удалось найти номер дашборда в «" + v[:80] + "». Вставьте ссылку вида .../Dashboard.jspa?selectPageId=12345 или просто номер")
    return m.group(1)

def dg_create(body):
    plan = dg_plan(body)
    given = {k: dg_parse_dash_id((body.get("dash_ids") or {}).get(k)) for k in ("work", "flow")}
    selected = set(body.get("selected") or [])
    mode = body.get("onexist", "skip")
    gsel = [g for g in DG_GADGETS if g[0] in selected]
    if not gsel: raise JiraError(400, "Не выбрано ни одного виджета")
    base = STATE["base"].rstrip("/")
    ef = dg_existing_filters(); ed = dg_existing_dashboards()
    res = {"filters": [], "dashboards": [], "manual": [], "log": []}
    fid = {}
    for fk in sorted({g[3] for g in gsel}):
        f = plan["filters"][fk]; ex = ef.get(f["name"]); name = f["name"]
        if ex and (ex.get("jql") or "").strip() == f["jql"]:
            fid[fk] = str(ex["id"]); res["filters"].append({"name": name, "id": fid[fk], "status": "уже был, использован"}); continue
        if ex: name = f"{name} ({time.strftime('%d.%m %H:%M')})"
        try:
            r = jira("POST", "/rest/api/2/filter", {"name": name, "description": "Создано Jira Toolkit", "jql": f["jql"], "favourite": True}, tries=3)
            fid[fk] = str(r["id"]); res["filters"].append({"name": name, "id": fid[fk], "status": "создан"})
        except JiraError as e:
            res["filters"].append({"name": name, "id": "", "status": "ошибка: " + str(e)[:200]})
    for dk in ("work", "flow"):
        gl = [g for g in gsel if g[1] == dk]
        if not gl: continue
        dname = plan["dash_names"][dk]; dinfo = {"key": dk, "name": dname, "id": "", "url": "", "status": "", "gadgets": []}
        res["dashboards"].append(dinfo)
        if given[dk]:
            dash_id = given[dk]; dinfo.update(id=dash_id, url=f"{base}/secure/Dashboard.jspa?selectPageId={dash_id}", status="использован ваш дашборд")
        elif dname in ed:
            if mode == "skip":
                dinfo["status"] = "дашборд с таким названием уже есть, пропущен (чтобы наполнить его, вставьте ссылку в поле доски)"; dinfo["id"] = ed[dname]; dinfo["url"] = f"{base}/secure/Dashboard.jspa?selectPageId={ed[dname]}"; continue
            dname = f"{dname} ({time.strftime('%d.%m %H:%M')})"; dinfo["name"] = dname
        if not given[dk]:
            dash_id = ""
            try:
                dash_id, notes = dg_create_dashboard(dname, "Создано Jira Toolkit")
                dinfo.update(id=dash_id, url=f"{base}/secure/Dashboard.jspa?selectPageId={dash_id}", status="создан")
            except JiraError as e:
                dinfo["status"] = "дашборд создать не удалось, создайте пустой вручную и вставьте ссылку в поле доски: " + str(e)[:300]
        for gid, _d, title, fk, url, kind, pf in gl:
            g = {"id": gid, "title": title, "status": ""}; dinfo["gadgets"].append(g)
            fidv = fid.get(fk)
            manual = lambda why: res["manual"].append({"dash": dname, "title": title, "kind": kind, "filter": plan["filters"][fk]["name"], "filter_id": fidv or "", "why": why})
            if not url or not pf: g["status"] = "добавить вручную"; manual("гаджет зависит от установленных плагинов"); continue
            if not dash_id or not fidv: g["status"] = "пропущен"; manual("нет дашборда или фильтра"); continue
            try:
                before = dg_gadget_ids(dash_id)
                gadget_id, raw = dg_add_gadget(dash_id, url, len([x for x in dinfo["gadgets"] if x["status"].startswith("добавлен")]) % 2, before)
                if not res.get("sample"): res["sample"] = raw[:600]
            except JiraError as e:
                g["status"] = "не добавлен: " + str(e)[:300]
                if e.code == 404 and not res.get("probe"):
                    try: res["probe"] = dg_probe(dash_id)
                    except JiraError: res["probe"] = None
                manual("Jira не приняла добавление гаджета"); continue
            try:
                days = plan["days_flow"] if dk == "flow" else plan["days"]
                if not gadget_id: raise JiraError(0, "не удалось определить номер добавленного гаджета")
                dg_set_prefs(dash_id, gadget_id, pf(fidv, days))
                g["status"] = "добавлен и настроен"
            except JiraError as e:
                g["status"] = "добавлен, но настройки не применились: " + str(e)[:300]; manual("гаджет добавлен пустым: выберите фильтр в его настройках")
    try:
        with open(os.path.join(DATA, f"dashgen_{time.strftime('%Y%m%d_%H%M%S')}.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    except OSError: pass
    for m in res["manual"]:
        m["filter_url"] = f"{base}/issues/?filter={m['filter_id']}" if m["filter_id"] else ""
    return res

# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "JiraToolkit"
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT[0]}", f"localhost:{PORT[0]}")
    def _auth(self):
        if not self._host_ok() or self.headers.get("X-Session") != SECRET:
            self._json({"error": "доступ запрещён"}, 403); return False
        return True
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            if not self._host_ok(): return self._json({"error": "доступ запрещён"}, 403)
            b = PAGE.replace("__SECRET__", SECRET).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b); return
        if not self._auth(): return
        if path == "/api/state": return self._json({"connected": bool(STATE["token"]), "base": STATE["base"], "user": STATE["user"],
                                                     "cconnected": bool(STATE.get("ctoken")), "cbase": STATE.get("cbase", ""), "cuser": STATE.get("cuser", "")})
        if path == "/api/dash/config": return self._json(load_dash())
        if path.startswith("/api/job/"):
            j = JOBS.get(path.split("/")[-1])
            return self._json({k: v for k, v in j.items() if k != "cancel"} if j else {"error": "нет такой задачи"}, 200 if j else 404)
        if path == "/api/journals":
            out = []
            for fn in sorted(os.listdir(DATA), reverse=True):
                if fn.startswith("journal_"):
                    try:
                        with open(os.path.join(DATA, fn), encoding="utf-8") as f: d = json.load(f)
                        out.append({"id": d["id"], "created": d["created"], "count": len(d["entries"])})
                    except Exception: pass
            return self._json(out)
        self._json({"error": "не найдено"}, 404)
    def do_POST(self):
        if not self._auth(): return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: return self._json({"error": "некорректный запрос"}, 400)
        path = self.path.split("?")[0]
        try:
            if path == "/api/connect":
                base = str(body.get("base", "")).strip().rstrip("/")
                tok = str(body.get("token", "")).strip().strip('"').strip("'").strip()
                if not base.startswith("http"): return self._json({"error": "укажите адрес Jira, например https://jira.company.ru"}, 400)
                if not tok or any(ch.isspace() for ch in tok): return self._json({"error": "токен пустой или содержит пробелы"}, 400)
                STATE.update(base=base, token=tok, insecure=bool(body.get("insecure", True)), user=""); PC_CACHE.clear()
                try: me = jira("GET", "/rest/api/2/myself", tries=2)
                except JiraError as e:
                    STATE.update(token="")
                    hint = " Токен не принят: нужен Personal Access Token, не истёкший." if e.code == 401 else ""
                    return self._json({"error": str(e) + hint}, 400)
                STATE["user"] = me.get("displayName") or me.get("name") or ""
                return self._json({"connected": True, "base": base, "user": STATE["user"]})
            if path == "/api/conf/connect":
                cb = str(body.get("base", "")).strip().rstrip("/")
                ct = str(body.get("token", "")).strip().strip('"').strip("'").strip()
                if not cb.startswith("http"): return self._json({"error": "укажите адрес Confluence"}, 400)
                if not ct or any(ch.isspace() for ch in ct): return self._json({"error": "токен пустой или содержит пробелы"}, 400)
                STATE.update(cbase=cb, ctoken=ct, cuser="", insecure=bool(body.get("insecure", STATE["insecure"])))
                try: me = conf("GET", "/rest/api/user/current", tries=2)
                except JiraError as e:
                    STATE.update(ctoken="")
                    hint = " Нужен Personal Access Token из Confluence (профиль → Настройки → Personal Access Tokens)." if e.code in (401, 403) else ""
                    return self._json({"error": str(e) + hint}, 400)
                if me.get("type") == "anonymous": STATE.update(ctoken=""); return self._json({"error": "Confluence не принял токен (анонимный доступ)"}, 400)
                STATE["cuser"] = me.get("displayName") or me.get("username") or ""
                return self._json({"cconnected": True, "cbase": cb, "cuser": STATE["cuser"]})
            if path == "/api/disconnect":
                STATE.update(token="", user="", ctoken="", cuser=""); SA_PAGES.clear(); return self._json({"connected": False})
            if not STATE["token"]: return self._json({"error": "сначала подключитесь к Jira"}, 401)
            if path == "/api/sa/parse":
                if not STATE.get("ctoken"): return self._json({"error": "сначала подключите Confluence"}, 401)
                prj = str(body.get("project") or "AMLM").strip().upper()
                if not _re_sa.match(r"^[A-Z][A-Z0-9_]*$", prj): return self._json({"error": "некорректный ключ проекта"}, 400)
                return self._json(sa_parse(body.get("url", ""), prj, str(body.get("issuetype") or "Задача").strip()))
            if path == "/api/sa/draft":
                info = SA_PAGES.get(str(body.get("page", "")))
                if not info: return self._json({"error": "сначала разберите страницу"}, 400)
                opts = {k: str(body.get(k) or "")[:500] for k in ("product", "template", "status", "recalc", "impact", "noimpact", "history", "tester")}
                opts["extra"] = [{"name": str(x.get("name") or "")[:120], "link": str(x.get("link") or "")[:500]} for x in (body.get("extra") or [])[:10] if isinstance(x, dict)]
                return self._json({"tasks": sa_drafts(info, opts)})
            if path == "/api/sa/create":
                return self._json(sa_create(body))
            if path == "/api/bulk/preview":
                return self._json({"rows": plan_rows(body.get("rows", []))})
            if path == "/api/bulk/apply":
                rows = body.get("rows", [])
                job = new_job("apply", len(rows))
                threading.Thread(target=run_apply, args=(job, rows, max(0.2, float(body.get("pause", 0.5)))), daemon=True).start()
                return self._json({"job": job["id"]})
            if path == "/api/bulk/rollback":
                jid = str(body.get("journal", "")).replace("/", "").replace("..", "")
                if not os.path.exists(os.path.join(DATA, f"journal_{jid}.json")): return self._json({"error": "журнал не найден"}, 404)
                job = new_job("rollback", 0)
                threading.Thread(target=run_rollback, args=(job, jid), daemon=True).start()
                return self._json({"job": job["id"]})
            if path.startswith("/api/job/") and path.endswith("/cancel"):
                j = JOBS.get(path.split("/")[-2])
                if j: j["cancel"] = True
                return self._json({"ok": True})
            if path == "/api/tree/create":
                import re as _re
                ex = frozenset(str(x).strip().upper() for x in (body.get("exclude") or []) if _re.match(r"^[A-Za-z][A-Za-z0-9_]*-\d+$", str(x).strip()))
                return self._json(create_structure(str(body.get("job", "")), body.get("name", ""), ex, body.get("layout")))
            if path == "/api/tree/start":
                import re as _re
                key = str(body.get("key", "")).strip().upper()
                if not _re.match(r"^[A-Z][A-Z0-9_]*-\d+$", key): return self._json({"error": "некорректный ключ задачи, пример: PROJ-123"}, 400)
                depth = min(max(int(body.get("depth", 3)), 1), 6); mx = min(max(int(body.get("max", 300)), 10), 1000)
                job = new_job("tree", 0); job["tree"] = None; job["msg"] = "Запуск"
                threading.Thread(target=run_tree, args=(job, key, depth, bool(body.get("links", True)), mx, frozenset(str(x).strip().upper() for x in (body.get("exclude") or []) if _re.match(r"^[A-Za-z][A-Za-z0-9_]*-\d+$", str(x).strip()))), daemon=True).start()
                return self._json({"job": job["id"]})
            if path == "/api/links/start":
                import re as _re
                key = str(body.get("key", "")).strip().upper()
                if not _re.match(r"^[A-Z][A-Z0-9_]*-\d+$", key): return self._json({"error": "некорректный ключ задачи, пример: INCIDENT-1027"}, 400)
                extra = str(body.get("jql", "")).strip()
                names = [x for x in str(body.get("fields", "")).split(",") if x.strip()][:10]
                mx = min(max(int(body.get("max", 1000)), 10), 5000)
                job = new_job("links", 0); job["links"] = None; job["msg"] = "Запуск"
                threading.Thread(target=run_links, args=(job, key, extra, names, bool(body.get("parse", True)), mx), daemon=True).start()
                return self._json({"job": job["id"]})
            if path == "/api/dashgen/preview":
                return self._json(dg_preview(body))
            if path == "/api/dashgen/probe":
                return self._json(dg_probe((body.get("dash_ids") or {}).get("work") or (body.get("dash_ids") or {}).get("flow") or body.get("dash_id")))
            if path == "/api/dashgen/create":
                return self._json(dg_create(body))
            if path == "/api/dash/run":
                return self._json({"cards": run_cards(body.get("cards", []), str(body.get("project", "")).strip())})
            if path == "/api/dash/save":
                with open(dash_path(), "w", encoding="utf-8") as f: json.dump(body.get("config", {}), f, ensure_ascii=False, indent=1)
                return self._json({"ok": True})
            self._json({"error": "не найдено"}, 404)
        except JiraError as e: self._json({"error": str(e)}, 502)
        except Exception as e: self._json({"error": "внутренняя ошибка: " + str(e)}, 500)

PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Jira Toolkit v0.26</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
 --paper:#F3F1EA;--sheet:#FCFBF8;--sunk:#EEEBE3;
 --graphite:#24231F;--graphite-2:#55534C;--pencil:#85827A;--faint:#B4B2A9;
 --rule:rgba(36,35,31,.11);--rule-2:rgba(36,35,31,.2);--rule-3:rgba(36,35,31,.34);
 --ink:#1F4E8C;--ink-hover:#183F72;--ink-tint:#E3EBF5;
 --stamp:#3B6D11;--stamp-tint:#E5EED8;
 --marker:#E9B949;--marker-tint:#FBEFC6;
 --redpen:#A32D2D;--redpen-tint:#F7E2E0;
 --t12:12px;--t14:14px;--t17:17px;--t20:20px;--t24:24px;--t28:28px;
 --r-s:4px;--r-m:8px;
 --mono:ui-monospace,"Cascadia Mono","Consolas","SFMono-Regular",Menlo,monospace;
 --ease:cubic-bezier(.23,1,.32,1);
 --bg:var(--paper);--card:var(--sheet);--tx:var(--graphite);--mut:var(--pencil);--br:var(--rule-2);--ac:var(--ink);--ok:var(--stamp);--wr:var(--marker);--er:var(--redpen)}
*{box-sizing:border-box}
html{-webkit-font-smoothing:antialiased}
body{margin:0;font:var(--t14)/1.5 "Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;background:var(--paper);color:var(--graphite);font-variant-numeric:tabular-nums}
a{color:var(--ink);text-decoration:none}a:hover{text-decoration:underline}
code{font-family:var(--mono);font-size:12px;background:var(--sunk);padding:1px 4px;border-radius:3px}
h2{font-size:var(--t24);font-weight:600;letter-spacing:-.01em;margin:0;text-wrap:balance}
h3{font-size:var(--t17);font-weight:600;margin:0}
h4{font-size:var(--t14);font-weight:600;margin:16px 0 6px}
input,select,textarea,button{font:inherit;color:inherit}
input,select,textarea{background:var(--sunk);border:1px solid var(--rule);border-radius:var(--r-s);padding:6px 8px;min-height:32px}
input::placeholder,textarea::placeholder{color:var(--faint)}
input:hover,select:hover,textarea:hover{border-color:var(--rule-2)}
input:focus,select:focus,textarea:focus{outline:none;background:#fff;border-color:var(--ink);box-shadow:0 0 0 3px var(--ink-tint)}
input[type=checkbox]{min-height:0;accent-color:var(--ink);width:15px;height:15px;vertical-align:-2px;margin:0 6px 0 0}
input[type=file]{background:none;border:0;padding:0;font-size:var(--t12);color:var(--graphite-2)}
input:disabled{color:var(--pencil)}
textarea{width:100%;min-height:150px;font-family:var(--mono);font-size:13px;line-height:1.6;resize:vertical}
button{background:var(--sheet);border:1px solid var(--rule-2);border-radius:var(--r-s);padding:6px 12px;min-height:32px;cursor:pointer;white-space:nowrap;transition:background-color 120ms var(--ease),border-color 120ms var(--ease),transform 120ms var(--ease)}
button:hover{border-color:var(--rule-3);background:#fff}
button:active{transform:scale(.97)}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
button:disabled{opacity:.45;cursor:default;transform:none}
button.p{background:var(--ink);border-color:var(--ink);color:#fff;font-weight:600}
button.p:hover{background:var(--ink-hover)}
button.sign{background:var(--graphite);border-color:var(--graphite);color:#fff;font-weight:600;padding:8px 16px}
button.sign:hover{background:#000}
button.d{color:var(--redpen);border-color:rgba(163,45,45,.35)}
button.d:hover{background:var(--redpen-tint)}
label{color:var(--graphite-2);font-size:13px}
details>summary{cursor:pointer;color:var(--graphite-2);font-size:13px;list-style:none;display:inline-flex;gap:6px;align-items:center}
details>summary::before{content:"?";display:inline-grid;place-items:center;width:16px;height:16px;border:1px solid var(--rule-3);border-radius:50%;font-size:11px;font-weight:600}
details[open]>summary{margin-bottom:8px}
.hide{display:none!important}

.app{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:100vh}
aside{padding:20px 12px;display:flex;flex-direction:column;gap:20px;position:sticky;top:0;height:100vh}
.brand{padding:0 8px;display:flex;align-items:baseline;gap:8px}
.brand b{font-size:var(--t17);letter-spacing:-.01em}
.brand span{font-family:var(--mono);font-size:11px;color:var(--pencil)}
nav{display:flex;flex-direction:column;gap:2px}
.navh{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--graphite);padding:8px 8px 6px}
.navh::after{content:"";flex:1;height:1px;background:var(--rule-2)}
.navh~.navh{margin-top:20px}
.navh+button{margin-top:2px}
nav>button{padding-left:12px}
nav button{border:0;background:none;text-align:left;padding:6px 8px;min-height:32px;color:var(--graphite-2);border-radius:var(--r-s);display:flex;justify-content:space-between;align-items:center}
nav button:hover{background:rgba(36,35,31,.05);color:var(--graphite)}
nav button.on{background:rgba(36,35,31,.08);color:var(--graphite);font-weight:600}
.conn{margin-top:auto;padding:12px 8px 0;border-top:1px solid var(--rule);font-size:var(--t12);color:var(--graphite-2);display:flex;flex-direction:column;gap:8px}
.conn .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--faint);margin-right:6px;vertical-align:1px}
.conn .cl.on .dot{background:var(--stamp)}
.conn .cl.err .dot{background:var(--redpen)}
.conn .cl.err{color:var(--redpen)}
.conn #cbase,.conn #cfbase{font-family:var(--mono);font-size:11px;color:var(--pencil);word-break:break-all;margin-top:-6px}
.gsep{margin:6px 0 0;padding-top:14px;border-top:1px dashed var(--rule);font-size:var(--t12);color:var(--pencil)}
.conn button{align-self:flex-start;min-height:28px;padding:3px 10px;font-size:var(--t12)}

main{padding:28px 36px 64px;width:100%;border-left:1px solid var(--rule);min-width:0}
main>*{max-width:1280px}
section>.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:16px}
.lead{color:var(--graphite-2);margin:4px 0 0;max-width:72ch;text-wrap:pretty}
.card{background:var(--sheet);border:1px solid var(--rule);border-radius:var(--r-m);padding:16px;margin-bottom:16px}
.card>.card{background:transparent}
.bar-q{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.f{display:flex;flex-direction:column;gap:4px}
.f>span{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--pencil)}
.f.grow{flex:1;min-width:220px}.f.grow input{width:100%}
.opts{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px dashed var(--rule)}
.help{margin-top:12px;font-size:13px;color:var(--graphite-2);max-width:90ch}
.help p{margin:0 0 6px}
/* подсказки в строке параметров: всплывают поверх, а не раздвигают строку */
.opts details.help{position:relative}
.opts details.help[open]>summary{margin-bottom:0}
.opts details.help>:not(summary){position:absolute;top:calc(100% + 6px);left:0;z-index:30;width:min(440px,80vw);margin:0;padding:10px 12px;
 background:var(--sheet);border:1px solid var(--rule-3);border-radius:6px;box-shadow:0 6px 20px rgba(0,0,0,.12);color:var(--graphite-2);line-height:1.45}
.row{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:8px 0}
.mut{color:var(--pencil)}.ok{color:var(--stamp)}.bad{color:var(--redpen)}
.warn{color:#8A5A00;background:var(--marker-tint);padding:0 4px;border-radius:3px;font-weight:600;font-size:var(--t12)}
.od{color:var(--redpen);font-weight:600}
#lmsg,#tmsg,#dmsg,#prevmsg{font-size:13px}#lmsg:not(:empty),#tmsg:not(:empty){margin-top:10px}

.steps{display:flex;gap:4px;font-size:var(--t12);color:var(--pencil);align-items:center}
.steps i{font-style:normal;display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;border:1px solid var(--rule-3);font-size:11px;margin-right:4px}
.steps b{font-weight:500;color:var(--graphite)}.steps .sep{width:20px;height:1px;background:var(--rule-2);margin:0 6px}

.tw{overflow:auto;border:1px solid var(--rule);border-radius:var(--r-m);background:var(--sheet)}
.card .tw{margin:0 -1px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--rule)}
th{position:sticky;top:0;z-index:1;background:var(--sheet);font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--pencil);border-bottom-color:var(--rule-2);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:rgba(36,35,31,.025)}
td .mut{font-size:var(--t12)}
td.key,td a[href*="/browse/"],.key{font-family:var(--mono);font-size:13px;white-space:nowrap}
td input{background:transparent}td input:focus{background:#fff}
td.grp{background:var(--paper)!important;font-weight:600;color:var(--graphite);font-size:var(--t12);letter-spacing:.03em}
ins{text-decoration:none;color:var(--ink);font-weight:600}
del{text-decoration:line-through;text-decoration-color:var(--redpen);text-decoration-thickness:1.5px;color:var(--graphite-2)}
.op{color:var(--pencil);font-size:var(--t12);margin-right:4px}

.chip{display:inline-block;padding:1px 8px;border-radius:3px;font-size:var(--t12);font-weight:500;white-space:nowrap;line-height:1.6;background:var(--sunk);color:var(--graphite-2)}
.chip a{color:inherit}
.apply,.c-indeterminate{background:var(--ink-tint);color:var(--ink)}
.c-done{background:var(--stamp-tint);color:var(--stamp)}
.skip,.c-new{background:var(--sunk);color:var(--graphite-2)}
.error,.c-none{background:var(--redpen-tint);color:var(--redpen)}

.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.tile{background:var(--sheet);border:1px solid var(--rule);border-radius:var(--r-m);padding:16px;text-decoration:none!important;color:inherit;display:flex;flex-direction:column;gap:4px;min-height:120px;transition:background-color 120ms var(--ease)}
.tile:hover{background:#fff}
.tile>.mut:first-child{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--pencil)}
.tile .n{font-size:var(--t28);font-weight:600;letter-spacing:-.02em;line-height:1.15;margin-top:auto;align-self:flex-start;padding:0 2px;border-radius:3px}
.tile .th{font-size:var(--t12);color:var(--pencil)}
.tile.warn .n{background:var(--marker-tint);box-shadow:inset 0 -.35em 0 rgba(233,185,73,.45)}
.tile.crit .n{color:var(--redpen)}
.tile.crit .th{color:var(--redpen);font-weight:500}
.tile.err .n{color:var(--faint)}
.tile.warn{background:var(--sheet);color:inherit;padding:16px;font-size:inherit;font-weight:inherit;border-radius:var(--r-m)}
.tile.crit{border-color:rgba(163,45,45,.3)}

.bar{height:4px;background:var(--sunk);border-radius:2px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--ink);transition:width 250ms var(--ease)}
.pbar{display:flex;height:8px;border-radius:2px;overflow:hidden;background:var(--sunk);min-width:220px}.pbar i{display:block;height:100%}
.tg{border:0;background:none;padding:0 4px;min-height:0;cursor:pointer;color:var(--pencil)}
.tg:hover{color:var(--redpen);background:none}
.lkd{white-space:pre-wrap;font-size:var(--t12);background:var(--paper);border-left:2px solid var(--rule-2);padding:8px 12px;margin:4px 0;font-family:var(--mono)}
.days{display:flex;align-items:flex-end;gap:2px;height:64px}.days div{flex:1;min-width:10px;max-width:36px;background:var(--ink);opacity:.75;border-radius:2px 2px 0 0;position:relative}.days div:hover{opacity:1}.days span{position:absolute;top:-16px;left:0;right:0;text-align:center;font-size:11px;color:var(--pencil)}
.sumline{display:flex;flex-wrap:wrap;gap:24px;align-items:baseline;margin:4px 0 8px}
.big{font-size:var(--t24);font-weight:600;letter-spacing:-.01em}
#lres>.mut:first-child{padding:8px 12px;font-size:var(--t12)}
.empty{display:none;border:1px dashed var(--rule-2);border-radius:var(--r-m);padding:40px 24px;text-align:center;color:var(--graphite-2)}
.empty b{display:block;color:var(--graphite);font-size:var(--t17);font-weight:600;margin-bottom:4px}
#prevcard.hide~.empty,#tres.hide~.empty,#lres.hide~.empty,#tiles:empty+.empty{display:block}
.confirm{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;margin-top:12px;padding-top:12px;border-top:1px solid var(--rule)}

#gate{max-width:560px;margin:8vh auto 0}
#gate .card{padding:24px}
#gate details.card{margin-top:16px;padding:16px 24px}
#gate details.card>summary{font-size:var(--t14);color:var(--graphite)}
#gate details.card>summary::before{content:"+"}
#gate details.card[open]>summary::before{content:"−"}
#gate .f input{width:100%}
#gate .stack{display:flex;flex-direction:column;gap:12px;margin-top:16px}
#connmsg{font-size:13px}
.lock{font-size:var(--t12);color:var(--pencil);margin-top:16px;padding-top:12px;border-top:1px solid var(--rule)}
pre{font-family:var(--mono)}

@media (max-width:820px){
 .app{grid-template-columns:minmax(0,1fr)}
 main{border-left:0}
 nav button{gap:8px}
 aside{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:8px;padding:12px;border-right:0;border-bottom:1px solid var(--rule)}
 nav{flex-direction:row;flex-wrap:wrap;width:100%}.navh{display:none}
 .conn{margin:0;border:0;padding:0;flex-direction:row;align-items:center}
 main{padding:20px 16px 48px}
 section>.head{flex-direction:column;align-items:flex-start}
 h2{font-size:var(--t20)}
 .f{min-width:0;max-width:100%}.f select,.f input{max-width:100%}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<div class="app">
<aside>
 <div class="brand"><b>Jira Toolkit</b><span>v0.26</span></div>
 <nav>
  <div class="navh">Смотреть</div>
  <button data-t="lk">Связанные задачи</button>
  <button data-t="tree">Структура эпика</button>
  <button data-t="dash">Метрики</button>
  <div class="navh w" title="Разделы, которые пишут в Jira">Менять в Jira</div>
  <button data-t="bulk" class="on w" title="Пишет в Jira">Массовые изменения</button>
  <button data-t="sa" class="w" title="Пишет в Jira">Задачи из анализа</button>
  <button data-t="dg" class="w" title="Пишет в Jira">Доски в Jira</button>
  <button data-t="jr" class="w" title="Пишет в Jira">Журнал и откат</button>
 </nav>
 <div class="conn" id="connbox">
  <div class="cl" id="cljira"><span class="dot"></span><span id="cstate">Jira: не подключено</span></div><div id="cbase"></div>
  <div class="cl" id="clconf"><span class="dot"></span><span id="cfstate">Confluence: не подключено</span></div><div id="cfbase"></div>
  <button id="bdisc" class="hide">Отключиться</button></div>
</aside>
<main>

<div id="gate">
 <div class="card">
  <h2>Подключение к Jira</h2>
  <p class="lead">Нужен адрес Jira и ваш Personal Access Token (Jira → профиль → Personal Access Tokens). Этого достаточно для всех разделов, кроме «Задачи из анализа».</p>
  <div class="stack">
   <label class="f"><span>Адрес Jira</span><input id="base" placeholder="https://jira.company.ru" value="https://jira.moscow.alfaintra.net"></label>
   <label class="f"><span>Personal Access Token Jira</span><input id="token" type="password" placeholder="вставьте токен" autocomplete="new-password"></label>
   <label><input id="insecure" type="checkbox" checked>Не проверять SSL-сертификат</label>
   <div class="row"><button class="p" id="bconn">Подключиться к Jira</button><span id="connmsg" class="mut"></span></div>
  </div>
  <div class="lock">Токен хранится только в памяти программы и уходит только в вашу Jira. Программа доступна только с этого компьютера.</div>
 </div>
 <details class="card" id="gconf">
  <summary><b>Confluence</b> <span class="mut">— необязательно, нужен только для раздела «Задачи из анализа»</span></summary>
  <div class="stack">
   <p class="mut" style="margin:0">Свой токен Confluence: аватар → «Настройки» → «Personal Access Tokens». Подключить можно и позже — в самом разделе.</p>
   <label class="f"><span>Адрес Confluence</span><input id="gconfbase" placeholder="https://confluence.company.ru" value="https://confluence.moscow.alfaintra.net"></label>
   <label class="f"><span>Personal Access Token Confluence</span><input id="gctoken" type="password" placeholder="токен из Confluence (не из Jira)" autocomplete="new-password"></label>
   <div class="row"><button id="gcconn">Подключить Confluence</button><span id="gcmsg" class="mut"></span></div>
  </div>
 </details>
</div>

<section id="t-bulk">
 <div class="head"><div><h2>Массовые изменения</h2><p class="lead">Список задач → предпросмотр «было → станет» → запись. Каждая запись попадает в журнал, её можно откатить.</p></div>
  <div class="steps"><span><i>1</i><b>Список</b></span><span class="sep"></span><span><i>2</i>Предпросмотр</span><span class="sep"></span><span><i>3</i>Запись</span></div></div>
 <div class="card">
  <div class="bar-q">
   <label class="f"><span>Операция</span><select id="op"></select></label>
   <label class="f grow"><span>Значение для всех строк</span><input id="gval" placeholder="если значение не указано в списке"></label>
  </div>
  <div style="margin-top:12px"><textarea id="rows" placeholder="INCIDENT-239	КН&#10;INCIDENT-240	КН"></textarea></div>
  <div class="row" style="justify-content:space-between">
   <div class="row" style="margin:0"><input type="file" id="file" accept=".csv,.txt,.tsv"><label>пауза, с <input id="pause" type="number" step="0.1" min="0.2" value="0.5" style="width:64px"></label></div>
   <div class="row" style="margin:0"><span id="prevmsg" class="mut"></span><button class="p" id="bprev">Предпросмотр</button></div>
  </div>
  <details class="help"><summary>Формат списка</summary><p>По одной задаче в строке: <code>ключ</code> или <code>ключ  значение</code>. Можно вставить прямо из Excel. Разделитель: Tab, «;» или «,». Строка-заголовок «ключ» пропускается.</p><p>Сначала пробуйте на 1–2 задачах.</p></details>
 </div>
 <div class="card hide" id="prevcard">
  <div class="row" style="margin-top:0"><h3>Предпросмотр</h3><span id="prevsum" class="mut"></span></div>
  <div class="tw" style="max-height:420px"><table><thead><tr><th>Задача</th><th>Заголовок</th><th>Сейчас</th><th>Изменение</th><th>Итог</th></tr></thead><tbody id="prevbody"></tbody></table></div>
  <div class="confirm"><label><input type="checkbox" id="conf">Понимаю, что изменения будут записаны в Jira. Журнал сохранится, откат доступен.</label>
   <button class="sign" id="bapply" disabled>Записать в Jira</button></div>
 </div>
 <div class="empty"><b>Здесь появится предпросмотр</b>Вставьте список задач и нажмите «Предпросмотр». В Jira ничего не запишется, пока вы не подтвердите.</div>
 <div class="card hide" id="jobcard">
  <div class="row" style="margin-top:0"><h3 id="jobtitle">Запись в Jira</h3><span id="jobsum" class="mut"></span><button id="bcancel" style="margin-left:auto">Остановить</button></div>
  <div class="bar"><i id="jobbar" style="width:0%"></i></div>
  <div id="jobres" class="tw" style="max-height:340px;margin-top:12px"></div>
 </div>
</section>

<section id="t-dg" class="hide">
 <div class="head"><div><h2>Доски в Jira</h2><p class="lead">Создаёт фильтры и два дашборда по проектам: «Рабочий» и «Статистика разбора потока». Сначала покажет план, потом создаст.</p></div></div>
 <div class="card">
  <div class="bar-q">
   <label class="f grow"><span>Проекты</span><input id="dgp" placeholder="INCIDENT, OPERNAP"></label>
   <label class="f"><span>Типы задач</span><input id="dgt" size="16" placeholder="необязательно"></label>
   <label class="f"><span>Компоненты</span><input id="dgc" size="16" placeholder="необязательно"></label>
   <button class="p" id="dgprev">Показать план</button>
  </div>
  <div class="opts">
   <label>период «Рабочего», дн. <input id="dgd" type="number" value="30" min="1" max="365" style="width:64px"></label>
   <label>период статистики, дн. <input id="dgdf" type="number" value="90" min="7" max="730" style="width:64px"></label>
   <label>если дашборд уже есть <select id="dgex"><option value="skip">пропустить</option><option value="new">создать новый</option></select></label>
  </div>
 </div>
 <div id="dgplan" class="hide">
  <div class="card">
   <h3>План</h3>
   <div class="bar-q" style="margin-top:12px">
    <label class="f grow"><span>Доска 1 — название</span><input id="dgn1"></label>
    <label class="f grow"><span>Доска 2 — название</span><input id="dgn2"></label>
   </div>
   <details class="help"><summary>Jira не даёт создать дашборд сама?</summary>
    <p>Создайте в Jira два пустых рабочих стола («Рабочий стол → Создать панель») и вставьте ссылки ниже. Программа создаст фильтры и добавит гаджеты в эти доски. Иначе оставьте поля пустыми.</p>
    <div class="bar-q"><label class="f grow"><span>Ссылка на доску 1</span><input id="dgi1"></label><label class="f grow"><span>Ссылка на доску 2</span><input id="dgi2"></label></div>
    <div class="row"><button id="dgprobe">Проверить API гаджетов</button><span class="mut">только чтение, ничего не меняет</span></div>
    <div id="dgprobeout"></div>
   </details>
   <div class="tw" style="max-height:520px;margin-top:12px"><table><thead><tr><th></th><th>Виджет</th><th>Тип гаджета</th><th>Фильтр (JQL)</th></tr></thead><tbody id="dgbody"></tbody></table></div>
   <div class="confirm"><span id="dgsum" class="mut"></span><button class="sign" id="dgmk">Создать в Jira</button></div>
   <div id="dgwarn" class="mut" style="font-size:13px"></div>
  </div>
 </div>
 <div id="dgres"></div>
</section>

<section id="t-dash" class="hide">
 <div class="head"><div><h2>Метрики</h2><p class="lead">Числа по JQL-запросам с порогами, наборы под роль в проекте.</p></div><span id="dmsg" class="mut"></span></div>
 <div class="card">
  <div class="bar-q">
   <label class="f"><span>Роль</span><select id="role"></select></label>
   <label class="f"><span>Проект</span><input id="proj" size="14" placeholder="INCIDENT" class="key"></label>
   <button class="p" id="drun">Обновить</button>
  </div>
 </div>
 <div id="tiles" class="tiles"></div>
 <div class="empty"><b>Метрики ещё не считались</b>Выберите роль и проект, затем нажмите «Обновить».</div>
 <details class="card" style="margin-top:16px"><summary>Настроить карточки</summary>
  <p class="help" style="margin-top:0">Карточка = запрос JQL и пороги. Число ≥ «внимание» выделяется маркером, ≥ «критично» — красным (0 — порог выключен). <code>{PROJECT}</code> подставляется из поля «Проект».</p>
  <div class="tw"><table><thead><tr><th>Название</th><th>JQL</th><th>Внимание</th><th>Критично</th><th></th></tr></thead><tbody id="cardsbody"></tbody></table></div>
  <div class="row"><button id="dadd">+ Карточка</button><button id="dsave">Сохранить настройки</button></div>
 </details>
</section>

<section id="t-tree" class="hide">
 <div class="head"><div><h2>Структура эпика</h2><p class="lead">Дерево задач, подзадач и связей со статусами, датами и временем в работе.</p></div></div>
 <div class="card">
  <div class="bar-q">
   <label class="f"><span>Эпик</span><input id="tkey" size="18" placeholder="ALFAMLM4-100" class="key"></label>
   <label class="f"><span>Глубина</span><select id="tdepth"><option>1</option><option>2</option><option selected>3</option><option>4</option><option>5</option></select></label>
   <label class="f"><span>Максимум задач</span><input id="tmax" type="number" value="300" min="10" max="1000" style="width:90px"></label>
   <button class="p" id="tgo">Собрать</button><button id="tcancel" class="hide">Остановить</button><button id="tcsv" class="hide">Скачать CSV</button>
  </div>
  <div class="opts"><label><input type="checkbox" id="tlinks" checked>Включать связи между задачами (blocks, relates и др.)</label>
   <label class="f"><span>Папки</span><select id="tgroup"><option value="">без папок</option><option value="project" selected>по проекту</option><option value="type">по типу задачи</option><option value="assignee">по исполнителю</option><option value="status">по статусу</option></select></label>
   <details class="help" style="margin:0"><summary>Как раскладываются папки</summary><p>Эпик остаётся наверху, под ним — папка на каждый проект (тип, исполнителя, статус). Внутри папки сохраняется вложенность: задача стоит под ближайшим «предком» из той же папки, иначе — прямо в папке. Эти же папки создаются в структуре Jira и попадают в столбец «Папка» в CSV.</p></details>
   <details class="help" style="margin:0"><summary>Как считаются даты</summary><p>Каждая задача показана один раз — под той, через которую найдена. «Начало» — первый переход в статус категории «В работе» (≈ — оценка по дате создания, если истории нет). «В работе» — календарные дни от начала до завершения или до сегодня.</p></details></div>
  <div class="opts hide" id="tmkrow"><label class="f grow"><span>Структура в Jira (плагин Structure)</span><input id="tsname" placeholder="Название структуры"></label>
   <button class="sign" id="tmk" style="align-self:flex-end">Создать структуру в Jira</button></div>
  <details id="tfrm" class="help hide"><summary>Столбец «Дней в работе» в Structure</summary>
   <p>Добавляется один раз: откройте структуру → «+» справа от заголовков → Formula → вставьте формулу, назовите «Дней в работе», формат Number. Сохраните представление (View) и используйте его для других структур.</p>
   <textarea id="tfrmtxt" rows="5" style="min-height:90px;font-size:12px" readonly></textarea>
   <div class="row"><button id="tfrmcp">Скопировать формулу</button><span id="tfrmmsg" class="mut"></span></div>
   <p>Имена <code>resolved</code>, <code>status</code>, <code>created</code> подсвечены красным, пока не привязаны: в блоке «Имена» выберите «Дата резолюции», «Статус», «Создано». Если ругнётся на <code>DAYS_BETWEEN</code>, пришлите текст ошибки.</p>
  </details>
  <div id="tmsg" class="mut"></div>
 </div>
 <div class="card hide" id="tsum"></div>
 <div class="tw hide" id="tres"></div>
 <div class="empty"><b>Укажите эпик</b>Программа обойдёт задачи эпика, подзадачи и связи на выбранную глубину.</div>
</section>

<section id="t-lk" class="hide">
 <div class="head"><div><h2>Связанные задачи</h2><p class="lead">Все задачи, связанные с указанной: дата, статус и нужные поля Jira. Выгрузка в CSV для Excel.</p></div></div>
 <div class="card">
  <div class="bar-q">
   <label class="f"><span>Задача</span><input id="lkey" size="18" placeholder="INCIDENT-1027" class="key"></label>
   <label class="f grow"><span>Доп. фильтр JQL</span><input id="ljql" placeholder="project = OPERNAP"></label>
   <button class="p" id="lgo">Собрать</button><button id="lcancel" class="hide">Остановить</button><button id="lcsv" class="hide">Скачать CSV</button>
  </div>
  <div class="opts">
   <label>поля в колонки <input id="lfields" size="40" value="Наименование клиента, ID Клиента" title="Названия полей как в Jira или id (customfield_12345), через запятую"></label>
   <label>максимум <input id="lmax" type="number" value="1000" min="10" max="5000" style="width:80px"></label>
   <label><input type="checkbox" id="lparse" checked>Искать КУС по ссылке в описании</label>
   <details class="help" style="margin:0"><summary>Как ищется КУС</summary><p>КУС клиента берётся из поля «Наименование клиента». КУС по ссылке партнёра ищется в описании: 6 символов латиница/цифры рядом со словом «ссылка». Если не сказано явно, строка помечается <span class="warn">проверить</span>; значение можно поправить в таблице, правка попадёт в CSV.</p></details>
  </div>
  <div id="lmsg" class="mut"></div>
 </div>
 <div class="card hide" id="lsum"></div>
 <div class="tw hide" id="lres"></div>
 <div class="empty"><b>Укажите задачу</b>Например, INCIDENT-1027 — получите все связанные с ней задачи с КУС и статусами.</div>
</section>

<section id="t-sa" class="hide">
 <div class="head"><div><h2>Задачи из анализа</h2><p class="lead">Страница системного анализа в Confluence → декомпозиция → задачи в Jira по шаблону команды.</p></div>
  <div class="steps"><span><i>1</i><b>Страница</b></span><span class="sep"></span><span><i>2</i>Декомпозиция</span><span class="sep"></span><span><i>3</i>Создание</span></div></div>
 <div class="card" id="sacon">
  <div class="bar-q">
   <label class="f grow"><span>Адрес Confluence</span><input id="confbase" value="https://confluence.moscow.alfaintra.net"></label>
   <label class="f grow"><span>Personal Access Token Confluence</span><input id="ctoken" type="password" placeholder="токен из Confluence, не из Jira" autocomplete="new-password"></label>
   <button class="p" id="cconn">Подключить Confluence</button>
  </div>
  <div id="cmsg" class="mut" style="margin-top:8px"></div>
  <details class="help"><summary>Где взять токен</summary><p>Confluence → аватар → «Настройки» → «Personal Access Tokens» → «Create token». Токен Jira к Confluence не подходит. Токен хранится только в памяти программы.</p></details>
 </div>
 <div class="card">
  <div class="bar-q">
   <label class="f grow"><span>Страница системного анализа</span><input id="saurl" placeholder="https://confluence.../pages/3811512924/..."></label>
   <label class="f"><span>Проект</span><input id="saprj" value="AMLM" size="8" class="key"></label>
   <label class="f"><span>Тип задачи</span><input id="satype" value="Задача" size="10"></label>
   <button class="p" id="sago">Разобрать</button>
  </div>
  <div id="samsg" class="mut" style="margin-top:8px"></div>
 </div>
 <div class="card hide" id="sainfo"></div>
 <div class="card hide" id="saset">
  <h3 style="margin-top:0">Параметры задач</h3>
  <div class="bar-q">
   <label class="f grow"><span>Продукт (начало названия задач)</span><input id="saprod"></label>
   <label class="f"><span>Шаблон описания</span><select id="satpl"><option value="rework">доработка продукта</option><option value="new">разработка продукта</option></select></label>
   <label class="f"><span>Эпик</span><input id="saepic" size="14" class="key" placeholder="AMLM-123"></label>
  </div>
  <div class="bar-q" style="margin-top:10px">
   <label class="f grow"><span>Влияние / зачем нужна доработка</span><input id="saimp" placeholder="подставится в шаблон"></label>
  </div>
  <div style="margin-top:12px"><b style="font-size:13px">Дополнительные места доработки</b> <span class="mut">— отдельная задача на каждое, например «бандле ДК»</span>
   <div id="saextra"></div><button id="saaddx" style="margin-top:6px">+ Место доработки</button></div>
  <div class="row"><button id="saredo">Обновить черновик</button><span class="mut">Правки в карточках ниже при этом сбросятся.</span></div>
 </div>
 <div class="card hide" id="saprev">
  <div class="row" style="margin-top:0"><h3 style="margin:0">Предпросмотр</h3><span id="saprevsum" class="mut"></span></div>
  <div id="satasks"></div>
  <div class="confirm"><label><input type="checkbox" id="saconf">Понимаю, что задачи будут созданы в Jira.</label>
   <button class="sign" id="samk" disabled>Создать задачи</button></div>
 </div>
 <div class="card hide" id="sares"></div>
 <div class="empty"><b>Укажите страницу</b>Программа прочитает разделы СА, источники и примеры и предложит задачи: анализ, реализация в DWH, места доработки и одну задачу на тестирование.</div>
</section>

<section id="t-jr" class="hide">
 <div class="head"><div><h2>Журнал и откат</h2><p class="lead">Каждая запись в Jira сохраняется в папке <code>jira_toolkit_data</code> рядом с программой. Откат возвращает задачи в состояние до записи.</p></div></div>
 <div class="tw"><table><thead><tr><th>Журнал</th><th>Создан</th><th>Изменений</th><th></th></tr></thead><tbody id="jrbody"><tr><td colspan="4" class="mut">Журналы появятся после подключения</td></tr></tbody></table></div>
 <div class="card hide" id="jrjob" style="margin-top:16px"><div class="row" style="margin-top:0"><h3 id="jrtitle"></h3><span id="jrsum" class="mut"></span></div><div class="bar"><i id="jrbar" style="width:0%"></i></div><div id="jrres" class="tw" style="max-height:300px;margin-top:12px"></div></div>
</section>
</main>
</div>
<script>
const SECRET="__SECRET__";
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function api(path,body){
  const o={headers:{"X-Session":SECRET,"Content-Type":"application/json"}};
  if(body!==undefined){o.method="POST";o.body=JSON.stringify(body);}
  const r=await fetch(path,o);let j={};try{j=await r.json();}catch(e){}
  if(!r.ok)throw new Error(j.error||("HTTP "+r.status));return j;
}
const OPS={component_add:"Добавить компонент",component_if_empty:"Поставить компонент, только если компонентов нет",label_add:"Добавить метку",label_remove:"Убрать метку",comment_add:"Добавить комментарий"};
const OPK={component_add:["Компонент","+"],component_if_empty:["Компонент","+"],label_add:["Метка","+"],label_remove:["Метка","-"],comment_add:["Комментарий","+"]};
function opCell(op,v){const k=OPK[op]||[OPS[op]||op,"+"];
  return `<span class="op">${esc(k[0])}</span>`+(k[1]==="-"?`<del>${esc(v)}</del>`:`<ins>${op==="comment_add"?"":"+ "}${esc(v)}</ins>`);}
function plural(n,a,b,c){const m=n%10,h=n%100;return m===1&&h!==11?a:(m>=2&&m<=4&&(h<10||h>=20))?b:c;}
$("op").innerHTML=Object.entries(OPS).map(([k,v])=>`<option value="${k}">${v}</option>`).join("");
let connected=false,jiraBase="";
let curTab="bulk";
function showTab(t){curTab=t;
  document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("on",x.dataset.t===t));
  ["bulk","sa","dg","dash","tree","lk","jr"].forEach(k=>$("t-"+k).classList.toggle("hide",!connected||k!==t));
  $("gate").classList.toggle("hide",connected);
  if(t==="jr")loadJournals();}
function setConn(s){connected=!!s.connected;jiraBase=String(s.base||"").replace(/\/$/,"");
  $("connbox").classList.toggle("on",connected);
  $("cljira").classList.toggle("on",connected);
  $("cstate").textContent=connected?("Jira"+(s.user?": "+s.user:"")):"Jira: не подключено";
  $("cbase").textContent=connected?String(s.base||"").replace(/^https?:\/\//,""):"";
  $("bdisc").classList.toggle("hide",!connected);
  $("connmsg").className="mut";$("connmsg").textContent="";
  showTab(curTab);}
$("token").addEventListener("keydown",e=>{if(e.key==="Enter")$("bconn").click();});
$("gctoken").addEventListener("keydown",e=>{if(e.key==="Enter")$("gcconn").click();});
$("gcconn").onclick=async()=>{$("gcmsg").className="mut";$("gcmsg").textContent="Проверяю...";
  try{const c=await api("/api/conf/connect",{base:$("gconfbase").value,token:$("gctoken").value,insecure:$("insecure").checked});
    $("gctoken").value="";setConf(c);
    try{const st=JSON.parse(localStorage.getItem("jt_sa")||"{}");st.confbase=$("gconfbase").value;localStorage.setItem("jt_sa",JSON.stringify(st));$("confbase").value=$("gconfbase").value;}catch(e){}
  }catch(e){setConf({cerror:e.message});}};
$("bconn").onclick=async()=>{$("connmsg").className="mut";$("connmsg").textContent="Проверяю...";
  try{const j=await api("/api/connect",{base:$("base").value,token:$("token").value,insecure:$("insecure").checked});
    $("token").value="";setConn(j);loadJournals();}catch(e){$("connmsg").className="bad";$("connmsg").textContent="Ошибка: "+e.message;}};
$("bdisc").onclick=async()=>{await api("/api/disconnect",{});setConn({connected:false});if(typeof setConf==="function")setConf({});};
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>showTab(b.dataset.t));
$("file").onchange=e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=()=>{$("rows").value=r.result;};r.readAsText(f,"utf-8");};

/* ---- массовые изменения ---- */
function parseRows(){
  const op=$("op").value,g=$("gval").value.trim(),out=[];
  $("rows").value.split(/\r?\n/).map(l=>l.trim()).filter(Boolean).forEach(l=>{
    let m=l.search(/[\t;,]/),key=m<0?l:l.slice(0,m).trim(),val=m<0?"":l.slice(m+1).trim().replace(/^["']|["']$/g,"");
    if(["key","ключ"].includes(key.toLowerCase()))return;
    out.push({key:key,op:op,value:val||g});});
  return out;}
let lastRows=[];
$("bprev").onclick=async()=>{
  if(!connected){$("prevmsg").textContent="Сначала подключитесь к Jira.";return;}
  lastRows=parseRows();if(!lastRows.length){$("prevmsg").textContent="Список пуст.";return;}
  $("prevmsg").textContent="Читаю задачи...";$("bapply").disabled=true;$("conf").checked=false;
  try{const j=await api("/api/bulk/preview",{rows:lastRows});
    const c={apply:0,skip:0,error:0};
    $("prevbody").innerHTML=j.rows.map(r=>{c[r.action]++;return `<tr><td class="key">${esc(r.key)}</td><td>${esc(r.summary)}</td><td class="mut">${esc(r.current)||"—"}</td>
      <td style="white-space:nowrap">${opCell(r.op,r.value)}</td><td><span class="chip ${r.action}">${r.action==="apply"?"к записи":r.action==="skip"?"пропуск":"ошибка"}</span> <span class="mut">${esc(r.reason)}</span></td></tr>`;}).join("");
    $("prevsum").textContent=`к записи ${c.apply} · пропуск ${c.skip} · ошибок ${c.error}`;
    $("bapply").textContent=c.apply?`Записать ${c.apply} ${plural(c.apply,"изменение","изменения","изменений")} в Jira`:"Нечего записывать";
    $("prevcard").classList.remove("hide");$("prevmsg").textContent="";$("bapply").dataset.n=c.apply;
  }catch(e){$("prevmsg").textContent="Ошибка: "+e.message;}};
$("conf").onchange=()=>{$("bapply").disabled=!($("conf").checked&&+($("bapply").dataset.n||0)>0);};
let curJob=null;
async function poll(jid,ui){
  for(;;){const j=await api("/api/job/"+jid);
    const pct=j.total?Math.round(100*j.done/j.total):0;
    $(ui.bar).style.width=pct+"%";$(ui.sum).textContent=`${j.done} из ${j.total} · ошибок: ${j.failed} · ${j.status}`;
    if(j.status!=="running"){$(ui.res).innerHTML=`<table><thead><tr><th>Задача</th><th>Операция</th><th>Результат</th><th>Детали</th></tr></thead><tbody>`+
      j.results.map(r=>`<tr><td class="key">${esc(r.key)}</td><td>${opCell(r.op,r.value)}</td><td>${esc(r.result)}</td><td>${esc(r.details)}</td></tr>`).join("")+`</tbody></table>`+
      (j.error?`<div class="bad">${esc(j.error)}</div>`:"");return j;}
    await new Promise(r=>setTimeout(r,1000));}}
$("bapply").onclick=async()=>{
  $("bapply").disabled=true;$("jobcard").classList.remove("hide");
  try{const s=await api("/api/bulk/apply",{rows:lastRows,pause:+$("pause").value||0.5});curJob=s.job;
    await poll(s.job,{bar:"jobbar",sum:"jobsum",res:"jobres"});loadJournals();
  }catch(e){$("jobsum").textContent="Ошибка: "+e.message;}};
$("bcancel").onclick=()=>{if(curJob)api("/api/job/"+curJob+"/cancel",{});};

/* ---- журнал ---- */
async function loadJournals(){
  if(!connected)return;
  try{const j=await api("/api/journals");
    $("jrbody").innerHTML=j.map(x=>`<tr><td class="key">${esc(x.id)}</td><td>${esc(x.created)}</td><td>${x.count}</td><td><button class="d" data-j="${esc(x.id)}">Откатить</button></td></tr>`).join("")||`<tr><td colspan="4" class="mut">Записей ещё нет — они появятся после первой записи в Jira</td></tr>`;
    document.querySelectorAll("#jrbody button").forEach(b=>b.onclick=async()=>{
      if(!confirm("Откатить изменения из журнала "+b.dataset.j+"? Добавленное будет убрано, убранное - возвращено."))return;
      $("jrjob").classList.remove("hide");$("jrtitle").textContent="Откат "+b.dataset.j;
      try{const s=await api("/api/bulk/rollback",{journal:b.dataset.j});await poll(s.job,{bar:"jrbar",sum:"jrsum",res:"jrres"});}catch(e){$("jrsum").textContent="Ошибка: "+e.message;}});
  }catch(e){}}

/* ---- дашборд ---- */
let dash=null,cards=[];
function renderCards(){
  $("cardsbody").innerHTML=cards.map((c,i)=>`<tr><td><input data-i="${i}" data-f="name" value="${esc(c.name)}" size="24"></td>
   <td><input data-i="${i}" data-f="jql" value="${esc(c.jql)}" style="width:100%;min-width:320px"></td>
   <td><input data-i="${i}" data-f="warn" type="number" value="${c.warn||0}" style="width:70px"></td>
   <td><input data-i="${i}" data-f="crit" type="number" value="${c.crit||0}" style="width:70px"></td>
   <td><button data-del="${i}">✕</button></td></tr>`).join("");
  document.querySelectorAll("#cardsbody input").forEach(inp=>inp.oninput=()=>{const c=cards[+inp.dataset.i];c[inp.dataset.f]=inp.type==="number"?+inp.value:inp.value;});
  document.querySelectorAll("#cardsbody button").forEach(b=>b.onclick=()=>{cards.splice(+b.dataset.del,1);renderCards();});}
async function loadDash(){
  dash=await api("/api/dash/config");
  $("role").innerHTML=Object.keys(dash.presets).map(r=>`<option>${esc(r)}</option>`).join("");
  $("role").value=dash.role;$("proj").value=dash.project||"";cards=JSON.parse(JSON.stringify(dash.presets[$("role").value]||[]));renderCards();}
$("role").onchange=()=>{dash.presets[dash.role]=cards;dash.role=$("role").value;cards=JSON.parse(JSON.stringify(dash.presets[dash.role]||[]));renderCards();$("tiles").innerHTML="";};
$("dadd").onclick=()=>{cards.push({name:"Новая метрика",jql:"",warn:0,crit:0});renderCards();};
let dgPlan=null;
function dgSum(){const sel=[...document.querySelectorAll(".dgsel:checked")].map(x=>x.value);if(!dgPlan)return;
  const g=dgPlan.gadgets.filter(x=>sel.includes(x.id));const boards=new Set(g.map(x=>x.dash)).size,fl=new Set(g.map(x=>x.filter)).size;
  $("dgsum").textContent=`Будет создано: досок ${boards}, фильтров ${fl} (нужны гаджетам), гаджетов ${g.length}.`;}
document.addEventListener("change",e=>{if(e.target.classList&&e.target.classList.contains("dgsel"))dgSum();});
function dgBody(){return {projects:$("dgp").value,types:$("dgt").value.split(","),components:$("dgc").value.split(","),days:+$("dgd").value||30,days_flow:+$("dgdf").value||90,
  onexist:$("dgex").value,dash_names:{work:$("dgn1").value,flow:$("dgn2").value},dash_ids:{work:$("dgi1").value,flow:$("dgi2").value},
  selected:[...document.querySelectorAll(".dgsel:checked")].map(x=>x.value)};}
$("dgprev").onclick=async()=>{$("dgres").innerHTML="";
  try{const b=dgBody();const p=await api("/api/dashgen/preview",b);dgPlan=p;
    $("dgn1").value=p.dash_names.work;$("dgn2").value=p.dash_names.flow;
    const grp=[["work","Доска 1: "+p.dash_names.work],["flow","Доска 2: "+p.dash_names.flow]];
    $("dgbody").innerHTML=grp.map(([k,t])=>`<tr><td colspan="4" class="grp">${esc(t)}</td></tr>`+p.gadgets.filter(g=>g.dash===k).map(g=>`<tr><td><input type="checkbox" class="dgsel" value="${esc(g.id)}" checked></td><td>${esc(g.title)}</td><td>${esc(g.kind)}${g.auto?"":' <span class="mut">(вручную)</span>'}</td><td><code>${esc(g.jql)}</code></td></tr>`).join("")).join("");
    dgSum();
    const w=[];if(p.dash_exists.work)w.push("дашборд «"+p.dash_names.work+"» уже есть");if(p.dash_exists.flow)w.push("дашборд «"+p.dash_names.flow+"» уже есть");
    const ef=Object.values(p.filters).filter(f=>f.exists).length;if(ef)w.push("фильтров с такими названиями: "+ef+" (с тем же JQL будут использованы повторно)");
    $("dgwarn").textContent=w.join("; ");$("dgplan").classList.remove("hide");
  }catch(e){$("dgres").innerHTML=`<div class="bad">Ошибка: ${esc(e.message)}</div>`;}};
function dgProbeHtml(p){if(!p)return"";return `<h4>Диагностика API гаджетов (доска ${esc(p.dash_id)})</h4><pre style="white-space:pre-wrap;font-size:12px">`+esc(p.probes.map(x=>x.code+"  "+x.path+"  "+x.note).join("\n"))+(p.specs.length?"\n\nГаджеты, которые видит Jira:\n"+p.specs.join("\n"):"")+"</pre>";}
$("dgprobe").onclick=async()=>{$("dgprobeout").innerHTML='<div class="mut">Проверяю...</div>';
  try{const b=dgBody();const p=await api("/api/dashgen/probe",b);$("dgprobeout").innerHTML=dgProbeHtml(p);}catch(e){$("dgprobeout").innerHTML=`<div class="bad">Ошибка: ${esc(e.message)}</div>`;}};
$("dgmk").onclick=async()=>{const b=dgBody();if(!b.selected.length){$("dgres").innerHTML='<div class="bad">Отметьте хотя бы один виджет.</div>';return;}
  if(!confirm(`Создать в Jira фильтры и дашборды для: ${b.projects}?\nВиджетов: ${b.selected.length}`))return;
  $("dgmk").disabled=true;$("dgres").innerHTML='<div class="mut">Создаю в Jira...</div>';
  try{const r=await api("/api/dashgen/create",b);
    let h="<h4>Фильтры</h4><ul>"+r.filters.map(f=>`<li>${esc(f.name)}: ${esc(f.status)}</li>`).join("")+"</ul>";
    h+=r.dashboards.map(d=>`<h4>${d.url?`<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a>`:esc(d.name)} <span class="mut">${esc(d.status)}</span></h4><ul>`+d.gadgets.map(g=>`<li>${esc(g.title)}: ${esc(g.status)}</li>`).join("")+"</ul>").join("");
    if(r.manual.length)h+=`<h4>Добавить вручную (Добавить гаджет → выбрать фильтр)</h4><ul>`+r.manual.map(m=>`<li>${esc(m.dash)}: «${esc(m.title)}», гаджет «${esc(m.kind)}», фильтр «${esc(m.filter)}»${m.filter_url?` (<a href="${esc(m.filter_url)}" target="_blank" rel="noopener">открыть</a>)`:""}. <span class="mut">${esc(m.why)}</span></li>`).join("")+"</ul>";
    if(r.sample)h+=`<h4>Ответ Jira на добавление гаджета (для диагностики)</h4><pre style="white-space:pre-wrap;font-size:12px">${esc(r.sample)}</pre>`;
    h+=dgProbeHtml(r.probe);
    $("dgres").innerHTML=h;
  }catch(e){$("dgres").innerHTML=`<div class="bad">Ошибка: ${esc(e.message)}</div>`;}
  $("dgmk").disabled=false;};
$("dsave").onclick=async()=>{dash.presets[dash.role]=cards;dash.project=$("proj").value;await api("/api/dash/save",{config:dash});$("dmsg").textContent="Сохранено";setTimeout(()=>$("dmsg").textContent="",2000);};
$("drun").onclick=async()=>{
  if(!connected){$("dmsg").textContent="Сначала подключитесь к Jira.";return;}
  $("dmsg").textContent="Считаю...";$("tiles").innerHTML="";
  try{const j=await api("/api/dash/run",{cards:cards,project:$("proj").value.trim()});
    $("tiles").innerHTML=j.cards.map((r,i)=>{const c=cards[i]||{};let cl="";
      if(r.error)cl="err";else if(c.crit&&r.total>=c.crit)cl="crit";else if(c.warn&&r.total>=c.warn)cl="warn";
      const th=r.error?esc(r.error):cl==="crit"?`выше критичного порога ${c.crit}`:cl==="warn"?`выше порога ${c.warn}`:(c.warn||c.crit)?`порог ${c.warn||"—"} / ${c.crit||"—"}`:"без порога";
      return `<a class="tile ${cl}" ${r.link?`href="${esc(r.link)}" target="_blank" rel="noopener"`:""} title="${esc(r.jql)}"><div class="mut">${esc(r.name)}</div>
        <div class="n">${r.error?"—":r.total}</div><div class="th">${th}</div></a>`;}).join("");
    $("dmsg").textContent="Обновлено "+new Date().toLocaleTimeString();
  }catch(e){$("dmsg").textContent="Ошибка: "+e.message;}};
/* всплывающие подсказки: одна открыта, закрываются кликом мимо и Esc */
document.addEventListener("click",e=>{document.querySelectorAll(".opts details.help[open]").forEach(d=>{if(!d.contains(e.target))d.open=false;});});
document.querySelectorAll(".opts details.help").forEach(d=>d.addEventListener("toggle",()=>{
  const pop=d.querySelector(":scope>:not(summary)");if(!pop||!d.open)return;pop.style.left="0";pop.style.right="auto";
  const r=pop.getBoundingClientRect();if(r.right>window.innerWidth-8){pop.style.left="auto";pop.style.right="0";}}));
document.addEventListener("keydown",e=>{if(e.key==="Escape")document.querySelectorAll(".opts details.help[open]").forEach(d=>d.open=false);});
/* ---- задачи из анализа ---- */
let saPage=null,saTasks=[],saInfo=null;
try{const st=JSON.parse(localStorage.getItem("jt_sa")||"{}");["saprj","satype","confbase"].forEach(k=>{if(st[k])$(k).value=st[k];});if(st.confbase)$("gconfbase").value=st.confbase;}catch(e){}
function saSave(){try{localStorage.setItem("jt_sa",JSON.stringify({saprj:$("saprj").value,satype:$("satype").value,confbase:$("confbase").value}));}catch(e){}}
function setConf(s){const on=!!s.cconnected;
  $("clconf").classList.toggle("on",on);$("clconf").classList.toggle("err",!!s.cerror);
  $("cfstate").textContent=on?("Confluence"+(s.cuser?": "+s.cuser:"")):s.cerror?"Confluence: токен не принят":"Confluence: не подключено";
  $("clconf").title=s.cerror||"";
  $("gcmsg").className=on?"ok":s.cerror?"bad":"mut";$("gcmsg").textContent=on?("Подключено"+(s.cuser?": "+s.cuser:"")):s.cerror?("Ошибка: "+s.cerror):"";
  $("cfbase").textContent=on?String(s.cbase||"").replace(/^https?:\/\//,""):"";
  $("cmsg").className=on?"ok":s.cerror?"bad":"mut";$("cmsg").textContent=on?("Confluence подключён"+(s.cuser?": "+s.cuser:"")):s.cerror?("Confluence не подключён: "+s.cerror):"";
  $("ctoken").closest(".bar-q").classList.toggle("hide",on);}
api("/api/state").then(setConf).catch(()=>{});
$("ctoken").addEventListener("keydown",e=>{if(e.key==="Enter")$("cconn").click();});
$("cconn").onclick=async()=>{$("cmsg").className="mut";$("cmsg").textContent="Проверяю...";
  try{const j=await api("/api/conf/connect",{base:$("confbase").value,token:$("ctoken").value,insecure:$("insecure").checked});$("ctoken").value="";saSave();setConf(j);}
  catch(e){$("cmsg").className="bad";$("cmsg").textContent="Ошибка: "+e.message;}};
function saExtraRow(n,l){const d=document.createElement("div");d.className="bar-q";d.style.marginTop="6px";
  d.innerHTML=`<label class="f grow"><span>Где доработать</span><input class="xn" value="${esc(n||"")}" placeholder="бандле ДК"></label><label class="f grow"><span>Ссылка на описание (необязательно)</span><input class="xl" value="${esc(l||"")}"></label><button class="tg" title="Убрать" style="align-self:flex-end;font-size:16px">✕</button>`;
  d.querySelector("button").onclick=()=>d.remove();$("saextra").appendChild(d);}
$("saaddx").onclick=()=>saExtraRow();
$("sago").onclick=async()=>{
  if(!$("saurl").value.trim()){$("samsg").className="bad";$("samsg").textContent="Вставьте ссылку на страницу.";return;}
  saSave();$("samsg").className="mut";$("samsg").textContent="Читаю страницу и проверяю Jira...";$("sago").disabled=true;
  ["sainfo","saset","saprev","sares"].forEach(k=>$(k).classList.add("hide"));
  try{const r=await api("/api/sa/parse",{url:$("saurl").value,project:$("saprj").value,issuetype:$("satype").value});
    saPage=r.page;saInfo=r.info;$("samsg").textContent="";
    const i=r.info,ep=i.epic_info;
    $("sainfo").innerHTML=`<div class="row" style="margin-top:0"><b><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a></b><span class="mut">версия ${esc(i.version)}</span></div>
     <div class="row mut"><span>продукт: <b>${esc(i.product)}</b></span><span>дата запуска: <b>${esc(i.date||"—")}</b></span>
      <span>источников: <b>${i.sources.length}</b></span><span>пунктов раздела 3: <b>${i.sections.length}</b></span><span>примеров: <b>${i.examples.length}</b></span></div>
     <div class="row mut">${i.links.map(l=>`<span>${esc(l.label)}: <span class="key">${esc(l.key)}</span></span>`).join("")}</div>
     ${ep?`<div class="row"><span>Эпик <span class="key">${esc(i.epic)}</span> — ${esc(ep.summary)} <span class="chip c-${ep.done?"done":"indeterminate"}">${esc(ep.status)}</span></span></div>`:""}
     ${r.warnings.map(w=>`<div class="bad">${esc(w)}</div>`).join("")}
     ${r.duplicates.length?`<details class="help" open><summary>Похожие задачи уже есть в ${esc($("saprj").value)} (${r.duplicates.length})</summary>${r.duplicates.map(d=>`<div><a class="key" href="#" data-k="${esc(d.key)}" target="_blank" rel="noopener">${esc(d.key)}</a> ${esc(d.summary)} <span class="mut">· ${esc(d.status)} · ${esc(d.created)}</span></div>`).join("")}</details>`:""}`;
    $("sainfo").querySelectorAll("a[data-k]").forEach(a=>a.href=jiraBase+"/browse/"+a.dataset.k);
    $("sainfo").classList.remove("hide");
    $("saprod").value=i.product;$("satpl").value=i.is_update?"rework":"new";
    $("saepic").value=ep&&ep.done?"":(i.epic||"");
    $("saextra").innerHTML="";$("saset").classList.remove("hide");
    await saDraft();
  }catch(e){$("samsg").className="bad";$("samsg").textContent="Ошибка: "+e.message;}
  $("sago").disabled=false;};
async function saDraft(){
  const extra=[...$("saextra").querySelectorAll(".bar-q")].map(d=>({name:d.querySelector(".xn").value,link:d.querySelector(".xl").value})).filter(x=>x.name.trim());
  const r=await api("/api/sa/draft",{page:saPage,product:$("saprod").value,template:$("satpl").value,impact:$("saimp").value,noimpact:"",extra:extra});
  saTasks=r.tasks;saRender();}
$("saredo").onclick=()=>saDraft().catch(e=>{$("samsg").className="bad";$("samsg").textContent="Ошибка: "+e.message;});
const SAROLE={analysis:"Анализ",dev:"Разработка",extra:"Доработка",test:"Тестирование"};
function saRender(){
  $("satasks").innerHTML=saTasks.map((t,i)=>`<div class="sat" data-i="${i}" style="border:1px solid var(--rule);border-radius:6px;padding:10px 12px;margin-top:10px">
    <div class="bar-q"><label style="align-self:center"><input type="checkbox" class="on" checked></label>
     <span class="chip c-new" style="align-self:center">${SAROLE[t.role]||t.role}</span>
     <label class="f grow"><span>Название</span><input class="sm" value="${esc(t.summary)}"></label>
     <label class="f"><span>Исполнитель (логин)</span><input class="as" size="12" placeholder="U_XXXXX"></label></div>
    <details style="margin-top:8px"><summary>Описание${t.todo?` · <span class="od">не заполнено полей: ${t.todo}</span>`:""}</summary>
     <textarea class="ds" rows="12" style="margin-top:6px;font-size:12px">${esc(t.description)}</textarea></details></div>`).join("");
  $("satasks").querySelectorAll(".on").forEach(c=>c.onchange=saCount);
  $("saprev").classList.remove("hide");$("saconf").checked=false;saCount();}
function saCollect(){return [...$("satasks").querySelectorAll(".sat")].filter(d=>d.querySelector(".on").checked).map(d=>({summary:d.querySelector(".sm").value,assignee:d.querySelector(".as").value,description:d.querySelector(".ds").value}));}
function saCount(){const n=saCollect().length,todo=saCollect().reduce((a,t)=>a+(t.description.match(/⚠ заполнить/g)||[]).length,0);
  $("saprevsum").textContent=`к созданию: ${n} ${plural(n,"задача","задачи","задач")} в ${$("saprj").value}`+($("saepic").value?`, эпик ${$("saepic").value}`:", без эпика")+(todo?` · не заполнено полей шаблона: ${todo}`:"");
  $("samk").textContent=`Создать ${n} ${plural(n,"задачу","задачи","задач")}`;$("samk").disabled=!n||!$("saconf").checked;}
$("saconf").onchange=saCount;$("saepic").oninput=saCount;
$("samk").onclick=async()=>{
  const tasks=saCollect(),todo=tasks.reduce((a,t)=>a+(t.description.match(/⚠ заполнить/g)||[]).length,0);
  if(todo&&!confirm(`В описаниях осталось незаполненных полей шаблона: ${todo}. Всё равно создать?`))return;
  if(!confirm(`Создать в ${$("saprj").value} задач: ${tasks.length}?`))return;
  $("samk").disabled=true;$("sares").classList.remove("hide");$("sares").innerHTML=`<span class="mut">Создаю задачи...</span>`;
  try{const r=await api("/api/sa/create",{page:saPage,project:$("saprj").value,issuetype:$("satype").value,epic:$("saepic").value,tasks:tasks});
    $("sares").innerHTML=`<div class="row" style="margin-top:0"><h3 style="margin:0">Создано: ${r.created} из ${tasks.length}</h3><span class="mut">журнал: ${esc(r.journal)}</span></div>
     <div class="tw"><table><thead><tr><th>Задача</th><th>Название</th><th>Замечание</th></tr></thead><tbody>${r.results.map(x=>`<tr><td>${x.key?`<a class="key" href="${esc(r.base)}/browse/${esc(x.key)}" target="_blank" rel="noopener">${esc(x.key)}</a>`:`<span class="bad">ошибка</span>`}</td><td>${esc(x.summary)}</td><td class="${x.error?"bad":"mut"}">${esc(x.error||x.note||"")}</td></tr>`).join("")}</tbody></table></div>`;
    $("saconf").checked=false;
  }catch(e){$("sares").innerHTML=`<span class="bad">Ошибка: ${esc(e.message)}</span>`;}
  saCount();};

/* ---- структура эпика ---- */
let treeData=null,collapsed=new Set(),treeJob=null,excl=new Set(),exclRoot="";
const fmtD=s=>s?s.split("-").reverse().join("."):"—";
$("tgo").onclick=async()=>{
  if(!connected){$("tmsg").textContent="Сначала подключитесь к Jira.";return;}
  const rk=$("tkey").value.trim().toUpperCase();
  if(rk!==exclRoot){excl=new Set();exclRoot=rk;}
  $("tmsg").className="mut";$("tmsg").textContent="Запуск...";$("tgo").disabled=true;$("tcancel").classList.remove("hide");
  $("tsum").classList.add("hide");$("tres").classList.add("hide");$("tcsv").classList.add("hide");$("tmkrow").classList.add("hide");$("tfrm").classList.add("hide");collapsed=new Set();
  try{const s=await api("/api/tree/start",{key:rk,depth:+$("tdepth").value,links:$("tlinks").checked,max:+$("tmax").value||300,exclude:[...excl]});treeJob=s.job;
    for(;;){const j=await api("/api/job/"+s.job);
      if(j.status==="running"){$("tmsg").textContent=(j.msg||"Работаю")+" · найдено задач: "+j.done;await new Promise(r=>setTimeout(r,1000));continue;}
      if(j.status==="done"&&j.tree){treeData=j.tree;$("tmsg").textContent="";$("tsname").value=defaultStructName();$("tfrmtxt").value=j.tree.work_formula||"";$("tfrm").classList.toggle("hide",!j.tree.work_formula);renderTree();}
      else{$("tmsg").className="bad";$("tmsg").textContent=j.status==="cancelled"?"Остановлено.":"Ошибка: "+(j.error||j.status);}
      break;}
  }catch(e){$("tmsg").className="bad";$("tmsg").textContent="Ошибка: "+e.message;}
  $("tgo").disabled=false;$("tcancel").classList.add("hide");};
let createdSig="";
$("tfrmcp").onclick=async()=>{const t=$("tfrmtxt");try{await navigator.clipboard.writeText(t.value);$("tfrmmsg").textContent="Скопировано";}catch(e){t.select();$("tfrmmsg").textContent="Выделено, нажмите Ctrl+C";}setTimeout(()=>$("tfrmmsg").textContent="",2500);};
$("tmk").onclick=async()=>{
  const gv=groupedView(getView(),$("tgroup").value),v=gv.filter(n=>!n.folder&&n.cat!=="none"),nf=gv.filter(n=>n.folder).length,name=$("tsname").value.trim();
  const layout=gv.map(n=>n.folder?{f:n.name,l:n.level}:{k:n.key,l:n.level});
  if(!name){$("tmsg").className="bad";$("tmsg").textContent="Укажите название структуры.";return;}
  const sig=treeJob+"|"+[...excl].sort().join(",")+"|"+$("tgroup").value;
  if(createdSig===sig&&!confirm("Структура для этого набора задач уже создавалась. Создать ещё одну?"))return;
  if(!confirm(`Создать в Jira структуру «${name}»?\nЗадач: ${v.length}`+(nf?`, папок: ${nf} (${$("tgroup").selectedOptions[0].text})`:"")+(excl.size?` (убрано вами: ${excl.size})`:"")+`\nПрава доступа программа не меняет.`))return;
  $("tmk").disabled=true;$("tmsg").className="mut";$("tmsg").textContent="Создаю структуру в Jira...";
  try{const r=await api("/api/tree/create",{job:treeJob,name:name,exclude:[...excl],layout:layout});
    createdSig=sig;
    const ok=r.verified;$("tmsg").className=ok?"ok":"bad";
    $("tmsg").innerHTML=(ok?`Структура создана и проверена: ${r.found} из ${r.expected} строк${r.folders?`, из них папок: ${r.folders}`:""}. `:`Структура создана (№ ${esc(r.id)}), но есть замечания. `)+
      `<a href="${esc(r.url)}" target="_blank" rel="noopener">Открыть в Jira</a>`+
      (r.skipped.length?`<div class="mut">Не добавлены (нет id/доступа): ${esc(r.skipped.join(", "))}</div>`:"")+
      (r.warnings||[]).map(w=>`<div class="bad">${esc(w)}</div>`).join("");
  }catch(e){$("tmsg").className="bad";$("tmsg").textContent="Ошибка: "+e.message;}
  $("tmk").disabled=false;};
function defaultStructName(){
  const n=treeData.nodes[0],d=new Date().toLocaleDateString("ru-RU");
  return ("Структура "+treeData.root+" — "+(n.summary||"")).slice(0,150)+" ("+d+")";}

$("tcancel").onclick=()=>{if(treeJob)api("/api/job/"+treeJob+"/cancel",{});};
function getView(){
  const o=treeData.nodes,drop=[],map={},v=[];
  o.forEach((n,i)=>{drop[i]=n.pid>=0&&(excl.has(n.key)||drop[n.pid]);});
  o.forEach((n,i)=>{if(drop[i])return;map[i]=v.length;const c=Object.assign({},n);c.pid=n.pid<0?-1:map[n.pid];c.has_children=false;v.push(c);});
  v.forEach(n=>{if(n.pid>=0)v[n.pid].has_children=true;});
  return v;}
try{const g=localStorage.getItem("jt_tree_group");if(g!=null)$("tgroup").value=g;}catch(e){}
$("tgroup").onchange=()=>{try{localStorage.setItem("jt_tree_group",$("tgroup").value);}catch(e){}if(treeData){collapsed=new Set();renderTree();}};
function groupOf(n,mode){
  if(mode==="project"){const k=n.project||n.key.split("-")[0];return{k:k,name:k+(n.project_name?" · "+n.project_name:"")};}
  if(mode==="type")return{k:n.type||"",name:n.type||"Без типа"};
  if(mode==="assignee")return{k:n.assignee||"",name:n.assignee||"Без исполнителя"};
  if(mode==="status")return{k:n.status||"",name:n.status||"Без статуса"};
  return null;}
/* Раскладка по папкам: корень наверху, под ним папки; внутри папки задача висит под ближайшим предком из той же папки. */
function groupedView(v,mode){
  if(!mode||v.length<2)return v.map(n=>Object.assign({folder:false,group:""},n));
  const root=v[0],gk=v.map(n=>groupOf(n,mode)),groups=new Map(),kids=new Map();
  for(let i=1;i<v.length;i++){
    const g=gk[i].k;let p=v[i].pid;
    while(p>0&&gk[p].k!==g)p=v[p].pid;
    const par=p>0?p:-1,kk=g+"|"+par;
    if(!groups.has(g))groups.set(g,{name:gk[i].name,cnt:0});
    else if(gk[i].name.length>groups.get(g).name.length)groups.get(g).name=gk[i].name;
    groups.get(g).cnt++;if(!kids.has(kk))kids.set(kk,[]);kids.get(kk).push(i);}
  const rk=gk[0].k,order=[...groups.keys()].sort((a,b)=>(b===rk)-(a===rk)||groups.get(a).name.localeCompare(groups.get(b).name,"ru"));
  const out=[Object.assign({},root,{folder:false,group:"",level:0,pid:-1,has_children:groups.size>0})];
  for(const g of order){
    const G=groups.get(g),fi=out.length;
    out.push({folder:true,key:"folder:"+g,name:G.name,cnt:G.cnt,level:1,pid:0,has_children:true,cat:"folder",group:G.name});
    const walk=(par,lvl,pidx)=>{for(const i of (kids.get(g+"|"+par)||[])){
      const idx=out.length;out.push(Object.assign({},v[i],{folder:false,group:G.name,level:lvl,pid:pidx,has_children:false}));
      out[pidx].has_children=true;walk(i,lvl+1,idx);}};
    walk(-1,2,fi);}
  return out;}
function calcSummary(v){
  const real=v.filter(n=>n.cat!=="none");
  const s={total:v.length,new:0,indeterminate:0,done:0,no_access:v.length-real.length,overdue:0};
  real.forEach(n=>{if(s[n.cat]!==undefined)s[n.cat]++;if(n.overdue)s.overdue++;});
  const st=real.map(n=>n.start).filter(Boolean).sort(),en=real.map(n=>n.end).filter(Boolean).sort();
  s.first_start=st[0]||"";s.last_end=(en.length&&s.done===real.length)?en[en.length-1]:"";
  const dd=real.filter(n=>n.cat==="done"&&n.days!=null).map(n=>n.days);
  s.avg_done_days=dd.length?Math.round(dd.reduce((a,b)=>a+b,0)/dd.length*10)/10:null;return s;}
function renderTree(){
  const t=treeData,v=getView(),s=calcSummary(v),tot=s.total-s.no_access||1;
  const w=c=>(100*s[c]/tot).toFixed(1)+"%";
  const ex=[...excl];
  $("tsum").innerHTML=`<div class="row"><b>${esc(t.root)}</b><span>задач в структуре: <b>${s.total}</b></span>
   <span class="pbar"><i style="width:${w("done")};background:var(--ok)"></i><i style="width:${w("indeterminate")};background:var(--ac)"></i><i style="width:${w("new")};background:var(--faint)"></i></span>
   <span>готово ${s.done} · в работе ${s.indeterminate} · к выполнению ${s.new}</span></div>
   <div class="row mut"><span>первое начало: <b>${fmtD(s.first_start)}</b></span><span>завершение структуры: <b>${fmtD(s.last_end)}</b></span>
   <span>среднее время готовых задач: <b>${s.avg_done_days==null?"—":s.avg_done_days+" дн."}</b></span>
   <span class="${s.overdue?"od":""}">просрочено: <b>${s.overdue}</b></span>${s.no_access?`<span class="bad">нет доступа: ${s.no_access}</span>`:""}</div>
   ${ex.length?`<div class="row"><span class="mut">Убрано из структуры (${ex.length}):</span>${ex.map(k=>`<span class="chip c-new" style="display:inline-flex;gap:4px">${esc(k)} <a href="#" data-rs="${esc(k)}" title="Вернуть">↺</a></span>`).join("")}
     <button id="rsall">Вернуть все</button></div>`:""}
   ${t.truncated?`<div class="bad">Достигнут лимит задач: структура неполная. Увеличьте «максимум задач» или уменьшите глубину.</div>`:""}`;
  $("tsum").classList.remove("hide");
  const gv=groupedView(v,$("tgroup").value);
  $("tres").innerHTML=`<table><thead><tr><th>Задача</th><th>Связь</th><th>Статус</th><th>Исполнитель</th><th>Начало</th><th>Завершение</th><th>В работе, дн.</th><th>Срок</th><th></th></tr></thead><tbody id="tbody">`+
   gv.map((n,i)=>n.folder?`<tr data-i="${i}"><td colspan="9" style="padding-left:${8+n.level*20}px;white-space:nowrap;background:var(--sunk)"><button class="tg" data-tg="${esc(n.key)}">${collapsed.has(n.key)?"▸":"▾"}</button> 📁 <b>${esc(n.name)}</b> <span class="mut">· задач: ${n.cnt}</span></td></tr>`:`<tr data-i="${i}"><td style="padding-left:${8+n.level*20}px;white-space:nowrap">${n.has_children?`<button class="tg" data-tg="${esc(n.key)}">${collapsed.has(n.key)?"▸":"▾"}</button>`:`<span style="display:inline-block;width:22px"></span>`}
     <a href="${esc(t.base)}/browse/${esc(n.key)}" target="_blank" rel="noopener">${esc(n.key)}</a> <span class="mut">${esc(n.type)}</span><div style="margin-left:22px;white-space:normal">${esc(n.summary)}</div></td>
     <td class="mut">${esc(n.rel)}</td><td><span class="chip c-${n.cat}">${esc(n.status)}</span></td><td>${esc(n.assignee)}</td>
     <td>${n.start?(n.start_approx?"≈ ":"")+fmtD(n.start):"—"}</td><td>${fmtD(n.end)}</td><td>${n.days==null?"—":n.days}</td><td class="${n.overdue?"od":""}">${fmtD(n.due)}</td>
     <td>${n.pid>=0?`<button class="tg" data-rm="${esc(n.key)}" title="Убрать задачу${n.has_children?" и всё, что найдено через неё":""}" style="font-size:16px">✕</button>`:""}</td></tr>`).join("")+`</tbody></table>`;
  $("tres").classList.remove("hide");$("tcsv").classList.remove("hide");$("tmkrow").classList.remove("hide");
  document.querySelectorAll("[data-tg]").forEach(b=>b.onclick=()=>{const k=b.dataset.tg;if(collapsed.has(k))collapsed.delete(k);else collapsed.add(k);b.textContent=collapsed.has(k)?"▸":"▾";applyCollapse(gv);});
  document.querySelectorAll("[data-rm]").forEach(b=>b.onclick=()=>{excl.add(b.dataset.rm);renderTree();});
  document.querySelectorAll("[data-rs]").forEach(a=>a.onclick=e=>{e.preventDefault();const k=a.dataset.rs;excl.delete(k);
    if(!treeData.nodes.some(n=>n.key===k)){$("tmsg").className="mut";$("tmsg").textContent="Задача "+k+" вернётся после повторной сборки структуры.";}
    renderTree();});
  if($("rsall"))$("rsall").onclick=()=>{const need=[...excl].some(k=>!treeData.nodes.some(n=>n.key===k));excl=new Set();
    if(need){$("tmsg").className="mut";$("tmsg").textContent="Часть задач вернётся после повторной сборки структуры.";}renderTree();};
  applyCollapse(gv);}
function applyCollapse(v){
  const hidden=[];
  document.querySelectorAll("#tbody tr").forEach(tr=>{const i=+tr.dataset.i,p=v[i].pid;
    hidden[i]=p>=0&&(hidden[p]||collapsed.has(v[p].key));tr.style.display=hidden[i]?"none":"";});}
$("tcsv").onclick=()=>{
  const v=groupedView(getView(),$("tgroup").value).filter(n=>!n.folder);
  const q=x=>'"'+String(x==null?"":x).replace(/"/g,'""')+'"';
  const head=["Папка","Уровень","Ключ","Заголовок","Тип","Связь","Статус","Категория","Исполнитель","Создана","Начало","Завершение","В работе (дн)","Срок","Ссылка"];
  const rows=v.map(n=>[n.group,n.level,n.key,n.summary,n.type,n.rel,n.status,{new:"К выполнению",indeterminate:"В работе",done:"Готово",none:"нет доступа"}[n.cat],n.assignee,n.created,(n.start_approx?"≈ ":"")+n.start,n.end,n.days==null?"":n.days,n.due,treeData.base+"/browse/"+n.key]);
  const csv="﻿"+[head].concat(rows).map(r=>r.map(q).join(";")).join("\r\n");
  const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"}));a.download="structure_"+treeData.root+".csv";a.click();};

/* ---- связанные задачи ---- */
let lkData=null,lkJob=null,lkExcl=new Set(),lkEdit={},lkOpen=new Set();
try{const st=JSON.parse(localStorage.getItem("jt_links")||"{}");if(st.fields)$("lfields").value=st.fields;if(st.jql!=null)$("ljql").value=st.jql;}catch(e){}
const catName={new:"К выполнению",indeterminate:"В работе",done:"Готово"};
const fmtDT=s=>s?s.slice(8,10)+"."+s.slice(5,7)+"."+s.slice(0,4)+(s.length>10?" "+s.slice(11):""):"—";
$("lgo").onclick=async()=>{
  if(!connected){$("lmsg").className="mut";$("lmsg").textContent="Сначала подключитесь к Jira.";return;}
  const k=$("lkey").value.trim().toUpperCase();
  try{localStorage.setItem("jt_links",JSON.stringify({fields:$("lfields").value,jql:$("ljql").value}));}catch(e){}
  $("lmsg").className="mut";$("lmsg").textContent="Запуск...";$("lgo").disabled=true;$("lcancel").classList.remove("hide");
  $("lsum").classList.add("hide");$("lres").classList.add("hide");$("lcsv").classList.add("hide");
  try{const s=await api("/api/links/start",{key:k,jql:$("ljql").value,fields:$("lfields").value,parse:$("lparse").checked,max:+$("lmax").value||1000});lkJob=s.job;
    for(;;){const j=await api("/api/job/"+s.job);
      if(j.status==="running"){$("lmsg").textContent=j.msg||"Работаю";await new Promise(r=>setTimeout(r,800));continue;}
      if(j.status==="done"&&j.links){if(!lkData||lkData.root!==j.links.root){lkExcl=new Set();lkEdit={};lkOpen=new Set();}
        lkData=j.links;$("lmsg").textContent="";renderLinks();}
      else{$("lmsg").className="bad";$("lmsg").textContent=j.status==="cancelled"?"Остановлено.":"Ошибка: "+(j.error||j.status);}
      break;}
  }catch(e){$("lmsg").className="bad";$("lmsg").textContent="Ошибка: "+e.message;}
  $("lgo").disabled=false;$("lcancel").classList.add("hide");};
$("lcancel").onclick=()=>{if(lkJob)api("/api/job/"+lkJob+"/cancel",{});};
const lkCus=r=>lkEdit[r.key]!=null?lkEdit[r.key]:r.link_cus;
const lkCheck=r=>lkEdit[r.key]==null&&r.check;
function lkView(){const q=($("lq")?$("lq").value:"").trim().toUpperCase(),only=$("lonly")&&$("lonly").checked;
  return lkData.rows.filter(r=>!lkExcl.has(r.key)&&(!only||lkCheck(r))&&(!q||(r.key+" "+r.fields.join(" ")+" "+lkCus(r)+" "+r.summary).toUpperCase().includes(q)));}
function renderLinks(){
  const d=lkData,all=d.rows.filter(r=>!lkExcl.has(r.key));
  const c={new:0,indeterminate:0,done:0};all.forEach(r=>{if(c[r.cat]!=null)c[r.cat]++;});
  const chk=all.filter(lkCheck).length,nolink=d.parse_desc?all.filter(r=>!lkCus(r)).length:0;
  const byDay={};all.forEach(r=>{const k=r.created.slice(0,10);byDay[k]=(byDay[k]||0)+1;});
  const days=Object.keys(byDay).sort(),mx=Math.max(1,...Object.values(byDay));
  const ex=[...lkExcl];
  $("lsum").innerHTML=`<div class="row"><b><a href="${esc(d.base)}/browse/${esc(d.root)}" target="_blank" rel="noopener">${esc(d.root)}</a></b><span class="mut">${esc(d.root_summary)}</span></div>
   <div class="row"><span>связанных задач: <b>${all.length}</b></span><span>готово ${c.done} · в работе ${c.indeterminate} · к выполнению ${c.new}</span>
   ${d.parse_desc?`<span class="${chk?"warn":""}">проверить КУС по ссылке: <b>${chk}</b></span><span class="mut">КУС по ссылке не найден: ${nolink}</span>`:""}</div>
   ${days.length>1?`<div class="mut" style="margin-top:10px">Создано по дням</div><div class="days" style="margin-top:18px">${days.map(k=>`<div style="height:${Math.max(4,70*byDay[k]/mx)}px" title="${fmtDT(k)}: ${byDay[k]}"><span>${byDay[k]}</span></div>`).join("")}</div>
     <div class="row mut" style="justify-content:space-between;font-size:11px;margin:2px 0 0"><span>${fmtDT(days[0])}</span><span>${fmtDT(days[days.length-1])}</span></div>`:""}
   ${d.missing.length?`<div class="bad">Не найдены поля: ${esc(d.missing.join(", "))}. Проверьте названия (как в Jira) или укажите id вида customfield_12345.</div>`:""}
   ${d.truncated?`<div class="bad">Показаны первые ${d.rows.length} из ${d.total}. Увеличьте «максимум».</div>`:""}
   ${ex.length?`<div class="row"><span class="mut">Убрано (${ex.length}):</span>${ex.map(k=>`<span class="chip c-new" style="display:inline-flex;gap:4px">${esc(k)} <a href="#" data-lrs="${esc(k)}" title="Вернуть">↺</a></span>`).join("")}<button id="lrsall">Вернуть все</button></div>`:""}
   <div class="row"><input id="lq" size="30" placeholder="Поиск по ключу, КУС, заголовку"><label class="mut"><input type="checkbox" id="lonly"> только «проверить»</label></div>`;
  $("lsum").classList.remove("hide");$("lcsv").classList.remove("hide");
  $("lq").oninput=renderLinkRows;$("lonly").onchange=renderLinkRows;
  document.querySelectorAll("[data-lrs]").forEach(a=>a.onclick=e=>{e.preventDefault();lkExcl.delete(a.dataset.lrs);renderLinks();});
  if($("lrsall"))$("lrsall").onclick=()=>{lkExcl=new Set();renderLinks();};
  renderLinkRows();}
function renderLinkRows(){
  const d=lkData,v=lkView(),nf=d.fields.length;
  $("lres").innerHTML=`<div class="mut" style="margin-bottom:6px">Показано: ${v.length}</div><table><thead><tr><th>Создана</th><th>Задача</th><th>Связь</th><th>Статус</th>${d.fields.map(f=>`<th>${esc(f)}</th>`).join("")}${d.parse_desc?"<th>КУС по ссылке (из описания)</th>":""}<th></th><th></th></tr></thead><tbody>`+
   v.map(r=>`<tr><td style="white-space:nowrap">${fmtDT(r.created)}</td>
     <td style="white-space:nowrap"><a href="${esc(d.base)}/browse/${esc(r.key)}" target="_blank" rel="noopener">${esc(r.key)}</a><div class="mut" style="white-space:normal;font-size:12px">${esc(r.summary)}</div></td>
     <td class="mut">${esc(r.rel)}</td><td><span class="chip c-${esc(r.cat)}">${esc(r.status)}</span></td>
     ${r.fields.map(x=>`<td><b>${esc(x)}</b></td>`).join("")}
     ${d.parse_desc?`<td style="white-space:nowrap"><input data-lce="${esc(r.key)}" value="${esc(lkCus(r))}" size="9" placeholder="—" title="${r.cands.length>1?"Найдено в описании: "+esc(r.cands.join(", ")):"Можно исправить вручную"}">${lkCheck(r)?' <span class="warn" title="В описании не сказано явно, что это КУС по ссылке">проверить</span>':""}${lkEdit[r.key]!=null?' <span class="mut" title="Исправлено вручную">✎</span>':""}</td>`:""}
     <td><button class="tg" data-lop="${esc(r.key)}" title="Описание">${lkOpen.has(r.key)?"▾":"▸"}</button></td>
     <td><button class="tg" data-lrm="${esc(r.key)}" title="Убрать из выгрузки" style="font-size:16px">✕</button></td></tr>
     ${lkOpen.has(r.key)?`<tr><td></td><td colspan="${5+nf+(d.parse_desc?1:0)}"><div class="lkd">${esc(r.description||"(описание пустое)")}</div></td></tr>`:""}`).join("")+`</tbody></table>`;
  $("lres").classList.remove("hide");
  document.querySelectorAll("[data-lce]").forEach(i=>i.onchange=()=>{const k=i.dataset.lce,val=i.value.trim().toUpperCase(),r=lkData.rows.find(x=>x.key===k);
    if(r&&val===r.link_cus&&!r.check)delete lkEdit[k];else lkEdit[k]=val;renderLinks();});
  document.querySelectorAll("[data-lop]").forEach(b=>b.onclick=()=>{const k=b.dataset.lop;if(lkOpen.has(k))lkOpen.delete(k);else lkOpen.add(k);renderLinkRows();});
  document.querySelectorAll("[data-lrm]").forEach(b=>b.onclick=()=>{lkExcl.add(b.dataset.lrm);renderLinks();});}
$("lcsv").onclick=()=>{
  const d=lkData,v=d.rows.filter(r=>!lkExcl.has(r.key));
  const q=x=>'"'+String(x==null?"":x).replace(/"/g,'""')+'"';
  const head=["Дата создания","Задача","Ссылка","Заголовок","Тип связи","Статус","Категория"].concat(d.fields).concat(d.parse_desc?["КУС по ссылке (из описания)","Проверить"]:[]).concat(["Исполнитель","Описание"]);
  const rows=v.map(r=>[fmtDT(r.created),r.key,d.base+"/browse/"+r.key,r.summary,r.rel,r.status,catName[r.cat]||r.cat].concat(r.fields).concat(d.parse_desc?[lkCus(r),lkCheck(r)?"да":""]:[]).concat([r.assignee,r.description]));
  const csv="﻿"+[head].concat(rows).map(r=>r.map(q).join(";")).join("\r\n");
  const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"}));a.download="linked_"+d.root+".csv";a.click();};

api("/api/state").then(s=>{setConn(s);loadDash();}).catch(()=>{setConn({connected:false});loadDash().catch(()=>{});});
</script></body></html>"""

def main():
    ap = argparse.ArgumentParser(description="Jira Toolkit: локальный помощник над Jira")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    a = ap.parse_args()
    srv = None
    for p in range(a.port, a.port + 20):
        try:
            PORT[0] = p; srv = ThreadingHTTPServer(("127.0.0.1", p), Handler); break
        except OSError: continue
    if srv is None: sys.exit("Не удалось занять порт")
    url = f"http://127.0.0.1:{PORT[0]}"
    print(f"Jira Toolkit запущен: {url}\nПриложение доступно только на этом компьютере. Остановить: Ctrl+C")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nОстановлено")

if __name__ == "__main__":
    main()
