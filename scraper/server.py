"""
Zion Jobs — Servidor Local do Admin
Roda em http://localhost:8765. O admin.html usa este servidor para:
- disparar o scraper e acompanhar o log
- revisar/aprovar empresas cadastradas (fila local, não pública)
- gerenciar vagas de empresa e publicar data/empresas.json + data/vagas.json no GitHub

Portado do GetFreelas (facincanitech/GetFreelas), com os endpoints de
aprovação de empresa e publicação de vagas que o Zion Jobs precisa.
"""

import json
import subprocess
import sys
import threading
import base64
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR        = Path(__file__).parent
DATA_DIR        = BASE_DIR.parent / "data"
LOG_FILE        = BASE_DIR / "last_run.json"
EMPRESAS_LOCAL  = DATA_DIR / "empresas_local.json"
EMPRESAS_PUB    = DATA_DIR / "empresas.json"
VAGAS_PUB       = DATA_DIR / "vagas.json"
CFG_FILE        = BASE_DIR / "config.json"
PORT            = 8765

with open(CFG_FILE, encoding="utf-8") as f:
    CFG = json.load(f)
GH_TOKEN  = CFG.get("github_token", "")
GH_REPO   = CFG.get("github_repo", "")
GH_BRANCH = CFG.get("github_branch", "main")

state = {"running": False, "last_run": None, "last_status": "nunca executado", "last_log": "", "last_jobs": 0}
if LOG_FILE.exists():
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    except Exception:
        pass

def save_state():
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in state.items() if k != "running"}, f, ensure_ascii=False, indent=2)

def run_scraper():
    if state["running"]:
        return
    state["running"] = True
    state["last_log"] = "Iniciando scraper...\n"
    try:
        script = BASE_DIR / "scraper.py"
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(BASE_DIR)
        )
        log = ""
        for line in proc.stdout:
            log += line
            state["last_log"] = log
        proc.wait()
        jobs_match = [l for l in log.splitlines() if "vagas raspadas" in l or "vagas selecionadas" in l]
        jobs_count = 0
        if jobs_match:
            import re
            m = re.search(r'(\d+)', jobs_match[-1])
            if m: jobs_count = int(m.group(1))
        state["last_run"]    = datetime.now().strftime("%d/%m/%Y %H:%M")
        state["last_jobs"]   = jobs_count
        state["last_status"] = "ok" if proc.returncode == 0 else "erro"
        state["last_log"]    = log
    except Exception as e:
        state["last_status"] = "erro"
        state["last_log"]    = str(e)
    finally:
        state["running"] = False
        save_state()

