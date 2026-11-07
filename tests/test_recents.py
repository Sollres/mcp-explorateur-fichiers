"""Outil fichiers_recents : filtre par date et taille, tri du plus récent."""
import os
import time
from datetime import datetime

import pytest

import server

pytestmark = pytest.mark.anyio

JOUR = 86_400


def creer(racine, nom, il_y_a_jours, taille=10):
    """Crée un fichier modifié il y a `il_y_a_jours` jours."""
    fichier = racine / nom
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_bytes(b"x" * taille)
    date = time.time() - il_y_a_jours * JOUR
    os.utime(fichier, (date, date))
    return fichier


def chemins(resultat: str) -> list[str]:
    """Chemins listés, dans l'ordre (sans la ligne de bilan)."""
    return [l.split("  ")[-1] for l in resultat.splitlines() if not l.startswith("(")]


async def test_tri_et_limite_de_jours(racine):
    creer(racine, "ancien.pdf", 10)
    creer(racine, "factures/mardi.pdf", 3)
    creer(racine, "hier.txt", 1)

    resultat = await server.fichiers_recents(jours=7)
    assert chemins(resultat) == ["hier.txt", "factures/mardi.pdf"]
    assert "(2 fichier(s) trouvé(s), 2 affiché(s))" in resultat

    resultat = await server.fichiers_recents(jours=30)
    assert chemins(resultat) == ["hier.txt", "factures/mardi.pdf", "ancien.pdf"]


async def test_format_date_et_taille(racine):
    fichier = creer(racine, "rapport.pdf", 2, taille=1536)
    date = datetime.fromtimestamp(fichier.stat().st_mtime)
    ligne = (await server.fichiers_recents()).splitlines()[0]
    assert ligne.startswith(f"{date:%Y-%m-%d %H:%M}")
    assert "1.5 Ko" in ligne
    assert ligne.endswith("rapport.pdf")


async def test_filtre_extension(racine):
    creer(racine, "a.pdf", 1)
    creer(racine, "b.PDF", 2)
    creer(racine, "c.docx", 1)
    assert chemins(await server.fichiers_recents(extension="pdf")) == ["a.pdf", "b.PDF"]


async def test_filtre_motif(racine):
    creer(racine, "Facture_EDF.pdf", 1)
    creer(racine, "devis.pdf", 1)
    assert chemins(await server.fichiers_recents(motif="facture")) == ["Facture_EDF.pdf"]


async def test_rechercher_fichiers_avec_jours(racine):
    """Les petits modèles passent souvent `jours` à rechercher_fichiers :
    il doit alors filtrer par date et trier du plus récent au plus ancien."""
    creer(racine, "facture_oct.pdf", 20)
    creer(racine, "facture_nov.pdf", 2)
    creer(racine, "devis_nov.pdf", 1)
    resultat = await server.rechercher_fichiers(motif="facture", extension="pdf", jours=7)
    assert chemins(resultat) == ["facture_nov.pdf"]
    resultat = await server.rechercher_fichiers(extension="pdf", jours=30)
    assert chemins(resultat) == ["devis_nov.pdf", "facture_nov.pdf", "facture_oct.pdf"]


async def test_motif_inutile_ignore_avec_note(racine):
    """« Quels PDF ai-je reçus cette semaine ? » -> motif='reçu' : aucun nom
    ne le contient, on renvoie quand même les PDF de la période."""
    creer(racine, "facture.pdf", 2)
    creer(racine, "vieux.pdf", 30)
    resultat = await server.rechercher_fichiers(motif="reçu", extension="pdf", jours=7)
    lignes = resultat.splitlines()
    assert lignes[0].startswith("Aucun nom de fichier ne contient « reçu »")
    assert chemins("\n".join(lignes[1:])) == ["facture.pdf"]


async def test_motif_qui_trouve_reste_prioritaire(racine):
    creer(racine, "facture.pdf", 2)
    creer(racine, "devis.pdf", 1)
    resultat = await server.fichiers_recents(motif="facture")
    assert chemins(resultat) == ["facture.pdf"]
    assert "Aucun nom" not in resultat


async def test_motif_sans_extension_ni_periode_pas_de_repli(racine):
    creer(racine, "facture.pdf", 2)
    resultat = await server.rechercher_fichiers(motif="reçu")
    assert resultat == "Aucun fichier dont le nom contient « reçu »."


async def test_motif_inutile_repli_sur_extension(racine):
    creer(racine, "facture.pdf", 2)
    creer(racine, "notes.txt", 2)
    resultat = await server.rechercher_fichiers(motif="reçu", extension="pdf")
    assert resultat == "Aucun nom de fichier ne contient « reçu ». Voici les fichiers .pdf :\nfacture.pdf"


async def test_rechercher_fichiers_sans_jours_inchange(racine):
    creer(racine, "facture_oct.pdf", 20)
    assert await server.rechercher_fichiers(motif="facture") == "facture_oct.pdf"


async def test_filtre_taille(racine):
    creer(racine, "petit.txt", 1, taille=500)
    creer(racine, "moyen.txt", 1, taille=5_000)
    creer(racine, "gros.txt", 1, taille=50_000)
    resultat = await server.fichiers_recents(taille_min_ko=1, taille_max_ko=10)
    assert chemins(resultat) == ["moyen.txt"]


async def test_filtre_sous_dossier(racine):
    creer(racine, "perso/a.txt", 1)
    creer(racine, "travail/b.txt", 1)
    assert chemins(await server.fichiers_recents(chemin="travail")) == ["travail/b.txt"]


async def test_ignore_fichiers_caches_et_verrous_office(racine):
    creer(racine, ".DS_Store", 1)
    creer(racine, "~$lettre.docx", 1)
    creer(racine, "lettre.docx", 1)
    assert chemins(await server.fichiers_recents()) == ["lettre.docx"]


async def test_max_resultats_garde_les_plus_recents(racine):
    for i in range(1, 6):
        creer(racine, f"f{i}.txt", i)
    resultat = await server.fichiers_recents(max_resultats=2)
    assert chemins(resultat) == ["f1.txt", "f2.txt"]
    assert "(5 fichier(s) trouvé(s), 2 affiché(s))" in resultat


async def test_plafonds(racine):
    for i in range(250):
        creer(racine, f"f{i}.txt", 0.5)
    resultat = await server.fichiers_recents(max_resultats=10**6, jours=-3)
    assert len(chemins(resultat)) == server.MAX_RESULTATS  # jours=-3 ramené à 1


async def test_aucun_resultat(racine):
    creer(racine, "vieux.txt", 100)
    resultat = await server.fichiers_recents(jours=7)
    assert resultat == "Aucun fichier modifié ces 7 dernier(s) jour(s) avec ces critères."


async def test_chemin_sortant_refuse(racine):
    with pytest.raises(ValueError, match="Accès refusé"):
        await server.fichiers_recents(chemin="../..")


@pytest.mark.parametrize(
    "octets,attendu",
    [(0, "0 o"), (1023, "1023 o"), (1024, "1.0 Ko"), (1536, "1.5 Ko"),
     (5 * 1024**2, "5.0 Mo"), (3 * 1024**3, "3.0 Go"), (2048 * 1024**3, "2048.0 Go")],
)
def test_taille_lisible(octets, attendu):
    assert server.taille_lisible(octets) == attendu
