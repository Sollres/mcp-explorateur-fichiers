"""Client MCP pour Ollama.

Utilisation :
    python client.py                          -> serveur local (stdio)
    python client.py http://127.0.0.1:8000/mcp  -> serveur distant (HTTP)

En mode HTTP, le jeton est lu dans la variable d'environnement MCP_TOKEN.
Le modèle peut être changé avec OLLAMA_MODELE (défaut : qwen2.5:7b).
"""
import asyncio
import os
import sys
from datetime import datetime

import httpx
import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

MODELE = os.environ.get("OLLAMA_MODELE", "qwen2.5:7b")

# Fenêtre de contexte demandée à Ollama (en tokens). Sans ce réglage,
# Ollama utilise une petite fenêtre par défaut et "oublie" vite.
TAILLE_CONTEXTE = 8192
# Budget de l'historique, en caractères (~1 token pour 3-4 caractères).
# On garde de la place pour les définitions d'outils et la réponse.
MAX_CARACTERES_HISTORIQUE = 20_000
# Un résultat d'outil plus long est tronqué avant d'être donné au modèle.
MAX_CARACTERES_RESULTAT = 8_000
# Évite qu'un modèle qui boucle appelle des outils indéfiniment.
MAX_APPELS_PAR_QUESTION = 8

PROMPT_SYSTEME = (
    "Tu es un assistant qui aide à retrouver des fichiers. "
    "Utilise les outils disponibles pour explorer les dossiers. "
    "Les chemins sont relatifs au dossier Documents. "
    "Réponds en français."
)
JOURS_SEMAINE = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def prompt_systeme(maintenant: datetime | None = None) -> str:
    """Prompt système avec la date du jour : sans elle, le modèle ne peut pas
    traduire « la semaine dernière » en nombre de jours pour fichiers_recents."""
    maintenant = maintenant or datetime.now()
    jour = JOURS_SEMAINE[maintenant.weekday()]
    return f"{PROMPT_SYSTEME} Nous sommes le {jour} {maintenant:%Y-%m-%d}."


class ErreurClient(Exception):
    """Erreur dont le message est destiné directement à l'utilisateur."""


# ----------------------------------------------------------------------
# Historique de conversation
# ----------------------------------------------------------------------
def _champ(message, nom):
    """Lit un champ d'un message, qu'il soit un dict ou un objet ollama."""
    if isinstance(message, dict):
        return message.get(nom)
    return getattr(message, nom, None)


def taille_message(message) -> int:
    contenu = _champ(message, "content") or ""
    appels = _champ(message, "tool_calls") or ""
    return len(contenu) + len(str(appels))


def tailler_historique(messages: list, max_caracteres: int) -> list:
    """Supprime les échanges les plus anciens quand l'historique dépasse
    `max_caracteres`. messages[0] est le prompt système, toujours conservé.

    Un « tour » = une question de l'utilisateur et tout ce qui suit
    (réponses, appels d'outils, résultats) jusqu'à la question suivante.
    On supprime des tours entiers pour ne jamais séparer un appel d'outil
    de son résultat. Le dernier tour est toujours conservé."""
    systeme, reste = messages[:1], messages[1:]
    debuts = [i for i, m in enumerate(reste) if _champ(m, "role") == "user"]
    if not debuts:
        return messages
    tours = [reste[a:b] for a, b in zip(debuts, debuts[1:] + [len(reste)])]

    total = sum(taille_message(m) for tour in tours for m in tour)
    while len(tours) > 1 and total > max_caracteres:
        total -= sum(taille_message(m) for m in tours.pop(0))
    return systeme + [m for tour in tours for m in tour]


# ----------------------------------------------------------------------
# Erreurs lisibles
# ----------------------------------------------------------------------
def _feuilles(erreur: BaseException):
    """Déplie les ExceptionGroup (levées par les transports MCP)."""
    if isinstance(erreur, BaseExceptionGroup):
        for sous in erreur.exceptions:
            yield from _feuilles(sous)
    else:
        yield erreur


def contient(erreur: BaseException, type_) -> bool:
    return any(isinstance(f, type_) for f in _feuilles(erreur))


def message_erreur(erreur: BaseException, url: str = "") -> str | None:
    """Traduit une erreur technique en message compréhensible.
    Renvoie None si l'erreur n'est pas reconnue."""
    cible = url or "le serveur MCP"
    for f in _feuilles(erreur):
        if isinstance(f, ErreurClient):
            return str(f)
        if isinstance(f, httpx.HTTPStatusError):
            code = f.response.status_code
            if code == 401:
                return "Jeton refusé par le serveur (401). Vérifie la variable MCP_TOKEN."
            return f"Le serveur MCP a répondu avec l'erreur HTTP {code}."
        if isinstance(f, httpx.TimeoutException):
            return f"{cible} ne répond pas (délai dépassé)."
        if isinstance(f, (httpx.ConnectError, ConnectionError)):
            return f"Impossible de joindre {cible}. Le serveur est-il lancé ?"
        if isinstance(f, McpError):
            return f"Erreur du serveur MCP : {f}"
    return None


async def verifier_ollama(client) -> None:
    """Vérifie qu'Ollama répond et que le modèle est installé."""
    try:
        reponse = await client.list()
    except (ConnectionError, httpx.HTTPError):
        raise ErreurClient(
            "Ollama ne répond pas. Ouvre l'application Ollama ou lance `ollama serve`."
        )
    installes = {m.model for m in reponse.models}
    if MODELE not in installes:
        raise ErreurClient(
            f"Le modèle {MODELE} n'est pas installé. Lance : ollama pull {MODELE}"
        )


