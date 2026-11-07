"""Interface web locale pour l'explorateur de fichiers.

Utilisation :
    python interface.py
        -> lance le serveur MCP en local (stdio) et ouvre le navigateur

    MCP_URL=http://127.0.0.1:8000/mcp MCP_TOKEN=ton_jeton python interface.py
        -> se connecte à un serveur MCP distant (HTTP)

L'interface écoute uniquement sur 127.0.0.1 (port INTERFACE_PORT, 8001 par
défaut) : elle n'est pas accessible depuis le réseau.
"""
import asyncio
import inspect
import json
import os
import re
import socket
import sys
import webbrowser
from collections import OrderedDict
from pathlib import Path

import ollama
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

import client as c

PORT = int(os.environ.get("INTERFACE_PORT", "8001"))
PAGE = Path(__file__).resolve().parent / "interface.html"

MAX_CONVERSATIONS = 50  # au-delà, les plus anciennes sont oubliées
APERCU_RESULTAT = 2_000  # caractères d'un résultat d'outil envoyés à la page
MAX_QUESTION = 4_000  # caractères


def decrire_outil(outil) -> dict:
    """Description d'un outil MCP pour le panneau « Outils » de la page :
    une phrase de résumé (la première de sa description), la description
    complète et la liste de ses paramètres."""
    # cleandoc retire l'indentation du code source (docstring sur plusieurs lignes).
    description = inspect.cleandoc(outil.description or "")
    texte = " ".join(description.split())
    # Première phrase : jusqu'au premier point suivi d'un espace
    # (« etc.) » ou « .pdf » ne coupent donc pas la phrase).
    resume = re.split(r"(?<=\.)\s", texte, maxsplit=1)[0] if texte else ""
    schema = outil.inputSchema or {}
    requis = set(schema.get("required", []))
    parametres = [
        {
            "nom": nom,
            "type": proprietes.get("type", ""),
            "defaut": proprietes.get("default"),
            "obligatoire": nom in requis,
        }
        for nom, proprietes in schema.get("properties", {}).items()
    ]
    return {"nom": outil.name, "resume": resume, "description": description,
            "parametres": parametres}


def creer_app(session, modele, outils_mcp) -> Starlette:
    """Crée l'application web à partir d'une session MCP déjà ouverte et
    d'un client Ollama (ou de faux objets, dans les tests)."""
    outils = c.outils_pour_ollama(outils_mcp)
    descriptions_outils = [decrire_outil(t) for t in outils_mcp]
    conversations: OrderedDict[str, list] = OrderedDict()
    en_cours: set[str] = set()  # conversations qui attendent une réponse

    def historique(conv_id: str) -> list:
        if conv_id not in conversations:
            conversations[conv_id] = [{"role": "system", "content": c.prompt_systeme()}]
            while len(conversations) > MAX_CONVERSATIONS:
                conversations.popitem(last=False)
        conversations.move_to_end(conv_id)
        return conversations[conv_id]

    async def page(request: Request):
        return FileResponse(PAGE)

    async def etat(request: Request):
        return JSONResponse({"modele": c.MODELE, "outils": descriptions_outils})

    async def message(request: Request):
        # Exiger du JSON empêche un autre site web d'envoyer des questions à
        # l'interface (un formulaire ne peut pas envoyer ce type de contenu).
        type_contenu = request.headers.get("content-type", "").split(";")[0].strip()
        if type_contenu != "application/json":
            return JSONResponse({"erreur": "Contenu JSON attendu."}, status_code=415)
        try:
            donnees = await request.json()
            question = str(donnees["question"]).strip()[:MAX_QUESTION]
            conv_id = str(donnees["conversation"])[:64]
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"erreur": "Requête invalide."}, status_code=400)
        if not question or not conv_id:
            return JSONResponse({"erreur": "Question vide."}, status_code=400)
        if conv_id in en_cours:
            return JSONResponse(
                {"erreur": "Une question est déjà en cours dans cette conversation."},
                status_code=409,
            )

        messages = historique(conv_id)
        en_cours.add(conv_id)
        file: asyncio.Queue = asyncio.Queue()

        def etape(evenement: dict) -> None:
            if evenement["type"] == "resultat":
                evenement = {**evenement, "texte": evenement["texte"][:APERCU_RESULTAT]}
            file.put_nowait(evenement)

        async def travail():
            try:
                reponse = await c.poser_question(
                    session, modele, messages, outils, question, etape
                )
                file.put_nowait({"type": "reponse", "texte": reponse or ""})
            except Exception as e:
                texte = c.message_erreur(e) or f"Erreur inattendue : {e}"
                file.put_nowait({"type": "erreur", "texte": texte})
            finally:
                en_cours.discard(conv_id)
                file.put_nowait(None)

        # La réponse continue même si la page est fermée en cours de route :
        # l'historique reste ainsi cohérent pour la question suivante.
        tache = asyncio.create_task(travail())

        async def flux():
            while (evenement := await file.get()) is not None:
                yield json.dumps(evenement, ensure_ascii=False) + "\n"
            await tache

        return StreamingResponse(flux(), media_type="application/x-ndjson")

    return Starlette(
        routes=[
            Route("/", page),
            Route("/api/etat", etat),
            Route("/api/message", message, methods=["POST"]),
        ],
        middleware=[
            # Refuse les requêtes dont l'en-tête Host n'est pas local
            # (protection contre les attaques par « DNS rebinding »).
            Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"]),
        ],
    )


def port_disponible(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def principal() -> None:
    if not port_disponible(PORT):
        raise c.ErreurClient(
            f"Le port {PORT} est déjà utilisé (l'interface tourne peut-être déjà ?). "
            "Choisis-en un autre, par exemple : INTERFACE_PORT=8002 python interface.py"
        )
    modele = ollama.AsyncClient()
    await c.verifier_ollama(modele)

    url_mcp = os.environ.get("MCP_URL", "")
    print(f"Connexion au serveur MCP {'(' + url_mcp + ')' if url_mcp else '(local)'} ...")
    async with c.connecter(url_mcp, os.environ.get("MCP_TOKEN", "")) as session:
        outils_mcp = (await session.list_tools()).tools
        app = creer_app(session, modele, outils_mcp)

        serveur = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
        )
        tache = asyncio.create_task(serveur.serve())
        while not serveur.started and not tache.done():
            await asyncio.sleep(0.05)
        if serveur.started:
            adresse = f"http://127.0.0.1:{PORT}"
            print(f"✅ Interface prête : {adresse}  (Ctrl+C pour arrêter)")
            if not os.environ.get("INTERFACE_SANS_NAVIGATEUR"):
                webbrowser.open(adresse)
        try:
            await tache
        except SystemExit:  # uvicorn n'a pas pu démarrer
            pass
        if not serveur.started:
            raise c.ErreurClient(f"Impossible de démarrer l'interface sur le port {PORT}.")


if __name__ == "__main__":
    try:
        asyncio.run(principal())
        print("\nÀ bientôt !")
    except KeyboardInterrupt:
        print("\nÀ bientôt !")
    except BaseException as erreur:
        if isinstance(erreur, SystemExit):
            raise
        if c.contient(erreur, KeyboardInterrupt):
            print("\nÀ bientôt !")
            sys.exit(0)
        texte = c.message_erreur(erreur, os.environ.get("MCP_URL", ""))
        if texte is None:
            raise
        sys.exit(f"❌ {texte}")
