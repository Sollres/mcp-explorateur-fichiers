"""Interface web : routes, protections, flux d'événements et historique.
Le modèle Ollama et la session MCP sont remplacés par des faux."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

import interface

OUTILS_MCP = [
    SimpleNamespace(name="lister_dossier", description="Liste un dossier.",
                    inputSchema={"type": "object", "properties": {"chemin": {"type": "string"}}}),
]


def appel(nom, **args):
    return SimpleNamespace(function=SimpleNamespace(name=nom, arguments=args))


def reponse_modele(texte="", appels=None):
    return SimpleNamespace(message=SimpleNamespace(
        role="assistant", content=texte, tool_calls=appels))


class FauxModele:
    """Rejoue une liste de réponses ; garde une copie des messages reçus."""

    def __init__(self, *reponses):
        self.reponses = list(reponses)
        self.recus = []
        self.attendre = None  # asyncio.Event optionnel

    async def chat(self, model, messages, tools, options):
        self.recus.append([dict(m) if isinstance(m, dict) else m for m in messages])
        if self.attendre:
            await self.attendre.wait()
        suivante = self.reponses.pop(0)
        if isinstance(suivante, BaseException):
            raise suivante
        return suivante


class FausseSession:
    def __init__(self, resultat="[fichier] a.txt", panne=None):
        self.resultat, self.panne, self.appels = resultat, panne, []

    async def call_tool(self, nom, args):
        self.appels.append((nom, args))
        if self.panne:
            raise self.panne
        return SimpleNamespace(content=[SimpleNamespace(text=self.resultat)], isError=False)


def client_web(modele, session=None):
    app = interface.creer_app(session or FausseSession(), modele, OUTILS_MCP)
    return TestClient(app, base_url="http://127.0.0.1")


def demander(web, question, conversation="c1"):
    r = web.post("/api/message", json={"question": question, "conversation": conversation})
    assert r.status_code == 200, r.text
    return [json.loads(l) for l in r.text.splitlines()]


def contenus(messages):
    return [m.get("content") if isinstance(m, dict) else m.content for m in messages]


# --- Routes et protections ---------------------------------------------
def test_page_et_etat():
    web = client_web(FauxModele())
    page = web.get("/")
    assert page.status_code == 200 and "Explorateur de fichiers" in page.text
    etat = web.get("/api/etat").json()
    assert etat["modele"] == interface.c.MODELE
    assert [o["nom"] for o in etat["outils"]] == ["lister_dossier"]


def test_decrire_outil():
    outil = SimpleNamespace(
        name="lire_fichier",
        description="Lit un fichier TEXTE (txt, md, etc.).\n    Ne pas utiliser pour les PDF.\n"
                    "    max_caracteres : 50 000 au maximum.",
        inputSchema={
            "type": "object",
            "properties": {
                "chemin": {"type": "string"},
                "max_caracteres": {"type": "integer", "default": 5000},
            },
            "required": ["chemin"],
        },
    )
    assert interface.decrire_outil(outil) == {
        "nom": "lire_fichier",
        # « etc.) » ne coupe pas la phrase ; on s'arrête au vrai point final.
        "resume": "Lit un fichier TEXTE (txt, md, etc.).",
        # Indentation du code source retirée :
        "description": "Lit un fichier TEXTE (txt, md, etc.).\nNe pas utiliser pour les PDF.\n"
                       "max_caracteres : 50 000 au maximum.",
        "parametres": [
            {"nom": "chemin", "type": "string", "defaut": None, "obligatoire": True},
            {"nom": "max_caracteres", "type": "integer", "defaut": 5000, "obligatoire": False},
        ],
    }


def test_decrire_outil_sans_description_ni_parametres():
    outil = SimpleNamespace(name="x", description=None, inputSchema=None)
    assert interface.decrire_outil(outil) == {
        "nom": "x", "resume": "", "description": "", "parametres": []}


def test_hote_non_local_refuse():
    web = client_web(FauxModele())
    assert web.get("/", headers={"Host": "site-malveillant.com"}).status_code == 400


def test_formulaire_refuse():
    """Un autre site ne peut envoyer qu'un formulaire, pas du JSON."""
    web = client_web(FauxModele())
    r = web.post("/api/message", data={"question": "x", "conversation": "c"})
    assert r.status_code == 415


