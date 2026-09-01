#!/usr/bin/env python3
"""
Refresh de dados do dashboard "Governança de Produto — GRC Builder".

Busca dados frescos no Azure DevOps e substitui SOMENTE os blocos de dados
JSON embutidos no index.html (roadmap-data, flow-metrics-data,
timeline-cards-data) — o HTML/CSS/JS do dashboard NUNCA é tocado.

Uso:
    AZURE_DEVOPS_PAT=xxxxx python3 scripts/refresh_data.py

Requer a variável de ambiente AZURE_DEVOPS_PAT (Personal Access Token do
Azure DevOps com permissão de leitura em Work Items).
"""

import base64
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

ORG = "porttus"
PROJECT = "GRC Builder"
PROJECT_URL_ENC = "GRC%20Builder"

PAT = os.environ.get("AZURE_DEVOPS_PAT")
if not PAT:
    print("ERRO: variável de ambiente AZURE_DEVOPS_PAT não definida.")
    sys.exit(1)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_ROOT, "index.html")

RELEVANT_TREE_TYPES = "'Epic','Discovery','Feature','Solicitação','Melhoria','User Story','Bug','Spike','Incidente','Iniciativas'"
FLOW_TYPES_WIQL = "'Feature','Solicitação','Melhoria','User Story','Bug','Spike','Incidente','Iniciativas'"
RESOLVED = {"Closed", "Feito", "Removed"}
OPEN_STATES = {"New", "Backlog"}  # estados de "ainda não iniciado" — nem WIP, nem concluído
FM_FLOW_TYPES = ["Melhoria", "User Story", "Bug", "Spike", "Incidente", "Iniciativas"]
FM_ALL_TYPES = ["Feature", "Solicitação", "Melhoria", "User Story", "Bug", "Spike", "Incidente", "Iniciativas"]
EXCLUDE_FE = {"Feature", "Solicitação", "Discovery"}
EXCLUDE_SCATTER = {"Feature", "Solicitação"}
DEV_EXCLUDE = {"Lucas Shihomatsu", "Jonathan Assunção", "Thamerson Gomes",
               "Victor Resaghi", "Cesar Costa"}

WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime.now(timezone.utc)


