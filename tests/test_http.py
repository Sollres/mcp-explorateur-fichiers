"""Mode HTTP : jetons, middleware d'authentification, et un test de bout en bout."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server
from conftest import lire_journal

PROJET = Path(__file__).resolve().parent.parent


# --- Lecture des jetons ------------------------------------------------
def test_charger_jetons(monkeypatch):
    monkeypatch.setenv("MCP_TOKENS", " alice : j1 ,bob:j2:avec:deux-points,,invalide, :vide")
    assert server.charger_jetons() == {"alice": "j1", "bob": "j2:avec:deux-points"}


def test_charger_jetons_vide(monkeypatch):
    monkeypatch.delenv("MCP_TOKENS", raising=False)
    assert server.charger_jetons() == {}


# --- Middleware --------------------------------------------------------
@pytest.fixture
def client_http():
    """Mini application protégée par AuthMiddleware, qui renvoie le nom
    associé au jeton (tel que les outils le verront)."""

    async def qui(request):
        return PlainTextResponse(request.scope["state"]["utilisateur_mcp"])

    app = Starlette(routes=[Route("/mcp", qui, methods=["POST"])])
    app.add_middleware(server.AuthMiddleware, jetons={"alice": "jeton-a", "bob": "jeton-b"})
    return TestClient(app)


def test_sans_jeton_refuse(client_http):
    r = client_http.post("/mcp")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert lire_journal()[-1]["evenement"] == "auth_refusee"


@pytest.mark.parametrize("entete", ["Bearer faux", "Bearer ", "Basic jeton-a", "jeton-a"])
def test_mauvais_jeton_refuse(client_http, entete):
    r = client_http.post("/mcp", headers={"Authorization": entete})
    assert r.status_code == 401


@pytest.mark.parametrize("jeton,nom", [("jeton-a", "alice"), ("jeton-b", "bob")])
def test_bon_jeton_accepte_et_identifie(client_http, jeton, nom):
    r = client_http.post("/mcp", headers={"Authorization": f"bearer {jeton}"})
    assert r.status_code == 200
    assert r.text == nom


# --- Bout en bout : vrai serveur HTTP + vrai client MCP ----------------
def port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serveur_http(tmp_path):
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "bonjour.txt").write_text("salut", encoding="utf-8")
    logs = tmp_path / "logs"
    port = port_libre()
    env = {
        **os.environ,
        "HOME": str(tmp_path),  # la racine devient tmp_path/Documents
        "MCP_LOGS_DIR": str(logs),
        "MCP_TRANSPORT": "http",
        "MCP_PORT": str(port),
        "MCP_TOKENS": "alice:jeton-alice",
    }
    processus = subprocess.Popen(
        [sys.executable, str(PROJET / "server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(100):  # jusqu'à 10 s pour démarrer
        try:
            httpx.post(url, timeout=0.2)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        processus.kill()
        pytest.fail("Le serveur HTTP n'a pas démarré.")
    yield url, logs / "audit.jsonl"
    processus.terminate()
    processus.wait(timeout=5)


@pytest.mark.lent
@pytest.mark.anyio
async def test_bout_en_bout(serveur_http):
    url, journal = serveur_http
    http = httpx.AsyncClient(headers={"Authorization": "Bearer jeton-alice"})
    async with http, streamable_http_client(url, http_client=http) as (lecture, ecriture, _):
        async with ClientSession(lecture, ecriture) as session:
            await session.initialize()
            outils = {t.name for t in (await session.list_tools()).tools}
            assert "rechercher_dans_contenu" in outils
            resultat = await session.call_tool("lister_dossier", {"chemin": ""})
            assert resultat.content[0].text == "[fichier] bonjour.txt"

    # Mauvais jeton : refusé avant même d'atteindre MCP.
    r = httpx.post(url, headers={"Authorization": "Bearer faux"})
    assert r.status_code == 401

    lignes = journal.read_text(encoding="utf-8")
    assert '"utilisateur_mcp": "alice", "outil": "lister_dossier"' in lignes
    assert '"evenement": "auth_refusee"' in lignes
