"""
Zion Jobs Scraper — Multi-source, sem IA
Busca vagas em RemoteOK, Remotive, Freelancer.com, Jooble e Adzuna.
Filtra, pontua por relevância e faz upsert direto na tabela `vagas` do
Supabase (schema unificado, compartilhado com as vagas postadas por empresa).

Portado do GetFreelas (facincanitech/GetFreelas) — mesma lógica de busca e
pontuação, só a publicação final muda: em vez de gerar/commitar JSON, grava
direto no Postgres do Supabase usando a service role key (bypassa RLS).
"""

import json
import re
import hashlib
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CFG_FILE = BASE_DIR / "config.json"

with open(CFG_FILE, encoding="utf-8") as f:
    CFG = json.load(f)

SB_URL              = CFG.get("supabase_url", "")
SB_SERVICE_ROLE_KEY = CFG.get("supabase_service_role_key", "")
MAX_JOBS  = CFG.get("max_jobs", 80)
SCORE_MIN = CFG.get("score_minimo", 3)

JOOBLE_KEY        = CFG.get("jooble_api_key", "")
JOOBLE_KEY_BACKUP = CFG.get("jooble_api_key_backup", "")
ADZUNA_ID   = CFG.get("adzuna_app_id", "")
ADZUNA_KEY  = CFG.get("adzuna_app_key", "")

BOOST = [w.lower() for w in CFG.get("palavras_boost", [])]
BLOCK = [w.lower() for w in CFG.get("palavras_block", [])]

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def fetch_json(url, headers=None, data=None, method="GET"):
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {"User-Agent": "ZionJobs-Scraper/1.0"},
        method=method
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").strip()

_BR = ['brasil','brazil','sao paulo','são paulo','rio de janeiro','curitiba','belo horizonte',
       'porto alegre','florianopolis','florianópolis','salvador','brasilia','brasília',
       'fortaleza','recife','manaus','goiania','goiânia','campinas','r$ ','brl']
_US = ['united states','usa',' us ','new york','los angeles','san francisco','chicago',
       'miami','seattle','boston','austin','texas','california','silicon valley','usd']
_CA = ['canada','toronto','vancouver','montreal','calgary','ottawa','edmonton']
_EU = ['europe','uk ','london','paris','berlin','amsterdam','lisbon','madrid','rome',
       'dublin','germany','france','spain','italy','netherlands','ireland','eur ']
_CN = ['china','shanghai','beijing','shenzhen','guangzhou','chengdu','hong kong']
_AU = ['australia','sydney','melbourne','brisbane','perth','adelaide']

def detect_country(location=''):
    loc = (location or '').lower().strip()
    if not loc or loc in ('worldwide','global','anywhere','remote','remoto',''):
        return 'global'
    for kw in _BR:
        if kw in loc: return 'BR'
    for kw in _US:
        if kw in loc: return 'US'
    for kw in _CA:
        if kw in loc: return 'CA'
    for kw in _EU:
        if kw in loc: return 'EU'
    for kw in _CN:
        if kw in loc: return 'CN'
    for kw in _AU:
        if kw in loc: return 'AU'
    return 'global'

_PT_WORDS = [
    'você', 'não', 'para', 'com', 'uma', 'projeto', 'vaga', 'preciso', 'precisa',
    'desenvolv', 'profissional', 'experiência', 'trabalh', 'empresa', 'contrat',
    'buscamos', 'procuro', 'procuramos', 'atuação', 'necessári', 'conhecimento'
]

def detect_lang(text):
    t = (text or '').lower()
    hints = sum(t.count(w) for w in _PT_WORDS)
    accented = sum(t.count(c) for c in 'ãõáéíóúâêôç')
    return 'pt' if (hints >= 2 or accented >= 3) else 'en'