def wiql(query):
    payload = json.dumps({"query": query})
    result = subprocess.run(
        ["curl", "-s", "-u", f":{PAT}", "-H", "Content-Type: application/json",
         "-d", payload, f"https://dev.azure.com/{ORG}/{PROJECT_URL_ENC}/_apis/wit/wiql?api-version=7.0"],
        capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def batch_fetch(ids, fields):
    all_items = []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        payload = json.dumps({"ids": chunk, "fields": fields})
        result = subprocess.run(
            ["curl", "-s", "-u", f":{PAT}", "-H", "Content-Type: application/json",
             "-d", payload,
             f"https://dev.azure.com/{ORG}/{PROJECT_URL_ENC}/_apis/wit/workitemsbatch?api-version=7.0"],
            capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        all_items.extend(data.get("value", []))
        time.sleep(0.05)
    return all_items


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def in_window(iso):
    d = parse_dt(iso)
    return d is not None and WINDOW_START <= d <= NOW


def pctl(data, p):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * (p / 100)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def stats_block(vals):
    if not vals:
        return {"count": 0, "avg": None, "median": None, "p85": None, "min": None, "max": None}
    return {
        "count": len(vals), "avg": round(statistics.mean(vals), 1),
        "median": round(statistics.median(vals), 1), "p85": pctl(vals, 85),
        "min": round(min(vals), 1), "max": round(max(vals), 1),
    }


def iso_week_key(dt):
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(week_key):
    y, w = week_key.split("-W")
    y, w = int(y), int(w)
    jan4 = datetime(y, 1, 4, tzinfo=timezone.utc)
    week1_monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
    monday = week1_monday + timedelta(weeks=w - 1)
    sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday, sunday


def month_key(dt):
    return dt.strftime("%Y-%m")


def year_key(dt):
    return dt.strftime("%Y")


def month_end(mk):
    y, mo = int(mk[:4]), int(mk[5:7])
    ny, nmo = (y, mo + 1) if mo < 12 else (y + 1, 1)
    return datetime(ny, nmo, 1, tzinfo=timezone.utc) - timedelta(seconds=1)


def year_end(yk):
    y = int(yk)
    return datetime(y + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)


# =========================================================
#  PARTE 1 — Árvore de Roadmap (Épicos + Discovery)
# =========================================================
def build_roadmap_tree():
    print("Buscando árvore de roadmap (Épicos + Discovery)...")
    q = (f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.WorkItemType] IN ({RELEVANT_TREE_TYPES})")
    ids = [wi["id"] for wi in wiql(q).get("workItems", [])]
    fields = ["System.Id", "System.Title", "System.WorkItemType", "System.State", "System.Parent"]
    items = batch_fetch(ids, fields)
    by_id_all = {it["fields"]["System.Id"]: it["fields"] for it in items}
    relevant_types = {"Epic", "Discovery", "Feature", "Solicitação", "Melhoria", "User Story", "Bug", "Spike", "Incidente", "Iniciativas"}

    def resolve_parent(id_):
        seen = set()
        cur = by_id_all.get(id_, {}).get("System.Parent")
        while cur is not None and cur not in seen:
            seen.add(cur)
            pf = by_id_all.get(cur)
            if pf is None:
                return None
            if pf.get("System.WorkItemType") in relevant_types:
                return cur
            cur = pf.get("System.Parent")
        return None

    nodes = {}
    for id_, f in by_id_all.items():
        t = f.get("System.WorkItemType")
        if t not in relevant_types:
            continue
        nodes[id_] = {"id": id_, "type": t, "title": f.get("System.Title", ""),
                      "state": f.get("System.State"), "true_parent": resolve_parent(id_)}

    children_of = defaultdict(list)
    for id_, n in nodes.items():
        if n["true_parent"] is not None:
            children_of[n["true_parent"]].append(id_)
    for p in children_of:
        children_of[p].sort(key=lambda cid: (nodes[cid]["state"] in RESOLVED, cid))

    def build_node(id_, level):
        n = nodes[id_]
        kids = [build_node(cid, level + 1) for cid in children_of.get(id_, [])]
        resolved = n["state"] in RESOLVED
        if kids:
            total = sum(k["total"] for k in kids)
            completed = sum(k["completed"] for k in kids)
        else:
            total, completed = 1, (1 if resolved else 0)
        pct = round(100 * completed / total) if total else 0
        return {"id": str(id_), "type": n["type"], "title": n["title"], "state": n["state"],
                "effective_state": n["state"], "level": level, "pct": pct, "total": total,
                "completed": completed, "resolved": resolved, "children": kids}

    def prune_display(node):
        kept = [prune_display(c) for c in node["children"] if not c["resolved"]]
        nn = dict(node)
        nn["children"] = kept
        return nn

    def count_nodes(node):
        return 1 + sum(count_nodes(c) for c in node["children"])

    epic_roots = sorted(i for i, n in nodes.items()
                         if n["type"] == "Epic" and n["true_parent"] is None and n["state"] not in RESOLVED)
    # Solicitações sem Épico pai são trabalho de ENTREGA (Delivery), não de
    # exploração — entram como raízes adicionais em Épicos, não em Discovery.
    orphan_solic = sorted(i for i, n in nodes.items() if n["type"] == "Solicitação" and n["true_parent"] is None)
    delivery_roots = epic_roots + orphan_solic
    epics_full = [build_node(r, 1) for r in delivery_roots]
    epics_display = [prune_display(r) for r in epics_full]
    e_total = sum(r["total"] for r in epics_full)
    e_completed = sum(r["completed"] for r in epics_full)
    epics_data = {
        "display": epics_display, "full": epics_full,
        "summary": {"total_items": e_total, "completed_items": e_completed,
                    "pct": round(100 * e_completed / e_total) if e_total else 0,
                    "n_roots": len(delivery_roots),
                    "visible_nodes": sum(count_nodes(r) for r in epics_display),
                    "full_nodes": sum(count_nodes(r) for r in epics_full),
                    "n_roots_active": sum(1 for r in epic_roots if nodes[r]["state"] == "Active")},
    }

    # Discovery: apenas itens do tipo Discovery em si — Solicitações órfãs
    # saíram daqui (ver acima) por serem trabalho de entrega, não exploração.
    disc_roots = sorted(i for i, n in nodes.items() if n["type"] == "Discovery" and n["true_parent"] is None)
    disc_full = [build_node(r, 1) for r in disc_roots]
    disc_display = [prune_display(r) for r in disc_full]
    d_total = sum(r["total"] for r in disc_full)
    d_completed = sum(r["completed"] for r in disc_full)
    discovery_data = {
        "display": disc_display, "full": disc_full,
        "summary": {"total_items": d_total, "completed_items": d_completed,
                    "pct": round(100 * d_completed / d_total) if d_total else 0,
                    "n_roots": len(disc_roots),
                    "visible_nodes": sum(count_nodes(r) for r in disc_display),
                    "full_nodes": sum(count_nodes(r) for r in disc_full)},
    }
    print(f"  Épicos: {epics_data['summary']}")
    print(f"  Discovery: {discovery_data['summary']}")
    return {"epics": epics_data, "discovery": discovery_data}, nodes, children_of


# =========================================================
#  PARTE 2 — Timeline (Feature/Solicitação/Discovery com datas)
# =========================================================
def fetch_iterations():
    print("Buscando sprints/iterations do time...")
    result = subprocess.run(
        ["curl", "-s", "-u", f":{PAT}",
         f"https://dev.azure.com/{ORG}/{PROJECT_URL_ENC}/_apis/work/teamsettings/iterations?api-version=7.0"],
        capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    # mapa: caminho completo da iteração (bate exatamente com System.IterationPath) -> {name, start, end}
    iter_map = {}
    for it in data.get("value", []):
        attrs = it.get("attributes", {})
        start = attrs.get("startDate")
        finish = attrs.get("finishDate")
        iter_map[it["path"]] = {
            "name": it["name"],
            "start": start[:10] if start else None,
            "end": finish[:10] if finish else None,
        }
    print(f"  {len(iter_map)} sprints encontrados.")
    return iter_map


def build_timeline_cards(roadmap_data, roadmap_nodes, children_of, iter_map, median_spike_cycle):
    print("Buscando datas (Start/Target) para cards da Timeline...")
    # Apenas Feature/Solicitação/Discovery alcançáveis a partir das árvores
    # ativas (epics.full + discovery.full) — não do universo total do projeto.
    tl_id_set = set()

    def collect(nodes):
        for n in nodes:
            if n["type"] in ("Feature", "Solicitação", "Discovery"):
                tl_id_set.add(int(n["id"]))
            collect(n.get("children", []))

    collect(roadmap_data["epics"]["full"])
    collect(roadmap_data["discovery"]["full"])
    tl_ids = sorted(tl_id_set)

    fields = ["System.Id", "Microsoft.VSTS.Scheduling.StartDate", "Microsoft.VSTS.Scheduling.TargetDate",
               "System.AssignedTo", "System.Tags", "System.IterationPath",
               "Microsoft.VSTS.Common.ActivatedDate", "Microsoft.VSTS.Common.ClosedDate", "System.CreatedDate"]
    items = batch_fetch(tl_ids, fields)
    sched = {it["fields"]["System.Id"]: it["fields"] for it in items}

    def count_all(id_):
        total, completed = 1, 0
        n = roadmap_nodes[id_]
        if n["state"] in RESOLVED:
            completed = 1
        kids = children_of.get(id_, [])
        if kids:
            total, completed = 0, 0
            for k in kids:
                kt, kc = count_all(k)
                total += kt
                completed += kc
        return total, completed

    cards = []
    for id_ in tl_ids:
        n = roadmap_nodes[id_]
        f = sched.get(id_, {})
        total, completed = count_all(id_)
        pct = round(100 * completed / total) if total else 0
        assignee = f.get("System.AssignedTo")
        start_date = f.get("Microsoft.VSTS.Scheduling.StartDate")
        target_date = f.get("Microsoft.VSTS.Scheduling.TargetDate")
        date_source = "real" if (start_date and target_date) else None
        # Problemas conhecidos de preenchimento errado no Azure DevOps:
        # - Itens com start > target têm datas invertidas no ADO.
        #   Para itens Feito/Closed com esse problema, preferimos o par
        #   ActivatedDate/ClosedDate que reflete o trabalho real.
        # - Itens Removed não devem aparecer no roadmap visual.
        if n["state"] == "Removed":
            continue  # pula — não renderizar no roadmap
        if start_date and target_date and target_date < start_date:
            # Datas invertidas no ADO: se houver ActivatedDate/ClosedDate, usa-os
            activated = f.get("Microsoft.VSTS.Common.ActivatedDate")
            closed    = f.get("Microsoft.VSTS.Common.ClosedDate")
            if activated and closed and closed >= activated:
                start_date  = activated
                target_date = closed
                date_source = "historico_real"
            else:
                # Inverte as datas do ADO para pelo menos ter algo coerente
                start_date, target_date = target_date, start_date
                date_source = "historico_real"

        # O Azure DevOps da Porttus limpa Start/Target Date quando o item é
        # fechado ("Feito"/"Closed"). Hierarquia de fallback por situação:
        #
        # RESOLVIDO sem datas:
        #   start  → ActivatedDate → CreatedDate
        #   target → ClosedDate (não usa ChangedDate: pode ser posterior ao encerramento)
        #
        # EM ABERTO sem ambas as datas:
        #   start  → ActivatedDate → CreatedDate
        #   target → estimativa via Lei de Little usando o Cycle Time mediano
        #            (nunca usa ChangedDate como target pois é data de edição, não entrega)
        if not start_date or not target_date:
            activated = f.get("Microsoft.VSTS.Common.ActivatedDate")
            closed    = f.get("Microsoft.VSTS.Common.ClosedDate")
            created   = f.get("System.CreatedDate")

            if n["state"] in RESOLVED:
                start_date  = start_date  or activated or created
                target_date = target_date or closed    or activated or created
                date_source = date_source or "historico_real"
            else:
                # Em aberto: usa ActivatedDate como âncora de start real;
                # se não tiver, CreatedDate. Target via Lei de Little.
                est_start = start_date or activated or created
                if est_start:
                    est_start_dt = parse_dt(est_start)
                    est_days = max(1, total) * median_spike_cycle
                    est_target_dt = est_start_dt + timedelta(days=round(est_days))
                    start_date  = start_date  or est_start
                    target_date = target_date or est_target_dt.isoformat()
                    date_source = date_source or "estimativa_lei_de_little"
        iteration_path = f.get("System.IterationPath")
        sprint = iter_map.get(iteration_path) if iteration_path else None
        raw_tags = f.get("System.Tags", "") or ""
        tags_list = [t.strip() for t in raw_tags.split(";") if t.strip()]
        cards.append({
            "id": str(id_), "type": n["type"], "title": n["title"], "state": n["state"],
            "pct": pct, "startDate": start_date[:10] if start_date else None,
            "targetDate": target_date[:10] if target_date else None,
            "assignee": assignee.get("displayName") if isinstance(assignee, dict) else None,
            "tags": tags_list, "sprint": sprint, "dateSource": date_source,
            "resolved": n["state"] in RESOLVED,
        })
    n_real = sum(1 for c in cards if c["dateSource"] == "real")
    n_hist = sum(1 for c in cards if c["dateSource"] == "historico_real")
    n_est = sum(1 for c in cards if c["dateSource"] == "estimativa_lei_de_little")
    n_none = sum(1 for c in cards if c["dateSource"] is None)
    print(f"  Timeline cards: {len(cards)} (datas reais: {n_real}, histórico real: {n_hist}, "
          f"estimadas via Lei de Little: {n_est}, sem data: {n_none})")
    return cards


# =========================================================
#  PARTE 3 — Flow Metrics completo
# =========================================================
def build_flow_metrics():
    print("Buscando itens de fluxo (Feature/Solicitação/Melhoria/UserStory/Bug/Spike)...")
    q = (f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.WorkItemType] IN ({FLOW_TYPES_WIQL})")
    ids = [wi["id"] for wi in wiql(q).get("workItems", [])]
    fields = ["System.Id", "System.WorkItemType", "System.State", "System.CreatedDate",
              "Microsoft.VSTS.Common.ActivatedDate", "Microsoft.VSTS.Common.ClosedDate",
              "System.ChangedDate", "System.Tags", "System.AssignedTo",
              "Microsoft.VSTS.Scheduling.StoryPoints"]
    items = batch_fetch(ids, fields)
    print(f"  Total de itens: {len(items)}")

    records = []
    for it in items:
        f = it["fields"]
        t = f["System.WorkItemType"]
        state = f.get("System.State")
        resolved = state in RESOLVED
        created = f.get("System.CreatedDate")
        activated = f.get("Microsoft.VSTS.Common.ActivatedDate")
        closed = f.get("Microsoft.VSTS.Common.ClosedDate")
        changed = f.get("System.ChangedDate")
        end_date = closed if resolved else None
        if resolved and not end_date:
            end_date = changed
        c_dt, a_dt, e_dt = parse_dt(created), parse_dt(activated), parse_dt(end_date)
        lead_days = (e_dt - c_dt).total_seconds() / 86400 if (c_dt and e_dt) else None
        cycle_days = (e_dt - a_dt).total_seconds() / 86400 if (a_dt and e_dt) else None
        assignee = f.get("System.AssignedTo")
        records.append({
            "id": f["System.Id"], "type": t, "state": state, "resolved": resolved,
            "created": created, "activated": activated, "end_date": end_date,
            "lead_days": lead_days, "cycle_days": cycle_days,
            "assignee": assignee.get("displayName") if isinstance(assignee, dict) else None,
            "tags": f.get("System.Tags", ""),
            "story_points": f.get("Microsoft.VSTS.Scheduling.StoryPoints"),
        })

    # ---- overall / by_type / flow_efficiency (período Jan/2026 - hoje) ----
    recs_period = [r for r in records if r["resolved"] and in_window(r["end_date"])]
    lead_all = [r["lead_days"] for r in recs_period if r["lead_days"] is not None]
    cycle_all = [r["cycle_days"] for r in recs_period if r["cycle_days"] is not None]
    overall = {"lead_time": stats_block(lead_all), "cycle_time": stats_block(cycle_all)}

    by_type = {}
    for t in FM_ALL_TYPES:
        lv = [r["lead_days"] for r in recs_period if r["type"] == t and r["lead_days"] is not None]
        cv = [r["cycle_days"] for r in recs_period if r["type"] == t and r["cycle_days"] is not None]
        if lv or cv:
            by_type[t] = {"lead_time": stats_block(lv), "cycle_time": stats_block(cv)}

    total_resolved_items = len(recs_period)
    relevant = [r for r in records if in_window(r.get("created")) or in_window(r.get("end_date")) or not r["resolved"]]
    total_items_considered = len(relevant)

    recs_fe = [r for r in recs_period if r["type"] not in EXCLUDE_FE]
    lead_fe = [r["lead_days"] for r in recs_fe if r["lead_days"] is not None]
    cycle_fe = [r["cycle_days"] for r in recs_fe if r["cycle_days"] is not None]
    lt_med, ct_med = statistics.median(lead_fe), statistics.median(cycle_fe)
    lt_p85, ct_p85 = pctl(lead_fe, 85), pctl(cycle_fe, 85)
    lt_avg, ct_avg = statistics.mean(lead_fe), statistics.mean(cycle_fe)
    flow_efficiency = {
        "median_pct": round(100 * ct_med / lt_med, 1), "p85_pct": round(100 * ct_p85 / lt_p85, 1),
        "avg_pct": round(100 * ct_avg / lt_avg, 1), "excluded_types": ["Feature", "Solicitação", "Discovery"],
        "n_lead": len(lead_fe), "n_cycle": len(cycle_fe),
    }

    # ---- semanas / meses / anos ----
    weeks = []
    cur = WINDOW_START
    while cur <= NOW:
        wk = iso_week_key(cur)
        if wk not in weeks:
            weeks.append(wk)
        cur += timedelta(days=7)
    last_wk = iso_week_key(NOW)
    if last_wk not in weeks:
        weeks.append(last_wk)
    weeks = sorted(set(weeks), key=lambda w: (int(w.split("-W")[0]), int(w.split("-W")[1])))
    week_end_map = {w: week_bounds(w)[1] for w in weeks}

    months = []
    y, m = 2026, 1
    while (y, m) <= (NOW.year, NOW.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    years = [str(yy) for yy in range(2026, NOW.year + 1)]

    def compute_bundle(period_keys, key_fn, end_fn):
        tbt = {p: {t: 0 for t in FM_FLOW_TYPES} for p in period_keys}
        bc = {p: 0 for p in period_keys}
        bd = {p: 0 for p in period_keys}
        for r in records:
            if r["type"] == "Bug":
                if r["created"] and in_window(r["created"]):
                    pk = key_fn(parse_dt(r["created"]))
                    if pk in bc:
                        bc[pk] += 1
                if r["resolved"] and r["end_date"] and in_window(r["end_date"]):
                    pk = key_fn(parse_dt(r["end_date"]))
                    if pk in bd:
                        bd[pk] += 1
            if r["resolved"] and r["end_date"] and in_window(r["end_date"]) and r["type"] in FM_FLOW_TYPES:
                pk = key_fn(parse_dt(r["end_date"]))
                if pk in tbt:
                    tbt[pk][r["type"]] += 1

        def wip_at(dt_end):
            c = 0
            for r in records:
                if r["activated"]:
                    act_dt = parse_dt(r["activated"])
                    if act_dt <= dt_end:
                        end_dt = parse_dt(r["end_date"]) if r["end_date"] else None
                        if not r["resolved"] or (end_dt and end_dt > dt_end):
                            c += 1
            return c

        wip_series = [{"period": p, "wip": wip_at(min(end_fn(p), NOW))} for p in period_keys]
        return {"periods": period_keys, "throughput_by_type": tbt, "bugs_created": bc,
                "bugs_delivered": bd, "wip_series": wip_series}

    print("  Calculando granularidade semanal...")
    gran_week = compute_bundle(weeks, iso_week_key, lambda p: week_end_map[p])
    print("  Calculando granularidade mensal...")
    gran_month = compute_bundle(months, month_key, month_end)
    print("  Calculando granularidade anual...")
    gran_year = compute_bundle(years, year_key, year_end)
    gran = {"week": gran_week, "month": gran_month, "year": gran_year}

    throughput_avg_per_week = round(
        sum(sum(v.values()) for v in gran_week["throughput_by_type"].values()) / len(weeks), 1)

    # ---- histograma / envelhecimento / bloqueios / dispersão ----
    recs_hist = [r for r in recs_period if r["type"] not in EXCLUDE_FE]
    cycle_vals = [r["cycle_days"] for r in recs_hist if r["cycle_days"] is not None]
    buckets = [(0, 3), (3, 7), (7, 14), (14, 21), (21, 35), (35, 60), (60, 999999)]
    labels = ["0-3d", "3-7d", "7-14d", "14-21d", "21-35d", "35-60d", "60d+"]
    histogram = [{"label": l, "count": sum(1 for v in cycle_vals if lo <= v < hi)}
                 for (lo, hi), l in zip(buckets, labels)]

    aging_items = []
    for r in records:
        if not r["resolved"] and r["state"] not in OPEN_STATES and r["type"] not in EXCLUDE_FE:
            age_ref = r["activated"] or r["created"]  # fallback: nem todo item tem ActivatedDate preenchido
            age = (NOW - parse_dt(age_ref)).total_seconds() / 86400
            aging_items.append({"id": r["id"], "type": r["type"], "age_days": round(age, 1)})
    aging_items.sort(key=lambda x: -x["age_days"])
    age_buckets = [(0, 7), (7, 14), (14, 30), (30, 60), (60, 999999)]
    age_labels = ["0-7d", "7-14d", "14-30d", "30-60d", "60d+"]
    aging_hist = [{"label": l, "count": sum(1 for a in aging_items if lo <= a["age_days"] < hi)}
                  for (lo, hi), l in zip(age_buckets, age_labels)]

    blocked = [r for r in records if not r["resolved"] and "blocked" in (r.get("tags") or "").lower()]

    scatter = []
    for r in records:
        if r["resolved"] and r["cycle_days"] is not None and in_window(r["end_date"]) and r["type"] not in EXCLUDE_SCATTER:
            scatter.append({"date": r["end_date"][:10], "cycle_days": round(r["cycle_days"], 1),
                             "type": r["type"], "id": r["id"]})
    scatter.sort(key=lambda x: x["date"])

    # ---- títulos para aging/blocked ----
    need_ids = list({item["id"] for item in aging_items[:15]} | {b["id"] for b in blocked})
    titles = {}
    if need_ids:
        title_items = batch_fetch(need_ids, ["System.Id", "System.Title"])
        titles = {it["fields"]["System.Id"]: it["fields"]["System.Title"] for it in title_items}

    wip_aging_top = [{**item, "title": titles.get(item["id"], "")} for item in aging_items[:15]]
    blocked_items = [{"id": r["id"], "type": r["type"], "title": titles.get(r["id"], ""), "state": r["state"]}
                     for r in blocked]

    # ---- WIP atual ----
    # WIP real = não concluído e já saiu do estado inicial (New/Backlog) —
    # não depende de ActivatedDate estar preenchido, já que nem todo fluxo
    # customizado do processo garante isso (ex: Spikes em "Análise Técnica"
    # sem essa data setada, mas visivelmente em andamento no board).
    wip_now = [r for r in records if not r["resolved"] and r["state"] not in OPEN_STATES and r["type"] in FM_FLOW_TYPES]
    wip_now_by_type = dict(Counter(r["type"] for r in wip_now))
    wip_now_total = len(wip_now)

    # ---- capacity (Lei de Little) ----
    cycle_avg = overall["cycle_time"]["avg"]
    capacity_per_day = round(wip_now_total / cycle_avg, 2) if cycle_avg else 0
    capacity_per_week = round(capacity_per_day * 7, 1)
    utilization_pct = round(100 * throughput_avg_per_week / capacity_per_week, 1) if capacity_per_week else 0
    capacity = {"wip_now": wip_now_total, "cycle_time_avg": cycle_avg, "capacity_per_day": capacity_per_day,
                "capacity_per_week": capacity_per_week, "actual_throughput_per_week": throughput_avg_per_week,
                "utilization_pct": utilization_pct}

    # ---- nota de lote de bugs (outlier) ----
    bugs_created_dates = defaultdict(list)
    for r in records:
        if r["type"] == "Bug" and r["created"] and in_window(r["created"]):
            d = parse_dt(r["created"])
            bugs_created_dates[iso_week_key(d)].append(d.date().isoformat())
    bugs_notes = {}
    for wk, dates in bugs_created_dates.items():
        if len(dates) < 10:
            continue
        top = Counter(dates).most_common(2)
        top_count = sum(c for _, c in top)
        if top_count / len(dates) > 0.6 and top_count >= 20:
            bugs_notes[wk] = (f"{top_count} dos {len(dates)} bugs criados nesta semana entraram em um lote de "
                              f"triagem concentrado em 1-2 dias, não reporte orgânico dia a dia.")

    # ---- por desenvolvedor (dedicação, entregas, produtividade, story points) ----
    def build_dev_periods(period_keys, key_fn):
        dev_stats = defaultdict(lambda: defaultdict(lambda: {"delivered": 0, "lead_sum": 0.0, "lead_n": 0,
                                                               "cycle_sum": 0.0, "cycle_n": 0}))
        for r in records:
            if r["type"] in EXCLUDE_FE or r["type"] not in FM_FLOW_TYPES:
                continue
            if not r["resolved"] or not r.get("assignee") or not r["end_date"]:
                continue
            if not in_window(r["end_date"]):
                continue
            pk = key_fn(parse_dt(r["end_date"]))
            if pk not in period_keys:
                continue
            s = dev_stats[r["assignee"]][pk]
            s["delivered"] += 1
            if r["lead_days"] is not None:
                s["lead_sum"] += r["lead_days"]
                s["lead_n"] += 1
            if r["cycle_days"] is not None:
                s["cycle_sum"] += r["cycle_days"]
                s["cycle_n"] += 1
        return dev_stats

    dedication = defaultdict(Counter)
    for r in records:
        if not r["resolved"] and r["state"] not in OPEN_STATES and r.get("assignee") and r["type"] in FM_FLOW_TYPES:
            dedication[r["assignee"]][r["type"]] += 1

    def finalize_dev(dev_stats, period_keys):
        devs = {}
        for dev in set(dev_stats.keys()) | set(dedication.keys()):
            period_data = {pk: dev_stats[dev].get(pk, {"delivered": 0, "lead_sum": 0.0, "lead_n": 0,
                                                        "cycle_sum": 0.0, "cycle_n": 0}) for pk in period_keys}
            wip_by_type = dict(dedication.get(dev, {}))
            devs[dev] = {"periods": period_data, "wip_by_type": wip_by_type,
                         "wip_total": sum(wip_by_type.values())}
        return devs

    gran_dev = {
        "week": {"periods": weeks, "developers": finalize_dev(build_dev_periods(weeks, iso_week_key), weeks)},
        "month": {"periods": months, "developers": finalize_dev(build_dev_periods(months, month_key), months)},
        "year": {"periods": years, "developers": finalize_dev(build_dev_periods(years, year_key), years)},
    }

    dev_delivery = defaultdict(Counter)
    for r in records:
        if r["type"] in EXCLUDE_FE or r["type"] not in FM_FLOW_TYPES:
            continue
        if r["resolved"] and r.get("assignee") and in_window(r["end_date"]):
            dev_delivery[r["assignee"]][r["type"]] += 1

    developers_list = []
    for dev in set(dedication.keys()) | set(dev_delivery.keys()):
        wip_by_type = dict(dedication.get(dev, {}))
        delivered_by_type = dict(dev_delivery.get(dev, {}))
        total_delivered = sum(delivered_by_type.values())
        developers_list.append({
            "name": dev, "wip_by_type": wip_by_type, "wip_total": sum(wip_by_type.values()),
            "delivered_by_type": delivered_by_type, "delivered_total": total_delivered,
            "avg_per_month": round(total_delivered / len(weeks), 1),
        })
    developers_list.sort(key=lambda d: -d["delivered_total"])
    developer_metrics = {"developers": developers_list, "months_count": len(weeks),
                         "period_label": "jan/2026 - hoje (semanal)"}

    dev_sp = defaultdict(lambda: {"total_sp": 0.0, "n": 0})
    for r in records:
        if r["type"] in EXCLUDE_FE or r["type"] not in FM_FLOW_TYPES:
            continue
        if r["resolved"] and r.get("assignee") and in_window(r["end_date"]) and r["story_points"] is not None:
            dev_sp[r["assignee"]]["total_sp"] += r["story_points"]
            dev_sp[r["assignee"]]["n"] += 1
    dev_story_points = {}
    for dev, d in dev_sp.items():
        avg = round(d["total_sp"] / d["n"], 1) if d["n"] else None
        dev_story_points[dev] = {"delivered_total_sp": round(d["total_sp"], 1), "delivered_items_with_sp": d["n"],
                                 "avg_sp_per_item": avg}

    flow_data = {
        "overall": overall, "by_type": by_type,
        "total_resolved_items": total_resolved_items, "total_items_considered": total_items_considered,
        "flow_efficiency": flow_efficiency, "throughput_avg_per_month": throughput_avg_per_week,
        "cycle_time_histogram": histogram, "wip_aging_histogram": aging_hist, "wip_aging_top": wip_aging_top,
        "blocked_items": blocked_items, "blocked_count": len(blocked_items),
        "wip_now_total": wip_now_total, "wip_now_by_type": wip_now_by_type, "bugs_notes": bugs_notes,
        "cycle_scatter": scatter, "capacity": capacity, "developer_metrics": developer_metrics,
        "dev_efficiency": gran_dev["week"], "dev_story_points": dev_story_points,
        "gran": gran, "gran_dev": gran_dev,
    }
    print(f"  Lead/Cycle Time recalculados. Flow Efficiency: {flow_efficiency['median_pct']}%")
    print(f"  Semanas: {weeks[0]} até {weeks[-1]} ({len(weeks)} semanas)")
    return flow_data


# =========================================================
#  PARTE 4 — Fila de Priorização (Backlog Priority / Stack Rank real)
# =========================================================
# =========================================================
#  PARTE 4b — Mapa de Demandas por Tipo (universo total do projeto)
# =========================================================
def build_type_map():
    print("Buscando Mapa de Demandas por Tipo (jan/2026 em diante)...")
    TYPE_ORDER = ['Epic', 'Discovery', 'Feature', 'Solicitação', 'Melhoria', 'User Story', 'Bug', 'Spike', 'Incidente', 'Iniciativas']
    types_wiql = ",".join(f"'{t}'" for t in TYPE_ORDER)
    q = (f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.WorkItemType] IN ({types_wiql}) "
         f"AND [System.CreatedDate] >= '2026-01-01'")
    ids = [wi["id"] for wi in wiql(q).get("workItems", [])]
    fields = ["System.Id", "System.WorkItemType", "System.State"]
    items = batch_fetch(ids, fields)

    counts = {t: {"total": 0, "open": 0, "done": 0} for t in TYPE_ORDER}
    for it in items:
        f = it["fields"]
        t = f["System.WorkItemType"]
        if t not in counts:
            continue
        counts[t]["total"] += 1
        if f["System.State"] in RESOLVED:
            counts[t]["done"] += 1
        else:
            counts[t]["open"] += 1

    grand_total = sum(c["total"] for c in counts.values())
    type_map = []
    for t in TYPE_ORDER:
        c = counts[t]
        type_map.append({
            "type": t, "total": c["total"], "open": c["open"], "done": c["done"],
            "pct": round(100 * c["total"] / grand_total, 1) if grand_total else 0,
        })
    print(f"  Total mapeado: {grand_total} itens.")
    return {"items": type_map, "grand_total": grand_total}


# =========================================================
#  PARTE 4c — Composição por frente de trabalho (Backlog -> Em teste)
#  Regra: itens de Melhoria/User Story/Bug/Spike CRIADOS entre
#  janeiro/2026 e hoje, já triados (estado a partir de "Backlog",
#  excluindo "New" que ainda não entrou no board) e ainda não
#  finalizados (exclui Resolved/Closed/Removed).
# =========================================================
def build_activity_composition():
    print("Buscando Composição por Frente de Trabalho (Backlog->Em teste, jan/2026-hoje)...")
    COMP_TYPES = ['Melhoria', 'User Story', 'Bug', 'Spike']
    types_wiql = ",".join(f"'{t}'" for t in COMP_TYPES)
    q = (f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.WorkItemType] IN ({types_wiql}) "
         f"AND [System.CreatedDate] >= '2026-01-01'")
    ids = [wi["id"] for wi in wiql(q).get("workItems", [])]
    fields = ["System.Id", "System.WorkItemType", "System.State"]
    items = batch_fetch(ids, fields)

    EXCLUDED_STATES = {"New", "Resolved", "Closed", "Removed"}
    counts = {t: 0 for t in COMP_TYPES}
    for it in items:
        f = it["fields"]
        t = f["System.WorkItemType"]
        s = f["System.State"]
        if t in counts and s not in EXCLUDED_STATES:
            counts[t] += 1

    total = sum(counts.values())
    result = {}
    for t in COMP_TYPES:
        result[t] = {"count": counts[t], "pct": round(100 * counts[t] / total) if total else 0}
    print(f"  Total em andamento (Backlog->Em teste, jan/2026-hoje): {total} — {result}")
    return {"items": result, "total": total, "period_label": "jan/2026 - hoje"}


def build_priority_queue():
    print("Buscando Fila de Priorização (itens Novo/A fazer, todos os tipos)...")
    QUEUE_TYPES = "'Discovery','Feature','Solicitação','Melhoria','User Story','Bug','Spike','Incidente','Iniciativas'"
    q = (f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.WorkItemType] IN ({QUEUE_TYPES}) "
         f"AND [System.State] IN ('A fazer','New')")
    ids = [wi["id"] for wi in wiql(q).get("workItems", [])]
    fields = ["System.Id", "System.WorkItemType", "System.State", "System.Title",
              "Microsoft.VSTS.Common.StackRank", "Microsoft.VSTS.Common.Priority", "System.AssignedTo"]
    items = batch_fetch(ids, fields)

    def sort_key(it):
        rank = it["fields"].get("Microsoft.VSTS.Common.StackRank")
        has_rank = rank is not None
        return (not has_rank, rank if has_rank else it["fields"]["System.Id"])

    items.sort(key=sort_key)

    PRIO_LABEL = {1: "Crítica", 2: "Alta", 3: "Média", 4: "Baixa"}
    queue = []
    for pos, it in enumerate(items, start=1):
        f = it["fields"]
        assignee = f.get("System.AssignedTo")
        queue.append({
            "position": pos, "id": f["System.Id"], "type": f["System.WorkItemType"],
            "title": f["System.Title"], "state": f["System.State"],
            "priority": f.get("Microsoft.VSTS.Common.Priority"),
            "priorityLabel": PRIO_LABEL.get(f.get("Microsoft.VSTS.Common.Priority")),
            "assignee": assignee.get("displayName") if isinstance(assignee, dict) else None,
        })
    print(f"  Fila de Priorização: {len(queue)} itens.")
    return queue


# =========================================================
#  PARTE 5 — Injeção nos blocos JSON do index.html
# =========================================================
def replace_json_block(html, script_id, new_data):
    pattern = re.compile(rf'(<script id="{script_id}" type="application/json">)(.*?)(</script>)', re.DOTALL)
    m = pattern.search(html)
    if not m:
        print(f"  AVISO: bloco '{script_id}' não encontrado — pulando.")
        return html
    new_json = json.dumps(new_data, ensure_ascii=False)
    return html[:m.start()] + m.group(1) + new_json + m.group(3) + html[m.end():]


def main():
    print(f"=== Refresh iniciado em {NOW.isoformat()} ===")
    if not os.path.exists(INDEX_PATH):
        print(f"ERRO: {INDEX_PATH} não encontrado.")
        sys.exit(1)

    html = open(INDEX_PATH, encoding="utf-8").read()

    roadmap_data, roadmap_nodes, children_of = build_roadmap_tree()
    iter_map = fetch_iterations()
    flow_data = build_flow_metrics()
    median_spike_cycle = None
    if flow_data["by_type"].get("Spike") and flow_data["by_type"]["Spike"]["cycle_time"]["median"] is not None:
        median_spike_cycle = flow_data["by_type"]["Spike"]["cycle_time"]["median"]
    elif flow_data["overall"]["cycle_time"]["median"] is not None:
        median_spike_cycle = flow_data["overall"]["cycle_time"]["median"]
    else:
        median_spike_cycle = 4.2  # fallback histórico conhecido
    timeline_cards = build_timeline_cards(roadmap_data, roadmap_nodes, children_of, iter_map, median_spike_cycle)
    priority_queue = build_priority_queue()
    type_map = build_type_map()
    activity_composition = build_activity_composition()

    html = replace_json_block(html, "roadmap-data", roadmap_data)
    html = replace_json_block(html, "timeline-cards-data", timeline_cards)
    html = replace_json_block(html, "flow-metrics-data", flow_data)
    html = replace_json_block(html, "priority-queue-data", priority_queue)
    html = replace_json_block(html, "type-map-data", type_map)
    html = replace_json_block(html, "activity-composition-data", activity_composition)

    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)

    print("=== Refresh concluído com sucesso. index.html atualizado. ===")
    print("NOTA: a aba 'Visão Estratégica' (roadmap embutido com itens de Discovery) "
          "não é atualizada automaticamente por este script — segue com o snapshot manual mais recente.")


if __name__ == "__main__":
    main()
