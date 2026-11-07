"""Client : historique de conversation et messages d'erreur."""
from types import SimpleNamespace

import httpx
import pytest

import client
from client import ErreurClient, message_erreur, tailler_historique

SYSTEME = {"role": "system", "content": "prompt"}


def tour(question, taille_resultat=0):
    """Un échange complet : question, appel d'outil, résultat, réponse."""
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": "", "tool_calls": [{"f": "lister"}]},
        {"role": "tool", "content": "r" * taille_resultat, "tool_name": "lister"},
        {"role": "assistant", "content": f"réponse à {question}"},
    ]


# --- Historique --------------------------------------------------------
def test_historique_court_inchange():
    messages = [SYSTEME, *tour("q1"), *tour("q2")]
    assert tailler_historique(messages, 10_000) == messages


def test_historique_supprime_les_tours_les_plus_anciens():
    messages = [SYSTEME, *tour("q1", 500), *tour("q2", 500), *tour("q3", 500)]
    resultat = tailler_historique(messages, 1_200)
    assert resultat == [SYSTEME, *tour("q2", 500), *tour("q3", 500)]


def test_historique_garde_toujours_le_dernier_tour():
    messages = [SYSTEME, *tour("q1"), *tour("q2", 50_000)]
    assert tailler_historique(messages, 1_000) == [SYSTEME, *tour("q2", 50_000)]


def test_historique_ne_coupe_jamais_un_tour():
    messages = [SYSTEME, *tour("q1", 300), *tour("q2", 300), *tour("q3", 300)]
    resultat = tailler_historique(messages, 700)
    # Chaque appel d'outil reste suivi de son résultat.
    roles = [m["role"] for m in resultat]
    assert roles[0] == "system"
    assert roles[1:] == ["user", "assistant", "tool", "assistant"] * (len(roles[1:]) // 4)


def test_historique_accepte_les_messages_ollama():
    """Les réponses du modèle sont des objets, pas des dict."""
    objet = SimpleNamespace(role="assistant", content="x" * 1_000, tool_calls=None)
    messages = [SYSTEME, {"role": "user", "content": "q1"}, objet,
                {"role": "user", "content": "q2"}]
    assert tailler_historique(messages, 100) == [SYSTEME, {"role": "user", "content": "q2"}]


def test_prompt_systeme_contient_la_date():
    from datetime import datetime
    prompt = client.prompt_systeme(datetime(2026, 11, 7, 15, 30))
    assert prompt.startswith(client.PROMPT_SYSTEME)
    assert prompt.endswith("Nous sommes le samedi 2026-11-07.")


# --- Messages d'erreur -------------------------------------------------
def erreur_http(code):
    requete = httpx.Request("POST", "http://x/mcp")
    return httpx.HTTPStatusError("x", request=requete, response=httpx.Response(code, request=requete))


@pytest.mark.parametrize(
    "erreur,attendu",
    [
        (ExceptionGroup("g", [erreur_http(401)]), "Jeton refusé"),
        (ExceptionGroup("g", [erreur_http(500)]), "erreur HTTP 500"),
        (ExceptionGroup("g", [httpx.ConnectError("refus")]), "Impossible de joindre http://srv/mcp"),
        (ExceptionGroup("g", [ExceptionGroup("g2", [httpx.ReadTimeout("t")])]), "délai dépassé"),
        (ErreurClient("message direct"), "message direct"),
    ],
)
def test_message_erreur(erreur, attendu):
    assert attendu in message_erreur(erreur, "http://srv/mcp")


def test_message_erreur_inconnue():
    assert message_erreur(ExceptionGroup("g", [ZeroDivisionError()])) is None


def test_contient_keyboard_interrupt():
    erreur = BaseExceptionGroup("g", [KeyboardInterrupt()])
    assert client.contient(erreur, KeyboardInterrupt)


# --- Vérification d'Ollama ---------------------------------------------
class FauxOllama:
    def __init__(self, modeles=None, panne=False):
        self.modeles, self.panne = modeles or [], panne

    async def list(self):
        if self.panne:
            raise ConnectionError("Failed to connect to Ollama")
        return SimpleNamespace(models=[SimpleNamespace(model=m) for m in self.modeles])


@pytest.mark.anyio
async def test_ollama_arrete():
    with pytest.raises(ErreurClient, match="Ollama ne répond pas"):
        await client.verifier_ollama(FauxOllama(panne=True))


@pytest.mark.anyio
async def test_modele_absent():
    with pytest.raises(ErreurClient, match=f"ollama pull {client.MODELE}"):
        await client.verifier_ollama(FauxOllama(modeles=["llama3.1:8b"]))


@pytest.mark.anyio
async def test_modele_present():
    await client.verifier_ollama(FauxOllama(modeles=[client.MODELE]))
