"""Client MCP pour Ollama.

Utilisation :
    python client.py                          -> serveur local (stdio)
    python client.py http://127.0.0.1:8000/mcp  -> serveur distant (HTTP)

En mode HTTP, le jeton est lu dans la variable d'environnement MCP_TOKEN.
"""
import asyncio
import os
import sys

import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

MODELE = "qwen2.5:7b"


def texte_de(resultat) -> str:
    return "\n".join(c.text for c in resultat.content if hasattr(c, "text"))


async def boucle_chat(session: ClientSession) -> None:
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

    client = ollama.AsyncClient()
    messages = [
        {
            "role": "system",
            "content": "Tu es un assistant qui aide à retrouver des fichiers. "
                       "Utilise les outils disponibles pour explorer les dossiers. "
                       "Les chemins sont relatifs au dossier Documents. "
                       "Réponds en français.",
        }
    ]

    while True:
        question = input("\nToi > ")
        if question.strip().lower() in ("quit", "exit"):
            break

        # Commande directe, sans passer par le modèle : /lister Collins
        if question.startswith("/lister"):
            chemin = question[len("/lister"):].strip()
            resultat = await session.call_tool("lister_dossier", {"chemin": chemin})
            print(texte_de(resultat))
            continue

        messages.append({"role": "user", "content": question})

        while True:
            reponse = await client.chat(model=MODELE, messages=messages, tools=outils)
            msg = reponse.message
            messages.append(msg)

            if not msg.tool_calls:
                print(f"\nIA > {msg.content}")
                break

            for appel in msg.tool_calls:
                nom = appel.function.name
                args = dict(appel.function.arguments)
                print(f"   [outil] {nom}({args})")

                resultat = await session.call_tool(nom, args)
                messages.append(
                    {"role": "tool", "content": texte_de(resultat), "tool_name": nom}
                )


async def main() -> None:
    if len(sys.argv) > 1:
        # --- Mode HTTP : serveur distant ---
        url = sys.argv[1]
        jeton = os.environ.get("MCP_TOKEN", "")
        if not jeton:
            sys.exit("Définis le jeton : export MCP_TOKEN=\"ton_jeton\"")
        entetes = {"Authorization": f"Bearer {jeton}"}
        print(f"Connexion à {url} ...")
        async with streamablehttp_client(url, headers=entetes) as (read, write, _):
            async with ClientSession(read, write) as session:
                await boucle_chat(session)
    else:
        # --- Mode stdio : serveur local lancé par le client ---
        params = StdioServerParameters(command=sys.executable, args=["server.py"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await boucle_chat(session)


asyncio.run(main())