@pytest.mark.parametrize("corps", [b"pas du json", b"{}", b'{"question": " ", "conversation": "c"}'])
def test_requetes_invalides(corps):
    web = client_web(FauxModele())
    r = web.post("/api/message", content=corps, headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# --- Flux d'événements -------------------------------------------------
def test_flux_outil_puis_reponse():
    modele = FauxModele(
        reponse_modele(appels=[appel("lister_dossier", chemin="Collins")]),
        reponse_modele("Voici le contenu : **a.txt**"),
    )
    session = FausseSession()
    evenements = demander(client_web(modele, session), "Que contient Collins ?")

    assert evenements == [
        {"type": "outil", "nom": "lister_dossier", "args": {"chemin": "Collins"}},
        {"type": "resultat", "nom": "lister_dossier", "texte": "[fichier] a.txt"},
        {"type": "reponse", "texte": "Voici le contenu : **a.txt**"},
    ]
    assert session.appels == [("lister_dossier", {"chemin": "Collins"})]


def test_apercu_du_resultat_tronque():
    modele = FauxModele(reponse_modele(appels=[appel("lister_dossier")]), reponse_modele("ok"))
    session = FausseSession(resultat="x" * 5_000)
    resultat = demander(client_web(modele, session), "q")[1]
    assert len(resultat["texte"]) == interface.APERCU_RESULTAT


# --- Historique --------------------------------------------------------
def test_historique_par_conversation():
    modele = FauxModele(reponse_modele("r1"), reponse_modele("r2"), reponse_modele("r3"))
    web = client_web(modele)
    demander(web, "q1", "A")
    demander(web, "q2", "A")
    demander(web, "q3", "B")

    assert contenus(modele.recus[1])[1:] == ["q1", "r1", "q2"]  # A se souvient
    assert contenus(modele.recus[2])[1:] == ["q3"]  # B repart de zéro
    assert "Nous sommes le" in contenus(modele.recus[2])[0]  # prompt système


def test_erreur_ollama_puis_reprise():
    modele = FauxModele(ConnectionError("down"), reponse_modele("r2"))
    web = client_web(modele)

    evenements = demander(web, "q1")
    assert evenements[-1]["type"] == "erreur"
    assert "Ollama ne répond plus" in evenements[-1]["texte"]

    demander(web, "q2")
    assert contenus(modele.recus[1])[1:] == ["q2"]  # q1 a été retirée


def test_erreur_inattendue_historique_coherent():
    """Si l'outil plante, l'appel d'outil sans résultat ne reste pas dans
    l'historique (sinon Ollama refuserait les questions suivantes)."""
    modele = FauxModele(reponse_modele(appels=[appel("lister_dossier")]), reponse_modele("r2"))
    web = client_web(modele, FausseSession(panne=RuntimeError("connexion perdue")))

    evenements = demander(web, "q1")
    assert evenements[-1] == {"type": "erreur", "texte": "Erreur inattendue : connexion perdue"}
    demander(web, "q2")
    assert contenus(modele.recus[1])[1:] == ["q2"]


def test_conversations_les_plus_anciennes_oubliees(monkeypatch):
    monkeypatch.setattr(interface, "MAX_CONVERSATIONS", 2)
    modele = FauxModele(*[reponse_modele(f"r{i}") for i in range(4)])
    web = client_web(modele)
    demander(web, "a1", "A")
    demander(web, "b1", "B")
    demander(web, "c1", "C")  # A est oubliée
    demander(web, "a2", "A")
    assert contenus(modele.recus[3])[1:] == ["a2"]


# --- Une seule question à la fois par conversation ---------------------
@pytest.mark.anyio
async def test_question_deja_en_cours():
    modele = FauxModele(reponse_modele("r1"))
    modele.attendre = asyncio.Event()
    app = interface.creer_app(FausseSession(), modele, OUTILS_MCP)
    transport = httpx.ASGITransport(app=app)
    corps = {"question": "q", "conversation": "A"}

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as web:
        async def seconde():
            await asyncio.sleep(0.1)
            r = await web.post("/api/message", json=corps)
            modele.attendre.set()
            return r

        r1, r2 = await asyncio.gather(web.post("/api/message", json=corps), seconde())

    assert r2.status_code == 409
    assert json.loads(r1.text.splitlines()[-1]) == {"type": "reponse", "texte": "r1"}