def _parse_date(date_val):
    try:
        if isinstance(date_val, (int, float)):
            return datetime.fromtimestamp(date_val, tz=timezone.utc)
        dt = datetime.fromisoformat(str(date_val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def iso_of(date_val):
    dt = _parse_date(date_val)
    return (dt or datetime.now(timezone.utc)).isoformat()

def urgency(date_val):
    dt = _parse_date(date_val)
    if not dt:
        return "normal"
    h = int((datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    if h < 12:  return "hot"
    if h < 48:  return "new"
    return "normal"

def job_id(source, raw_id):
    return int(hashlib.md5(f"{source}{raw_id}".encode()).hexdigest()[:8], 16)

def categorize(tags, title, desc=""):
    text = " ".join(list(tags) + [title, desc]).lower()
    if any(w in text for w in ["python","javascript","typescript","react","vue","angular","node","flutter","backend","frontend","devops","software","developer","engineer","data","ml","ai","cloud","aws","docker","api","fullstack","mobile","ios","android"]):
        return "tech"
    if any(w in text for w in ["design","figma","ui","ux","graphic","illustrator","photoshop","brand","logo","motion","sketch","canva","creative"]):
        return "design"
    if any(w in text for w in ["write","writing","writer","content","copywriter","editor","seo","blog","article","translation","redator","conteúdo"]):
        return "texto"
    if any(w in text for w in ["video","audio","podcast","youtube","editing","animation","3d","motion","after effects","premiere","filming"]):
        return "audio"
    if any(w in text for w in ["marketing","social media","ads","campaign","growth","email","analytics","ppc","sem"]):
        return "design"
    return "tech"

def score(job):
    text = (job.get("title","") + " " + " ".join(job.get("tags",[])) + " " + job.get("desc","")).lower()
    pts = 5
    for w in BOOST:
        if w in text: pts += 1
    for w in BLOCK:
        if w in text: pts -= 3
    if job.get("payNum", 0) > 0: pts += 1
    if job.get("urgency") == "hot": pts += 2
    elif job.get("urgency") == "new": pts += 1
    if job.get("country") == "BR": pts += 2  # prioriza vagas do Brasil no feed
    return max(0, min(10, pts))

# ─────────────────────────────────────────
# SOURCE 1 — RemoteOK (grátis, sem auth)
# ─────────────────────────────────────────
def fetch_remoteok():
    print("🔍 RemoteOK...")
    try:
        data = fetch_json("https://remoteok.com/api", headers={
            "User-Agent": "ZionJobs-Scraper/1.0",
            "Accept": "application/json"
        })
        jobs = []
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            tags     = item.get("tags") or []
            title    = item.get("position", "")
            desc     = strip_html(item.get("description", ""))[:3000]
            sal_min  = item.get("salary_min") or 0
            sal_max  = item.get("salary_max") or 0
            pay_txt  = f"${sal_min:,}–${sal_max:,}/ano" if sal_min and sal_max else "A combinar"
            date_val = item.get("epoch", 0)
            jobs.append({
                "id": job_id("remoteok", item.get("id","")), "title": title,
                "company": item.get("company", "—"),
                "category": categorize(tags, title, desc),
                "desc": desc, "contact": item.get("url", ""),
                "pay": pay_txt, "payNum": sal_min or sal_max,
                "tags": [t for t in tags[:5] if t],
                "location": "Remoto", "country": "US",
                "urgency": urgency(date_val), "criado_em": iso_of(date_val),
                "source": "RemoteOK"
            })
        print(f"   ✓ {len(jobs)} vagas")
        return jobs
    except Exception as e:
        print(f"   ✗ Erro: {e}")
        return []

# ─────────────────────────────────────────
# SOURCE 2 — Remotive (grátis, sem auth)
# ─────────────────────────────────────────
def fetch_remotive():
    print("🔍 Remotive...")
    try:
        data = fetch_json("https://remotive.com/api/remote-jobs?limit=100")
        jobs = []
        for item in data.get("jobs", []):
            tags     = item.get("tags") or []
            title    = item.get("title", "")
            desc     = strip_html(item.get("description", ""))[:3000]
            salary   = item.get("salary", "") or ""
            date_val = item.get("publication_date", "")
            location = item.get("candidate_required_location", "Global") or "Global"
            jobs.append({
                "id": job_id("remotive", item.get("id","")), "title": title,
                "company": item.get("company_name", "—"),
                "category": categorize(tags, title, desc),
                "desc": desc, "contact": item.get("url", ""),
                "pay": salary if salary else "A combinar", "payNum": 0,
                "tags": [t for t in tags[:5] if t],
                "location": "Remoto", "country": detect_country(location),
                "urgency": urgency(date_val), "criado_em": iso_of(date_val),
                "source": "Remotive"
            })
        print(f"   ✓ {len(jobs)} vagas")
        return jobs
    except Exception as e:
        print(f"   ✗ Erro: {e}")
        return []

# ─────────────────────────────────────────
# SOURCE 3 — Jooble (grátis com API key)
# ─────────────────────────────────────────
def _jooble_request(key, params):
    payload = json.dumps({**params, "page": 1}).encode()
    return fetch_json(
        f"https://jooble.org/api/{key}",
        headers={"Content-Type": "application/json"},
        data=payload,
        method="POST"
    )

def fetch_jooble():
    if not JOOBLE_KEY and not JOOBLE_KEY_BACKUP:
        print("⏭  Jooble — sem API key, pulando")
        return []
    print("🔍 Jooble...")
    try:
        searches = [
            {"keywords": "freelance remote", "location": ""},
            {"keywords": "freelancer remoto", "location": "Brasil"},
            {"keywords": "desenvolvedor freelancer", "location": "Brasil"},
        ]
        jobs = []
        seen = set()
        for params in searches:
            try:
                try:
                    data = _jooble_request(JOOBLE_KEY, params)
                except Exception as e1:
                    if JOOBLE_KEY_BACKUP:
                        print(f"   ⚠ Key principal falhou ({e1}), usando backup...")
                        data = _jooble_request(JOOBLE_KEY_BACKUP, params)
                    else:
                        raise
                for item in data.get("jobs", []):
                    jid = item.get("id", "")
                    if jid in seen: continue
                    seen.add(jid)
                    title    = item.get("title", "")
                    desc     = strip_html(item.get("snippet", ""))[:3000]
                    company  = item.get("company", "—")
                    date_val = item.get("updated", "")
                    salary   = item.get("salary", "") or ""
                    tags     = [t.strip() for t in title.split() if len(t) > 3][:5]
                    jobs.append({
                        "id": job_id("jooble", jid), "title": title, "company": company,
                        "category": categorize([], title, desc),
                        "desc": desc, "contact": item.get("link", ""),
                        "pay": salary if salary else "A combinar", "payNum": 0,
                        "tags": tags,
                        "location": item.get("location", "Remoto") or "Remoto",
                        "country": detect_country(item.get("location", "")),
                        "urgency": urgency(date_val), "criado_em": iso_of(date_val),
                        "source": "Jooble"
                    })
            except Exception as e:
                print(f"   ⚠ Jooble query '{params['keywords']}': {e}")
        print(f"   ✓ {len(jobs)} vagas")
        return jobs
    except Exception as e:
        print(f"   ✗ Erro: {e}")
        return []

# ─────────────────────────────────────────
# SOURCE 4 — Adzuna (grátis com cadastro)
# ─────────────────────────────────────────
def fetch_adzuna():
    if not ADZUNA_ID or not ADZUNA_KEY:
        print("⏭  Adzuna — sem API key, pulando")
        return []
    print("🔍 Adzuna...")
    try:
        countries = ["br", "us", "gb"]
        jobs = []
        for country in countries:
            url = (
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
                f"?app_id={ADZUNA_ID}&app_key={ADZUNA_KEY}"
                f"&results_per_page=20&what=freelance+remote&content-type=application/json"
            )
            try:
                data = fetch_json(url)
                for item in data.get("results", []):
                    title    = item.get("title", "")
                    desc     = strip_html(item.get("description", ""))[:3000]
                    company  = item.get("company", {}).get("display_name", "—")
                    date_val = item.get("created", "")
                    sal_min  = item.get("salary_min", 0) or 0
                    sal_max  = item.get("salary_max", 0) or 0
                    pay_txt  = f"${sal_min:,.0f}–${sal_max:,.0f}" if sal_min else "A combinar"
                    tags     = item.get("category", {}).get("label", "").split("/")
                    tags     = [t.strip() for t in tags if t.strip()]
                    jobs.append({
                        "id": job_id("adzuna", item.get("id","")), "title": title, "company": company,
                        "category": categorize(tags, title, desc),
                        "desc": desc, "contact": item.get("redirect_url", ""),
                        "pay": pay_txt, "payNum": sal_min,
                        "tags": tags[:5],
                        "location": item.get("location", {}).get("display_name", "Remoto") or "Remoto",
                        "country": "BR" if country == "br" else "US" if country == "us" else "EU" if country == "gb" else "global",
                        "urgency": urgency(date_val), "criado_em": iso_of(date_val),
                        "source": "Adzuna"
                    })
            except Exception as e:
                print(f"   ⚠ Adzuna/{country}: {e}")
        print(f"   ✓ {len(jobs)} vagas")
        return jobs
    except Exception as e:
        print(f"   ✗ Erro: {e}")
        return []

# ─────────────────────────────────────────
# SOURCE 5 — Freelancer.com (grátis, sem auth, global + BR)
# ─────────────────────────────────────────
def fetch_freelancer():
    print("🔍 Freelancer.com...")
    try:
        urls = [
            "https://www.freelancer.com/api/projects/0.1/projects/active/?limit=50&job_details=true&project_details=true&full_description=true&languages[]=pt",
            "https://www.freelancer.com/api/projects/0.1/projects/active/?limit=50&job_details=true&project_details=true&full_description=true&languages[]=en",
        ]
        jobs = []
        seen = set()
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read())
                projects = data.get("result", {}).get("projects", [])
                for item in projects:
                    pid = item.get("id", "")
                    if pid in seen: continue
                    seen.add(pid)
                    title     = item.get("title", "")
                    desc      = strip_html(item.get("description") or item.get("preview_description", ""))[:3000]
                    budget    = item.get("budget", {}) or {}
                    pay_min   = budget.get("minimum", 0) or 0
                    pay_max   = budget.get("maximum", 0) or 0
                    currency  = item.get("currency", {}).get("sign", "$") or "$"
                    if pay_min and pay_max:
                        pay_txt = f"{currency}{pay_min:,.0f}–{currency}{pay_max:,.0f}"
                    elif pay_min:
                        pay_txt = f"a partir de {currency}{pay_min:,.0f}"
                    else:
                        pay_txt = "A combinar"
                    jobs_list = item.get("jobs", []) or []
                    tags      = [j.get("name","") for j in jobs_list if j.get("name")][:5]
                    date_val  = item.get("time_submitted", "")
                    country   = item.get("language", "").upper() or "Global"
                    bid_stats = item.get("bid_stats") or {}
                    jobs.append({
                        "id": job_id("freelancer", pid), "title": title, "company": "Freelancer.com",
                        "category": categorize(tags, title, desc),
                        "desc": desc or "Ver descrição completa no link.",
                        "contact": f"https://www.freelancer.com/projects/{item.get('seo_url','')}",
                        "pay": pay_txt, "payNum": pay_min, "tags": tags,
                        "location": "Remoto",
                        "country": "BR" if "languages[]=pt" in url else "global",
                        "urgency": urgency(date_val), "criado_em": iso_of(date_val),
                        "source": "Freelancer",
                        "extra": {
                            "propostas": bid_stats.get("bid_count"),
                            "proposta_media": (f"{currency}{bid_stats['bid_avg']:,.0f}" if bid_stats.get("bid_avg") else None),
                            "prazo_dias": item.get("bidperiod"),
                            "tipo_pagamento": "Por hora" if item.get("type") == "hourly" else "Preço fixo",
                            "status": "Aberta" if item.get("frontend_project_status") == "open" else item.get("frontend_project_status"),
                            "id_projeto": pid
                        }
                    })
            except Exception as e:
                print(f"   ⚠ {e}")
        print(f"   ✓ {len(jobs)} vagas")
        return jobs
    except Exception as e:
        print(f"   ✗ Erro: {e}")
        return []

# ─────────────────────────────────────────
# NORMALIZAÇÃO PRO SCHEMA UNIFICADO DE VAGA (tabela `vagas` no Supabase)
# ─────────────────────────────────────────
def to_vaga(job):
    location = job.get("location") or ""
    cidade = "" if location.lower() in ("remoto", "global", "") else location
    return {
        "external_id": f"{job.get('source','scraper').lower()}-{job['id']}",
        "titulo": job["title"],
        "descricao": job["desc"],
        "tipo": "Freela",
        "modalidade": "Remoto",
        "setor": job.get("category", ""),
        "cidade": cidade,
        "estado": "",
        "salario": job.get("pay", "A combinar"),
        "cota_pcd": False,
        "cursos_necessarios": [],
        "tags": job.get("tags", []),
        "pais": job.get("country", "global"),
        "idioma": detect_lang(job["title"] + " " + job["desc"]),
        "extra": {k: v for k, v in (job.get("extra") or {}).items() if v is not None},
        "origem": "scraper",
        "fonte": job.get("source", ""),
        "link_externo": job.get("contact", ""),
        "empresa_id": None,
        "empresa_nome": job.get("company", "—"),
        "empresa_logo_url": "",
        "empresa_verificado": False,
        "criado_em": job.get("criado_em") or datetime.now(timezone.utc).isoformat()
    }

# ─────────────────────────────────────────
# SUPABASE — upsert direto na tabela `vagas` (service role, ignora RLS)
# ─────────────────────────────────────────
def _sb_request(method, path, body=None):
    headers = {
        "apikey": SB_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SB_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{SB_URL}/rest/v1/{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()

def publish_vagas_supabase(vagas_raspadas):
    if not SB_URL or not SB_SERVICE_ROLE_KEY or SB_SERVICE_ROLE_KEY == "SUA_SERVICE_ROLE_KEY_AQUI":
        print("⚠  Supabase não configurado (supabase_url / supabase_service_role_key) — nada foi publicado.")
        return False
    try:
        # Remove as vagas raspadas antigas — as de empresa (origem=empresa) não são tocadas.
        _sb_request("DELETE", "vagas?origem=eq.scraper")
        # Insere as novas em lote.
        if vagas_raspadas:
            _sb_request("POST", "vagas", vagas_raspadas)
        print(f"   ✓ {len(vagas_raspadas)} vagas raspadas publicadas em {SB_URL}")
        return True
    except Exception as e:
        print(f"   ✗ Erro publicando no Supabase: {e}")
        return False

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    print("\n" + "="*50)
    print("  Zion Jobs Scraper")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("="*50 + "\n")

    # 1. Busca todas as fontes
    all_jobs = (
        fetch_remoteok()   +
        fetch_remotive()   +
        fetch_freelancer() +
        fetch_jooble()     +
        fetch_adzuna()
    )
    print(f"\n📦 Total bruto: {len(all_jobs)} vagas")

    # 2. Deduplicação por ID
    seen, unique = set(), []
    for j in all_jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            unique.append(j)
    print(f"🔎 Únicos: {len(unique)}")

    # 3. Filtra bloqueadas
    filtered = [j for j in unique if not any(w in (j["title"] + " " + j["desc"]).lower() for w in BLOCK)]

    # 4. Score por relevância
    for j in filtered:
        j["score_ia"] = score(j)

    # 5. Ordena: score desc, depois recência
    order = {"hot": 0, "new": 1, "normal": 2}
    filtered.sort(key=lambda x: (-x["score_ia"], order.get(x["urgency"], 2)))

    # 6. Seleciona os melhores
    final = [j for j in filtered if j["score_ia"] >= SCORE_MIN][:MAX_JOBS]
    print(f"✅ {len(final)} vagas selecionadas (score ≥ {SCORE_MIN})\n")

    # 7. Normaliza pro schema unificado
    vagas_raspadas = [to_vaga(j) for j in final]

    # 8. Publica direto na tabela `vagas` do Supabase (substitui as raspadas antigas)
    print("🚀 Publicando no Supabase...")
    publish_vagas_supabase(vagas_raspadas)

    print(f"\n🎉 Concluído — {len(vagas_raspadas)} vagas raspadas\n")

if __name__ == "__main__":
    main()
