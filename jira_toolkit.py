#!/usr/bin/env python3
"""Jira Toolkit - локальный помощник над Jira (прототип).

Запуск:   python jira_toolkit.py
Откроется страница http://127.0.0.1:8765 в браузере.

Модули:
  * Массовые изменения: вставьте список задач -> предпросмотр "было -> станет" -> запись -> журнал и откат.
  * Дашборд: настраиваемые карточки-метрики (JQL) с порогами, готовые наборы под роли.
  * Связанные задачи: выгрузка всех связанных с задачей запросов (дата, статус, поля вроде «Наименование клиента», КУС по ссылке из описания) с выгрузкой в CSV.
  * Структура эпика: дерево задач эпика, подзадач и связей со статусом, датами начала/завершения и временем в работе; выгрузка в CSV;
    кнопка «Создать структуру в Jira» (плагин Structure, REST API).

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
STATE = {"base": "", "token": "", "insecure": True, "user": ""}
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
TREE_FIELDS = ["summary", "status", "issuetype", "assignee", "created", "resolutiondate", "duedate", "issuelinks", "subtasks"]
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
    return {"key": it["key"].upper(), "id": str(it.get("id", "")), "summary": f.get("summary", ""), "type": (f.get("issuetype") or {}).get("name", ""),
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
                    nodes[ck] = {"key": ck, "id": "", "summary": "(нет доступа или не найдена)", "type": "", "status": "нет доступа", "cat": "none", "assignee": "",
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

def parse_formula(formula):
    out = []
    for part in (formula or "").split(","):
        p = part.strip().split(":")
        if len(p) >= 3: out.append((p[2], int(p[1])))
    return out

def create_structure(job_id, name, exclude):
    job = JOBS.get(job_id)
    tree = job.get("tree") if job else None
    if not tree: raise JiraError(400, "Сначала соберите структуру (результат сборки не найден)")
    name = str(name or "").strip()[:200]
    if not name: raise JiraError(400, "Укажите название структуры")
    nodes = tree["nodes"]; drop = []
    for i, n in enumerate(nodes):
        drop.append(n["pid"] >= 0 and (n["key"] in exclude or drop[n["pid"]]))
    rows, skipped = [], []
    for i, n in enumerate(nodes):
        if drop[i]: continue
        iid = str(n.get("id", ""))
        if iid.isdigit(): rows.append((n["key"], iid, n["level"]))
        else: skipped.append(n["key"])
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
    result = {"id": sid, "url": STATE["base"].rstrip("/") + f"/secure/StructureBoard.jspa?s={sid}", "expected": len(rows),
              "skipped": skipped, "warnings": [], "verified": False, "found": 0}
    try:
        with open(os.path.join(DATA, f"structure_{sid}.json"), "w", encoding="utf-8") as f:
            json.dump({"id": sid, "name": name, "root": tree["root"], "created": time.strftime("%Y-%m-%d %H:%M:%S"), "issues": [r[0] for r in rows]}, f, ensure_ascii=False, indent=1)
    except OSError: pass
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
        if path == "/api/state": return self._json({"connected": bool(STATE["token"]), "base": STATE["base"], "user": STATE["user"]})
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
            if path == "/api/disconnect":
                STATE.update(token="", user=""); return self._json({"connected": False})
            if not STATE["token"]: return self._json({"error": "сначала подключитесь к Jira"}, 401)
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
                return self._json(create_structure(str(body.get("job", "")), body.get("name", ""), ex))
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

PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Jira Toolkit v0.23</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#1d2733;--mut:#6b7785;--br:#d9dee4;--ac:#1f5fbf;--ok:#1e8e4e;--wr:#c27a00;--er:#c62828}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:var(--tx)}
header{background:var(--card);border-bottom:1px solid var(--br);padding:12px 20px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:16px;margin:0 12px 0 0}input,select,textarea,button{font:inherit}
input,select,textarea{border:1px solid var(--br);border-radius:6px;padding:6px 8px;background:#fff;color:var(--tx)}
button{border:1px solid var(--br);background:#fff;border-radius:6px;padding:6px 12px;cursor:pointer}
button.p{background:var(--ac);color:#fff;border-color:var(--ac)}button:disabled{opacity:.5;cursor:default}
button.d{color:var(--er);border-color:var(--er)}
main{max-width:1200px;margin:0 auto;padding:16px 20px}
nav{display:flex;gap:4px;margin-bottom:12px}nav button{border-radius:6px 6px 0 0}nav button.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.card{background:var(--card);border:1px solid var(--br);border-radius:8px;padding:14px;margin-bottom:14px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:6px 0}.mut{color:var(--mut)}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--br);padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f0f2f5;font-weight:600}.chip{padding:1px 8px;border-radius:10px;font-size:12px;color:#fff;white-space:nowrap}
.apply{background:var(--ok)}.skip{background:#8a96a3}.error{background:var(--er)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.tile{border:1px solid var(--br);border-left:6px solid var(--ok);border-radius:8px;padding:12px;background:#fff;text-decoration:none;color:inherit;display:block}
.tile.warn{border-left-color:var(--wr)}.tile.crit{border-left-color:var(--er)}.tile.err{border-left-color:#8a96a3}
.tile .n{font-size:30px;font-weight:700}.hide{display:none}textarea{width:100%;min-height:130px;font-family:ui-monospace,Consolas,monospace}
.bar{height:10px;background:#e4e8ec;border-radius:5px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ac)}
#connmsg{font-size:13px}.ok{color:var(--ok)}.bad{color:var(--er)}
.c-new{background:#8a96a3}.c-indeterminate{background:#1f5fbf}.c-done{background:#1e8e4e}.c-none{background:#c62828}
.tg{border:0;background:none;padding:0 4px;cursor:pointer;color:var(--mut)}.od{color:var(--er);font-weight:600}
.lkd{white-space:pre-wrap;font-size:12px;background:#f6f7f9;border-radius:6px;padding:8px;margin:4px 0}.warn{color:var(--wr);font-weight:600}
.days{display:flex;align-items:flex-end;gap:3px;height:70px}.days div{flex:1;min-width:14px;background:var(--ac);border-radius:3px 3px 0 0;position:relative}.days span{position:absolute;top:-16px;left:0;right:0;text-align:center;font-size:11px;color:var(--mut)}
.pbar{display:flex;height:12px;border-radius:6px;overflow:hidden;background:#e4e8ec;min-width:220px}.pbar i{display:block;height:100%}
</style></head><body>
<header>
 <h1>Jira Toolkit <span class="mut" style="font-weight:400;font-size:12px">v0.23</span></h1>
 <input id="base" size="34" placeholder="Адрес Jira, https://jira.company.ru" value="https://jira.moscow.alfaintra.net">
 <input id="token" type="password" size="26" placeholder="Personal Access Token" autocomplete="off">
 <label class="mut"><input id="insecure" type="checkbox" checked> не проверять SSL-сертификат</label>
 <button class="p" id="bconn">Подключиться</button><button id="bdisc" class="hide">Отключиться</button>
 <span id="connmsg" class="mut">Токен хранится только в памяти этого приложения и уходит только в вашу Jira.</span>
</header>
<main>
<nav><button data-t="bulk" class="on">Массовые изменения</button><button data-t="dash">Дашборд</button><button data-t="tree">Структура эпика</button><button data-t="lk">Связанные задачи</button><button data-t="jr">Журнал и откат</button></nav>

<section id="t-bulk">
 <div class="card">
  <div class="row"><b>1. Что сделать</b>
   <select id="op"></select>
   <input id="gval" size="26" placeholder="Значение для всех строк (если нет в списке)">
   <label class="mut">пауза между задачами, с <input id="pause" type="number" step="0.1" min="0.2" value="0.5" style="width:70px"></label></div>
  <div class="mut">Вставьте список (можно прямо из Excel): <code>ключ</code> или <code>ключ  значение</code>, по одной задаче в строке. Разделитель: Tab, «;» или «,».</div>
  <textarea id="rows" placeholder="INCIDENT-239	КН&#10;INCIDENT-240	КН"></textarea>
  <div class="row"><input type="file" id="file" accept=".csv,.txt,.tsv"> <button class="p" id="bprev">2. Предпросмотр</button><span id="prevmsg" class="mut"></span></div>
 </div>
 <div class="card hide" id="prevcard">
  <div class="row"><b>Предпросмотр</b><span id="prevsum" class="mut"></span></div>
  <div style="max-height:380px;overflow:auto"><table><thead><tr><th>Задача</th><th>Заголовок</th><th>Сейчас</th><th>Операция</th><th>Результат</th></tr></thead><tbody id="prevbody"></tbody></table></div>
  <div class="row"><label><input type="checkbox" id="conf"> Понимаю, что изменения будут записаны в Jira (журнал сохранится, откат доступен)</label>
   <button class="p" id="bapply" disabled>3. Записать в Jira</button></div>
 </div>
 <div class="card hide" id="jobcard">
  <div class="row"><b id="jobtitle">Выполнение</b><span id="jobsum" class="mut"></span><button id="bcancel">Остановить</button></div>
  <div class="bar"><i id="jobbar" style="width:0%"></i></div>
  <div id="jobres" style="max-height:340px;overflow:auto;margin-top:8px"></div>
 </div>
</section>

<section id="t-dash" class="hide">
 <div class="card">
  <b>Создать рабочие доски в Jira</b>
  <div class="mut">Программа создаёт в Jira сохранённые фильтры по проектам и два рабочих стола с готовыми гаджетами: «Рабочий» (не закрытые задачи, графики) и «Статистика разбора потока» (создано/закрыто по периодам, время решения, разрезы).</div>
  <div class="row"><b>Проекты</b><input id="dgp" size="24" placeholder="напр. INCIDENT, OPERNAP">
   <label class="mut">типы задач <input id="dgt" size="18" placeholder="необязательно, через запятую"></label>
   <label class="mut">компоненты <input id="dgc" size="18" placeholder="необязательно"></label></div>
  <div class="row"><label class="mut">период «Рабочего», дней <input id="dgd" type="number" value="30" min="1" max="365" style="width:70px"></label>
   <label class="mut">период статистики, дней <input id="dgdf" type="number" value="90" min="7" max="730" style="width:70px"></label>
   <label class="mut">если дашборд уже есть <select id="dgex"><option value="skip">пропустить</option><option value="new">создать новый</option></select></label>
   <button class="p" id="dgprev">Показать, что будет создано</button></div>
  <div id="dgplan" class="hide">
   <div class="row"><b>Названия</b><input id="dgn1" size="40"><input id="dgn2" size="40"></div>
   <div class="row"><b>Пустые доски из Jira</b><input id="dgi1" size="40" placeholder="ссылка на доску 1 (Рабочий)"><input id="dgi2" size="40" placeholder="ссылка на доску 2 (Статистика)"></div>
   <div class="row"><button id="dgprobe">Проверить API гаджетов</button><span class="mut">диагностика: только чтение, ничего не меняет</span></div>
   <div id="dgprobeout"></div>
   <div class="mut">Если Jira не даёт создать дашборд автоматически: создайте в Jira два пустых рабочих стола («Рабочий стол → Создать панель») и вставьте их ссылки сюда. Программа создаст фильтры и добавит гаджеты в эти доски. Поля можно оставить пустыми.</div>
   <div style="max-height:560px;overflow:auto"><table><thead><tr><th></th><th>Виджет</th><th>Тип гаджета</th><th>Фильтр (JQL)</th></tr></thead><tbody id="dgbody"></tbody></table></div>
   <div id="dgsum" class="mut" style="margin:6px 0"></div>
   <div class="row"><button class="p" id="dgmk">Создать рабочие доски в Jira</button><span id="dgwarn" class="mut"></span></div>
  </div>
  <div id="dgres"></div>
 </div>
 <div class="card">
  <div class="row"><b>Роль</b><select id="role"></select><b>Проект</b><input id="proj" size="12" placeholder="напр. INCIDENT">
   <button class="p" id="drun">Обновить метрики</button><button id="dsave">Сохранить настройки</button><span id="dmsg" class="mut"></span></div>
  <div class="mut">Карточка = запрос JQL + пороги. Количество ≥ «внимание» — жёлтая, ≥ «критично» — красная (0 — порог выключен). <code>{PROJECT}</code> подставляется из поля «Проект».</div>
  <table><thead><tr><th>Название</th><th>JQL</th><th>Внимание</th><th>Критично</th><th></th></tr></thead><tbody id="cardsbody"></tbody></table>
  <div class="row"><button id="dadd">+ Карточка</button></div>
 </div>
 <div class="tiles" id="tiles"></div>
</section>

<section id="t-tree" class="hide">
 <div class="card">
  <div class="row"><b>Основной эпик</b><input id="tkey" size="16" placeholder="напр. ALFAMLM4-100">
   <label class="mut">глубина <select id="tdepth"><option>1</option><option>2</option><option selected>3</option><option>4</option><option>5</option></select></label>
   <label class="mut"><input type="checkbox" id="tlinks" checked> включать связи между задачами (blocks, relates и др.)</label>
   <label class="mut">максимум задач <input id="tmax" type="number" value="300" min="10" max="1000" style="width:80px"></label>
   <button class="p" id="tgo">Собрать структуру</button><button id="tcsv" class="hide">Скачать CSV</button><button id="tcancel" class="hide">Остановить</button></div>
  <div class="row hide" id="tmkrow"><b>Структура в Jira</b><input id="tsname" size="70" placeholder="Название структуры">
   <button class="p" id="tmk">Создать структуру в Jira</button></div>
  <div class="mut">Дерево строится от эпика: задачи эпика, подзадачи и связанные задачи на указанную глубину. Каждая задача показана один раз — под тем, через кого найдена.
   «Начало» — первый переход в статус категории «В работе» по истории задачи (≈ — оценка по дате создания, если истории нет). «В работе» — календарные дни от начала до завершения или до сегодня.</div>
  <details id="tfrm" class="hide"><summary><b>Столбец «Дней в работе» в Structure</b></summary>
   <div class="mut">В Structure нет готового столбца с днями в работе. Его добавляют один раз: откройте созданную структуру → «+» справа от заголовков столбцов → Formula → вставьте формулу ниже, назовите столбец «Дней в работе», формат Number. Потом сохраните это представление (View) и используйте для других структур. Считается от первого перехода в статус категории «В работе» (список статусов взят из вашей Jira) до решения или до сегодня, в календарных днях.</div>
   <textarea id="tfrmtxt" rows="5" style="width:100%;font-family:monospace;font-size:12px" readonly></textarea>
   <div class="row"><button id="tfrmcp">Скопировать формулу</button><span id="tfrmmsg" class="mut"></span></div>
   <div class="mut">Формула не проверялась на вашем Structure. Имена <code>resolved</code>, <code>status</code> и <code>created</code> подсвечиваются красным, пока не привязаны к полям: в блоке «Имена» под формулой нажмите на каждое и выберите атрибут («Дата резолюции», «Статус», «Создано»). Для задач, созданных сразу в рабочем статусе, началом считается дата создания. Если ругнётся на <code>DAYS_BETWEEN</code>, пришлите текст ошибки.</div>
  </details>
  <div id="tmsg" class="mut"></div>
 </div>
 <div class="card hide" id="tsum"></div>
 <div class="card hide" id="tres" style="overflow:auto"></div>
</section>
<section id="t-lk" class="hide">
 <div class="card">
  <div class="row"><b>Задача</b><input id="lkey" size="16" placeholder="напр. INCIDENT-1027">
   <label class="mut">доп. фильтр JQL <input id="ljql" size="30" placeholder="необязательно, напр. project = OPERNAP"></label>
   <label class="mut">максимум <input id="lmax" type="number" value="1000" min="10" max="5000" style="width:80px"></label></div>
  <div class="row"><label class="mut">поля Jira в колонки <input id="lfields" size="44" value="Наименование клиента, ID Клиента" title="Названия полей как в Jira или id (customfield_12345), через запятую"></label>
   <label class="mut"><input type="checkbox" id="lparse" checked> искать КУС по ссылке в описании</label>
   <button class="p" id="lgo">Собрать</button><button id="lcsv" class="hide">Скачать CSV</button><button id="lcancel" class="hide">Остановить</button></div>
  <div class="mut">Выгружает все задачи, связанные с указанной (любой тип связи): дата создания, статус, значения выбранных полей.
   КУС клиента берётся из поля «Наименование клиента». КУС по ссылке партнёра в отдельном поле не хранится — программа ищет его в описании (6 символов латиница/цифры, рядом слово «ссылка»).
   Если в описании не сказано, какой это КУС, строка помечается <span class="warn">проверить</span>; значение можно поправить прямо в таблице, правка попадёт в CSV.</div>
  <div id="lmsg" class="mut"></div>
 </div>
 <div class="card hide" id="lsum"></div>
 <div class="card hide" id="lres" style="overflow:auto"></div>
</section>
<section id="t-jr" class="hide">
 <div class="card"><b>Журналы изменений</b> <span class="mut">(папка jira_toolkit_data рядом с программой)</span>
  <table><thead><tr><th>Журнал</th><th>Создан</th><th>Изменений</th><th></th></tr></thead><tbody id="jrbody"></tbody></table></div>
 <div class="card hide" id="jrjob"><div class="row"><b id="jrtitle"></b><span id="jrsum" class="mut"></span></div><div class="bar"><i id="jrbar" style="width:0%"></i></div><div id="jrres" style="max-height:300px;overflow:auto;margin-top:8px"></div></div>
</section>
</main>
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
$("op").innerHTML=Object.entries(OPS).map(([k,v])=>`<option value="${k}">${v}</option>`).join("");
let connected=false;
function setConn(s){connected=!!s.connected;
  $("connmsg").className=connected?"ok":"mut";
  $("connmsg").textContent=connected?("Подключено: "+(s.user||"")+" · "+s.base):"Токен хранится только в памяти этого приложения и уходит только в вашу Jira.";
  $("bconn").classList.toggle("hide",connected);$("bdisc").classList.toggle("hide",!connected);
  $("token").classList.toggle("hide",connected);$("base").disabled=connected;}
$("bconn").onclick=async()=>{$("connmsg").className="mut";$("connmsg").textContent="Проверяю...";
  try{const j=await api("/api/connect",{base:$("base").value,token:$("token").value,insecure:$("insecure").checked});
    $("token").value="";setConn(j);loadJournals();}catch(e){$("connmsg").className="bad";$("connmsg").textContent="Ошибка: "+e.message;}};
$("bdisc").onclick=async()=>{await api("/api/disconnect",{});setConn({connected:false});};
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("on",x===b));
  ["bulk","dash","tree","lk","jr"].forEach(t=>$("t-"+t).classList.toggle("hide",t!==b.dataset.t));
  if(b.dataset.t==="jr")loadJournals();});
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
    $("prevbody").innerHTML=j.rows.map(r=>{c[r.action]++;return `<tr><td>${esc(r.key)}</td><td>${esc(r.summary)}</td><td>${esc(r.current)}</td>
      <td>${esc(OPS[r.op]||r.op)}: <b>${esc(r.value)}</b></td><td><span class="chip ${r.action}">${r.action==="apply"?"записать":r.action==="skip"?"пропуск":"ошибка"}</span> ${esc(r.reason)}</td></tr>`;}).join("");
    $("prevsum").textContent=`к записи: ${c.apply} · пропуск: ${c.skip} · ошибок: ${c.error}`;
    $("prevcard").classList.remove("hide");$("prevmsg").textContent="";$("bapply").dataset.n=c.apply;
  }catch(e){$("prevmsg").textContent="Ошибка: "+e.message;}};
$("conf").onchange=()=>{$("bapply").disabled=!($("conf").checked&&+($("bapply").dataset.n||0)>0);};
let curJob=null;
async function poll(jid,ui){
  for(;;){const j=await api("/api/job/"+jid);
    const pct=j.total?Math.round(100*j.done/j.total):0;
    $(ui.bar).style.width=pct+"%";$(ui.sum).textContent=`${j.done} из ${j.total} · ошибок: ${j.failed} · ${j.status}`;
    if(j.status!=="running"){$(ui.res).innerHTML=`<table><thead><tr><th>Задача</th><th>Операция</th><th>Результат</th><th>Детали</th></tr></thead><tbody>`+
      j.results.map(r=>`<tr><td>${esc(r.key)}</td><td>${esc(OPS[r.op]||r.op)}: ${esc(r.value)}</td><td>${esc(r.result)}</td><td>${esc(r.details)}</td></tr>`).join("")+`</tbody></table>`+
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
    $("jrbody").innerHTML=j.map(x=>`<tr><td>${esc(x.id)}</td><td>${esc(x.created)}</td><td>${x.count}</td><td><button class="d" data-j="${esc(x.id)}">Откатить</button></td></tr>`).join("")||`<tr><td colspan="4" class="mut">Пока нет</td></tr>`;
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
    $("dgbody").innerHTML=grp.map(([k,t])=>`<tr><td colspan="4" style="background:#e8eefb;font-weight:600">${esc(t)}</td></tr>`+p.gadgets.filter(g=>g.dash===k).map(g=>`<tr><td><input type="checkbox" class="dgsel" value="${esc(g.id)}" checked></td><td>${esc(g.title)}</td><td>${esc(g.kind)}${g.auto?"":' <span class="mut">(вручную)</span>'}</td><td><code>${esc(g.jql)}</code></td></tr>`).join("")).join("");
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
      return `<a class="tile ${cl}" ${r.link?`href="${esc(r.link)}" target="_blank" rel="noopener"`:""} title="${esc(r.jql)}"><div class="mut">${esc(r.name)}</div>
        <div class="n">${r.error?"—":r.total}</div><div class="mut" style="font-size:12px">${esc(r.error||"открыть в Jira")}</div></a>`;}).join("");
    $("dmsg").textContent="Обновлено "+new Date().toLocaleTimeString();
  }catch(e){$("dmsg").textContent="Ошибка: "+e.message;}};
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
  const v=getView().filter(n=>n.cat!=="none"),name=$("tsname").value.trim();
  if(!name){$("tmsg").className="bad";$("tmsg").textContent="Укажите название структуры.";return;}
  const sig=treeJob+"|"+[...excl].sort().join(",");
  if(createdSig===sig&&!confirm("Структура для этого набора задач уже создавалась. Создать ещё одну?"))return;
  if(!confirm(`Создать в Jira структуру «${name}»?\nЗадач: ${v.length}`+(excl.size?` (убрано вами: ${excl.size})`:"")+`\nПрава доступа программа не меняет.`))return;
  $("tmk").disabled=true;$("tmsg").className="mut";$("tmsg").textContent="Создаю структуру в Jira...";
  try{const r=await api("/api/tree/create",{job:treeJob,name:name,exclude:[...excl]});
    createdSig=sig;
    const ok=r.verified;$("tmsg").className=ok?"ok":"bad";
    $("tmsg").innerHTML=(ok?`Структура создана и проверена: ${r.found} из ${r.expected} строк. `:`Структура создана (№ ${esc(r.id)}), но есть замечания. `)+
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
   <span class="pbar"><i style="width:${w("done")};background:var(--ok)"></i><i style="width:${w("indeterminate")};background:var(--ac)"></i><i style="width:${w("new")};background:#8a96a3"></i></span>
   <span>готово ${s.done} · в работе ${s.indeterminate} · к выполнению ${s.new}</span></div>
   <div class="row mut"><span>первое начало: <b>${fmtD(s.first_start)}</b></span><span>завершение структуры: <b>${fmtD(s.last_end)}</b></span>
   <span>среднее время готовых задач: <b>${s.avg_done_days==null?"—":s.avg_done_days+" дн."}</b></span>
   <span class="${s.overdue?"od":""}">просрочено: <b>${s.overdue}</b></span>${s.no_access?`<span class="bad">нет доступа: ${s.no_access}</span>`:""}</div>
   ${ex.length?`<div class="row"><span class="mut">Убрано из структуры (${ex.length}):</span>${ex.map(k=>`<span class="chip c-new" style="display:inline-flex;gap:4px">${esc(k)} <a href="#" data-rs="${esc(k)}" style="color:#fff" title="Вернуть">↺</a></span>`).join("")}
     <button id="rsall">Вернуть все</button></div>`:""}
   ${t.truncated?`<div class="bad">Достигнут лимит задач: структура неполная. Увеличьте «максимум задач» или уменьшите глубину.</div>`:""}`;
  $("tsum").classList.remove("hide");
  $("tres").innerHTML=`<table><thead><tr><th>Задача</th><th>Связь</th><th>Статус</th><th>Исполнитель</th><th>Начало</th><th>Завершение</th><th>В работе, дн.</th><th>Срок</th><th></th></tr></thead><tbody id="tbody">`+
   v.map((n,i)=>`<tr data-i="${i}"><td style="padding-left:${8+n.level*20}px;white-space:nowrap">${n.has_children?`<button class="tg" data-tg="${esc(n.key)}">${collapsed.has(n.key)?"▸":"▾"}</button>`:`<span style="display:inline-block;width:22px"></span>`}
     <a href="${esc(t.base)}/browse/${esc(n.key)}" target="_blank" rel="noopener">${esc(n.key)}</a> <span class="mut">${esc(n.type)}</span><div style="margin-left:22px;white-space:normal">${esc(n.summary)}</div></td>
     <td class="mut">${esc(n.rel)}</td><td><span class="chip c-${n.cat}">${esc(n.status)}</span></td><td>${esc(n.assignee)}</td>
     <td>${n.start?(n.start_approx?"≈ ":"")+fmtD(n.start):"—"}</td><td>${fmtD(n.end)}</td><td>${n.days==null?"—":n.days}</td><td class="${n.overdue?"od":""}">${fmtD(n.due)}</td>
     <td>${n.pid>=0?`<button class="tg" data-rm="${esc(n.key)}" title="Убрать задачу${n.has_children?" и всё, что найдено через неё":""}" style="font-size:16px">✕</button>`:""}</td></tr>`).join("")+`</tbody></table>`;
  $("tres").classList.remove("hide");$("tcsv").classList.remove("hide");$("tmkrow").classList.remove("hide");
  document.querySelectorAll("[data-tg]").forEach(b=>b.onclick=()=>{const k=b.dataset.tg;if(collapsed.has(k))collapsed.delete(k);else collapsed.add(k);b.textContent=collapsed.has(k)?"▸":"▾";applyCollapse(v);});
  document.querySelectorAll("[data-rm]").forEach(b=>b.onclick=()=>{excl.add(b.dataset.rm);renderTree();});
  document.querySelectorAll("[data-rs]").forEach(a=>a.onclick=e=>{e.preventDefault();const k=a.dataset.rs;excl.delete(k);
    if(!treeData.nodes.some(n=>n.key===k)){$("tmsg").className="mut";$("tmsg").textContent="Задача "+k+" вернётся после повторной сборки структуры.";}
    renderTree();});
  if($("rsall"))$("rsall").onclick=()=>{const need=[...excl].some(k=>!treeData.nodes.some(n=>n.key===k));excl=new Set();
    if(need){$("tmsg").className="mut";$("tmsg").textContent="Часть задач вернётся после повторной сборки структуры.";}renderTree();};
  applyCollapse(v);}
function applyCollapse(v){
  const hidden=[];
  document.querySelectorAll("#tbody tr").forEach(tr=>{const i=+tr.dataset.i,p=v[i].pid;
    hidden[i]=p>=0&&(hidden[p]||collapsed.has(v[p].key));tr.style.display=hidden[i]?"none":"";});}
$("tcsv").onclick=()=>{
  const v=getView();
  const q=x=>'"'+String(x==null?"":x).replace(/"/g,'""')+'"';
  const head=["Уровень","Ключ","Заголовок","Тип","Связь","Статус","Категория","Исполнитель","Создана","Начало","Завершение","В работе (дн)","Срок","Ссылка"];
  const rows=v.map(n=>[n.level,n.key,n.summary,n.type,n.rel,n.status,{new:"К выполнению",indeterminate:"В работе",done:"Готово",none:"нет доступа"}[n.cat],n.assignee,n.created,(n.start_approx?"≈ ":"")+n.start,n.end,n.days==null?"":n.days,n.due,treeData.base+"/browse/"+n.key]);
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
   ${ex.length?`<div class="row"><span class="mut">Убрано (${ex.length}):</span>${ex.map(k=>`<span class="chip c-new" style="display:inline-flex;gap:4px">${esc(k)} <a href="#" data-lrs="${esc(k)}" style="color:#fff" title="Вернуть">↺</a></span>`).join("")}<button id="lrsall">Вернуть все</button></div>`:""}
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

api("/api/state").then(s=>{setConn(s);loadDash();}).catch(()=>loadDash().catch(()=>{}));
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