# ─────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────
def read_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, data):
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def github_publish(path, payload, message):
    if not GH_TOKEN or GH_TOKEN == "SEU_TOKEN_AQUI":
        return False, "Token GitHub não configurado"
    content = base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")).decode()
    api = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "ZionJobs-Admin/1.0"
    }
    sha = None
    try:
        req = urllib.request.Request(f"{api}?ref={GH_BRANCH}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            sha = json.loads(r.read()).get("sha")
    except Exception:
        pass
    body = {"message": message, "content": content, "branch": GH_BRANCH, **({"sha": sha} if sha else {})}
    req = urllib.request.Request(api, data=json.dumps(body).encode(), headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def publish_empresas_e_vagas():
    empresas = read_json(EMPRESAS_LOCAL, [])
    verificadas = [e for e in empresas if e.get("status") == "verificado"]
    ok1, msg1 = github_publish(
        "data/empresas.json",
        {"updated_at": datetime.now(timezone.utc).isoformat(), "empresas": verificadas},
        f"chore: atualiza empresas.json — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    vagas_doc = read_json(VAGAS_PUB, {"vagas": []})
    ok2, msg2 = github_publish(
        "data/vagas.json", vagas_doc,
        f"chore: atualiza vagas.json — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    return ok1 and ok2, f"empresas: {msg1} | vagas: {msg2}"

# ─────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors()
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_POST(self):
        try:
            if self.path == "/register":
                data = self.read_body()
                empresas = read_json(EMPRESAS_LOCAL, [])
                empresas = [e for e in empresas if e.get("email") != data.get("email")]
                data["id"] = data.get("id") or str(uuid.uuid4())[:8]
                data["status"] = "pendente"
                data["receivedAt"] = datetime.now().isoformat()
                empresas.append(data)
                write_json(EMPRESAS_LOCAL, empresas)
                self.send_json(200, {"ok": True})

            elif self.path == "/approve":
                data = self.read_body()
                empresas = read_json(EMPRESAS_LOCAL, [])
                dias = 365 if data.get("plano") == "anual" else 30
                found = False
                for e in empresas:
                    if e.get("id") == data.get("id"):
                        e["status"] = "verificado"
                        e["plano"] = data.get("plano", "mensal")
                        e["expira_em"] = (datetime.now(timezone.utc) + timedelta(days=dias)).isoformat()
                        found = True
                if not found:
                    self.send_json(404, {"ok": False, "error": "empresa não encontrada"})
                    return
                write_json(EMPRESAS_LOCAL, empresas)
                ok, msg = publish_empresas_e_vagas()
                self.send_json(200, {"ok": True, "published": ok, "msg": msg})

            elif self.path == "/reject":
                data = self.read_body()
                empresas = read_json(EMPRESAS_LOCAL, [])
                for e in empresas:
                    if e.get("id") == data.get("id"):
                        e["status"] = "rejeitado"
                write_json(EMPRESAS_LOCAL, empresas)
                self.send_json(200, {"ok": True})

            elif self.path == "/vaga/add":
                data = self.read_body()
                vagas_doc = read_json(VAGAS_PUB, {"vagas": []})
                vaga = {
                    "id": f"empresa-{str(uuid.uuid4())[:8]}",
                    "titulo": data.get("titulo", ""),
                    "descricao": data.get("descricao", ""),
                    "tipo": data.get("tipo", "CLT"),
                    "modalidade": data.get("modalidade", "Remoto"),
                    "setor": data.get("setor", ""),
                    "cidade": data.get("cidade", ""),
                    "estado": data.get("estado", ""),
                    "salario": data.get("salario", "A combinar"),
                    "cota_pcd": bool(data.get("cota_pcd", False)),
                    "cursos_necessarios": data.get("cursos_necessarios", []),
                    "tags": [],
                    "origem": "empresa",
                    "fonte": "",
                    "link_externo": "",
                    "empresa": data.get("empresa", {}),
                    "criado_em": datetime.now(timezone.utc).isoformat()
                }
                vagas_doc.setdefault("vagas", []).append(vaga)
                vagas_doc["total"] = len(vagas_doc["vagas"])
                vagas_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
                write_json(VAGAS_PUB, vagas_doc)
                ok, msg = publish_empresas_e_vagas()
                self.send_json(200, {"ok": True, "published": ok, "msg": msg, "vaga": vaga})

            elif self.path == "/vaga/delete":
                data = self.read_body()
                vagas_doc = read_json(VAGAS_PUB, {"vagas": []})
                vagas_doc["vagas"] = [v for v in vagas_doc.get("vagas", []) if v.get("id") != data.get("id")]
                vagas_doc["total"] = len(vagas_doc["vagas"])
                vagas_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
                write_json(VAGAS_PUB, vagas_doc)
                ok, msg = publish_empresas_e_vagas()
                self.send_json(200, {"ok": True, "published": ok, "msg": msg})

            elif self.path == "/publish":
                ok, msg = publish_empresas_e_vagas()
                self.send_json(200, {"ok": ok, "msg": msg})

            else:
                self.send_json(404, {"error": "not found"})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": str(e)})

    def do_GET(self):
        if self.path == "/empresas":
            self.send_json(200, {"empresas": read_json(EMPRESAS_LOCAL, [])})

        elif self.path == "/vagas":
            self.send_json(200, read_json(VAGAS_PUB, {"vagas": []}))

        elif self.path == "/status":
            self.send_json(200, {
                "running": state["running"], "last_run": state["last_run"],
                "last_status": state["last_status"], "last_jobs": state["last_jobs"],
                "last_log": state["last_log"][-3000:] if state["last_log"] else ""
            })

        elif self.path == "/scrape":
            if state["running"]:
                self.send_json(200, {"ok": False, "msg": "Scraper já está rodando..."})
                return
            threading.Thread(target=run_scraper, daemon=True).start()
            self.send_json(200, {"ok": True, "msg": "Scraper iniciado!"})

        elif self.path == "/ping":
            self.send_json(200, {"ok": True})

        else:
            self.send_json(404, {"error": "not found"})

def main():
    print(f"⚡ Zion Jobs — servidor admin em http://localhost:{PORT}")
    print("   Mantenha esta janela aberta enquanto usa o admin.html.\n")
    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")

if __name__ == "__main__":
    main()
