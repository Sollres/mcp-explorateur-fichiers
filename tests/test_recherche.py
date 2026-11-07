"""Recherche par nom, par contenu, et cache du texte extrait."""
import os

import pytest

import server
from conftest import creer_docx, creer_pdf

pytestmark = pytest.mark.anyio


# --- Recherche par nom -------------------------------------------------
async def test_rechercher_par_motif_et_extension(racine):
    (racine / "factures").mkdir()
    (racine / "factures" / "Facture_mars.pdf").write_bytes(b"")
    (racine / "factures" / "facture_mars.txt").write_text("", encoding="utf-8")
    (racine / "devis.pdf").write_bytes(b"")

    assert await server.rechercher_fichiers(motif="FACTURE", extension="PDF") == (
        "factures/Facture_mars.pdf"
    )
    resultat = await server.rechercher_fichiers(extension=".pdf")
    assert set(resultat.splitlines()) == {"factures/Facture_mars.pdf", "devis.pdf"}


async def test_rechercher_ignore_dossiers_inutiles(racine):
    (racine / "node_modules").mkdir()
    (racine / "node_modules" / "rapport.md").write_text("", encoding="utf-8")
    (racine / "rapport.md").write_text("", encoding="utf-8")
    assert await server.rechercher_fichiers(motif="rapport") == "rapport.md"


async def test_rechercher_sans_critere(racine):
    resultat = await server.rechercher_fichiers()
    assert resultat == "Précise au moins un motif ou une extension."


async def test_rechercher_plafonne(racine):
    for i in range(250):
        (racine / f"f{i}.txt").write_text("", encoding="utf-8")
    resultat = await server.rechercher_fichiers(extension="txt", max_resultats=10**6)
    assert len(resultat.splitlines()) == server.MAX_RESULTATS


# --- Recherche dans le contenu -----------------------------------------
async def test_contenu_plusieurs_formats(racine):
    (racine / "a.txt").write_text("le code est ananas 42", encoding="utf-8")
    creer_docx(racine / "b.docx", ["Rien ici", "Ananas en Word"])
    creer_pdf(racine / "c.pdf", ["ananas en PDF"])
    (racine / "d.txt").write_text("autre chose", encoding="utf-8")

    resultat = await server.rechercher_dans_contenu(mot="Ananas")
    lignes = resultat.splitlines()
    fichiers = {l for l in lignes if not l.startswith(" ") and not l.startswith("(")}
    assert fichiers == {"a.txt", "b.docx", "c.pdf"}
    assert "...le code est ananas 42..." in resultat
    assert "(4 fichier(s) analysé(s), 0 ignoré(s)" in resultat


async def test_contenu_filtre_extension(racine):
    (racine / "a.txt").write_text("ananas", encoding="utf-8")
    creer_docx(racine / "b.docx", ["ananas"])
    resultat = await server.rechercher_dans_contenu(mot="ananas", extension="docx")
    assert resultat.splitlines()[0] == "b.docx"
    assert "a.txt" not in resultat


async def test_contenu_ignore_fichiers_trop_gros(racine, monkeypatch):
    monkeypatch.setattr(server, "TAILLE_MAX_RECHERCHE", 10)
    (racine / "gros.txt").write_text("ananas " * 10, encoding="utf-8")
    resultat = await server.rechercher_dans_contenu(mot="ananas")
    assert resultat.startswith("Aucun fichier")
    assert "1 ignoré(s)" in resultat


async def test_contenu_fichier_illisible(racine):
    (racine / "casse.docx").write_bytes(b"pas un vrai docx")
    resultat = await server.rechercher_dans_contenu(mot="x")
    assert "0 fichier(s) analysé(s), 1 ignoré(s)" in resultat


# --- Cache -------------------------------------------------------------
@pytest.fixture
def compteur_extractions(monkeypatch):
    """Compte les vraies extractions de texte (hors cache)."""
    appels = []
    originale = server.extraire_texte

    def espion(fichier, limite=0):
        appels.append(fichier.name)
        return originale(fichier, limite)

    monkeypatch.setattr(server, "extraire_texte", espion)
    return appels


async def test_cache_evite_de_relire(racine, compteur_extractions):
    creer_docx(racine / "a.docx", ["ananas"])
    await server.rechercher_dans_contenu(mot="ananas")
    await server.rechercher_dans_contenu(mot="ananas")
    assert compteur_extractions == ["a.docx"]


async def test_cache_invalide_si_fichier_modifie(racine, compteur_extractions):
    fichier = racine / "a.txt"
    fichier.write_text("ananas", encoding="utf-8")
    await server.rechercher_dans_contenu(mot="ananas")

    fichier.write_text("kiwi", encoding="utf-8")
    st = fichier.stat()
    os.utime(fichier, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    resultat = await server.rechercher_dans_contenu(mot="kiwi")
    assert resultat.splitlines()[0] == "a.txt"
    assert compteur_extractions == ["a.txt", "a.txt"]


async def test_cache_memorise_les_fichiers_illisibles(racine, compteur_extractions):
    (racine / "casse.docx").write_bytes(b"pas un vrai docx")
    await server.rechercher_dans_contenu(mot="x")
    await server.rechercher_dans_contenu(mot="x")
    assert compteur_extractions == ["casse.docx"]


def test_cache_retire_les_plus_anciens(tmp_path):
    cache = server.CacheTexte(max_caracteres=10)
    fichiers = []
    for nom in "abc":
        f = tmp_path / nom
        f.write_text(nom, encoding="utf-8")
        fichiers.append((f, f.stat()))

    cache.ranger(*fichiers[0], "1234")
    cache.ranger(*fichiers[1], "5678")
    assert cache.obtenir(*fichiers[0]) == (True, "1234")  # "a" devient récent
    cache.ranger(*fichiers[2], "9012")  # dépasse 10 : on retire le moins récent, "b"

    assert cache.obtenir(*fichiers[1]) == (False, None)
    assert cache.obtenir(*fichiers[0]) == (True, "1234")
    assert cache.obtenir(*fichiers[2]) == (True, "9012")
    assert cache.total == 8