# ----------------------------------------------------------------------
# Conversation
# ----------------------------------------------------------------------
def texte_de(resultat) -> str:
    texte = "\n".join(c.text for c in resultat.content if hasattr(c, "text"))
    return f"Erreur : {texte}" if resultat.isError else texte


async def appeler_outil(session: ClientSession, nom: str, args: dict) -> str:
    """Appelle un outil MCP. Une erreur de l'outil est renvoyée comme texte
    (le modèle peut alors corriger sa demande) au lieu de tout arrêter."""
    try:
        texte = texte_de(await session.call_tool(nom, args))
    except McpError as e:
        texte = f"Erreur lors de l'appel de l'outil {nom} : {e}"
    if len(texte) > MAX_CARACTERES_RESULTAT:
        texte = texte[:MAX_CARACTERES_RESULTAT] + "\n[... résultat tronqué]"
    return texte


async def demander_modele(client, messages: list, outils: list | None):
    """Appelle Ollama. Transforme les erreurs connues en ErreurClient."""
    try:
        reponse = await client.chat(
            model=MODELE,
            messages=messages,
            tools=outils,
            options={"num_ctx": TAILLE_CONTEXTE},
        )
    except (ConnectionError, httpx.HTTPError):
        raise ErreurClient("Ollama ne répond plus. Est-il toujours lancé ?")
    except ollama.ResponseError as e:
        raise ErreurClient(f"Erreur d'Ollama : {e.error}")
    return reponse.message


async def repondre(session: ClientSession, client, messages: list, outils: list) -> str:
    """Fait tourner le modèle (et ses appels d'outils) jusqu'à une réponse."""
    for _ in range(MAX_APPELS_PAR_QUESTION):
        msg = await demander_modele(client, messages, outils)
        messages.append(msg)
        if not msg.tool_calls:
            return msg.content

        for appel in msg.tool_calls:
            nom = appel.function.name
            args = dict(appel.function.arguments)
            print(f"   [outil] {nom}({args})")
            texte = await appeler_outil(session, nom, args)
            messages.append({"role": "tool", "content": texte, "tool_name": nom})

    # Trop d'appels : on force une réponse finale, sans outils.
    print("   (limite d'appels d'outils atteinte, réponse avec ce qui a été trouvé)")
    msg = await demander_modele(client, messages, None)
    messages.append(msg)
    return msg.content


async def boucle_chat(session: ClientSession, client) -> None:
    """Conversation avec le modèle, identique quel que soit le transport."""
    await session.initialize()

    outils_mcp = (await session.list_tools()).tools
    outils = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in outils_mcp
    ]
    print("Outils chargés :", [t.name for t in outils_mcp])
    print(f"Modèle : {MODELE}. Tape « quit » ou Ctrl+D pour sortir.")

    messages = [{"role": "system", "content": prompt_systeme()}]

    while True:
        try:
            question = input("\nToi > ").strip()
        except EOFError:  # Ctrl+D
            print()
            break
        if not question:
            continue
        if question.lower() in ("quit", "exit"):
            break

        # Commande directe, sans passer par le modèle : /lister Collins
        if question.startswith("/lister"):
            chemin = question[len("/lister"):].strip()
            print(await appeler_outil(session, "lister_dossier", {"chemin": chemin}))
            continue

        message_question = {"role": "user", "content": question}
        messages.append(message_question)
        messages[:] = tailler_historique(messages, MAX_CARACTERES_HISTORIQUE)
        try:
            reponse = await repondre(session, client, messages, outils)
        except ErreurClient as e:
            # Question annulée : on retire la question et tout ce qui a suivi.
            i = next(i for i, m in enumerate(messages) if m is message_question)
            del messages[i:]
            print(f"\n⚠️  {e}")
            continue
        print(f"\nIA > {reponse}")


async def main() -> None:
    client = ollama.AsyncClient()
    await verifier_ollama(client)

    if len(sys.argv) > 1:
        # --- Mode HTTP : serveur distant ---
        url = sys.argv[1]
        jeton = os.environ.get("MCP_TOKEN", "")
        if not jeton:
            raise ErreurClient('Définis le jeton : export MCP_TOKEN="ton_jeton"')
        http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {jeton}"},
            # Délais par défaut des transports MCP : 30 s, et 5 min en lecture
            # car le serveur peut garder un flux de réponse ouvert.
            timeout=httpx.Timeout(30, read=300),
        )
        print(f"Connexion à {url} ...")
        async with http, streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await boucle_chat(session, client)
    else:
        # --- Mode stdio : serveur local lancé par le client ---
        params = StdioServerParameters(command=sys.executable, args=["server.py"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await boucle_chat(session, client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nÀ bientôt !")
    except BaseException as erreur:
        if isinstance(erreur, SystemExit):
            raise
        if contient(erreur, KeyboardInterrupt):
            print("\nÀ bientôt !")
            sys.exit(0)
        message = message_erreur(erreur, sys.argv[1] if len(sys.argv) > 1 else "")
        if message is None:
            raise  # erreur inconnue : on garde la trace complète pour déboguer
        sys.exit(f"❌ {message}")
