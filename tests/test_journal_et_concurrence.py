"""Décorateur journaliser : journal d'audit et exécution dans un thread."""
import time

import anyio
import pytest

import server
from conftest import lire_journal

pytestmark = pytest.mark.anyio


@server.journaliser
def outil_lent(duree: float) -> str:
    time.sleep(duree)
    return "fini"


@server.journaliser
def outil_qui_plante(texte: str) -> str:
    raise RuntimeError("panne")


async def test_journal_appel_reussi(racine):
    (racine / "x.txt").write_text("contenu secret", encoding="utf-8")
    await server.lire_fichier(chemin="x.txt")

    entree = lire_journal()[-1]
    assert entree["outil"] == "lire_fichier"
    assert entree["statut"] == "ok"
    assert entree["arguments"] == {"chemin": "x.txt"}
    assert entree["taille_resultat"] == len("contenu secret")
    assert "utilisateur_mcp" not in entree  # hors HTTP
    # Le contenu lu n'est jamais écrit dans le journal.
    assert "contenu secret" not in str(entree)


async def test_journal_tronque_les_longs_arguments():
    with pytest.raises(RuntimeError):
        await outil_qui_plante(texte="z" * 1_000)
    entree = lire_journal()[-1]
    assert entree["statut"] == "erreur"
    assert entree["erreur"] == "panne"
    assert len(entree["arguments"]["texte"]) == 200


async def test_outils_executes_en_parallele():
    """Deux appels lents de 0,3 s en même temps doivent prendre ~0,3 s, pas 0,6 s."""
    debut = time.perf_counter()
    async with anyio.create_task_group() as groupe:
        groupe.start_soon(outil_lent, 0.3)
        groupe.start_soon(outil_lent, 0.3)
    assert time.perf_counter() - debut < 0.5


async def test_boucle_asynchrone_non_bloquee():
    """Pendant un outil lent, les autres tâches continuent de tourner."""
    ticks = []

    async def horloge():
        for _ in range(5):
            ticks.append(time.perf_counter())
            await anyio.sleep(0.05)

    async with anyio.create_task_group() as groupe:
        groupe.start_soon(outil_lent, 0.4)
        groupe.start_soon(horloge)
    assert len(ticks) == 5
    assert ticks[-1] - ticks[0] < 0.35  # l'horloge n'a pas attendu l'outil
