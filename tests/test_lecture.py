"""Lecture de chaque format et respect des plafonds."""
import pytest

import server
from conftest import creer_docx, creer_pdf, creer_pptx, creer_xlsx

pytestmark = pytest.mark.anyio


# --- Texte -------------------------------------------------------------
async def test_lire_fichier_texte(racine):
    (racine / "note.md").write_text("# Titre\nBonjour", encoding="utf-8")
    assert await server.lire_fichier(chemin="note.md") == "# Titre\nBonjour"


async def test_lire_fichier_tronque_avec_mention(racine):
    (racine / "long.txt").write_text("a" * 100, encoding="utf-8")
    resultat = await server.lire_fichier(chemin="long.txt", max_caracteres=10)
    assert resultat.startswith("a" * 10 + "\n")
    assert "tronqué à 10 caractères" in resultat


async def test_lire_fichier_plafonne(racine):
    (racine / "gros.txt").write_text("x" * 60_000, encoding="utf-8")
    resultat = await server.lire_fichier(chemin="gros.txt", max_caracteres=10**9)
    assert resultat.count("x") == server.MAX_CARACTERES_LECTURE


async def test_lire_fichier_valeur_negative(racine):
    (racine / "a.txt").write_text("abc", encoding="utf-8")
    resultat = await server.lire_fichier(chemin="a.txt", max_caracteres=-5)
    assert resultat.startswith("a\n")  # ramené à 1 caractère


async def test_lire_fichier_binaire(racine):
    (racine / "image.txt").write_bytes(b"\xff\xfe\x00\x81" * 10)
    resultat = await server.lire_fichier(chemin="image.txt")
    assert "pas un fichier texte" in resultat


async def test_lire_fichier_introuvable(racine):
    assert await server.lire_fichier(chemin="absent.txt") == "Fichier introuvable."


# --- PDF ---------------------------------------------------------------
async def test_lire_pdf(racine):
    creer_pdf(racine / "doc.pdf", ["Page un", "Page deux", "Page trois"])
    resultat = await server.lire_pdf(chemin="doc.pdf")
    assert "[PDF de 3 page(s), pages 1 à 3]" in resultat
    assert "Page un" in resultat and "Page trois" in resultat


async def test_lire_pdf_plage_de_pages(racine):
    creer_pdf(racine / "doc.pdf", ["Page un", "Page deux", "Page trois"])
    resultat = await server.lire_pdf(chemin="doc.pdf", page_debut=2, page_fin=2)
    assert "Page deux" in resultat
    assert "Page un" not in resultat and "Page trois" not in resultat


async def test_lire_pdf_page_hors_limite(racine):
    creer_pdf(racine / "doc.pdf", ["Seule page"])
    resultat = await server.lire_pdf(chemin="doc.pdf", page_debut=5)
    assert resultat == "Ce PDF n'a que 1 page(s)."


async def test_lire_pdf_scanne(racine):
    creer_pdf(racine / "scan.pdf", [""])
    assert "OCR" in await server.lire_pdf(chemin="scan.pdf")


async def test_lire_pdf_mauvaise_extension(racine):
    (racine / "faux.txt").write_text("x", encoding="utf-8")
    resultat = await server.lire_pdf(chemin="faux.txt")
    assert resultat == "Ce fichier n'est pas un fichier .pdf."


# --- Word --------------------------------------------------------------
async def test_lire_word_avec_tableau(racine):
    creer_docx(
        racine / "lettre.docx",
        ["Madame, Monsieur,", "Cordialement"],
        tableau=[["Nom", "Ville"], ["Collins", "Paris"]],
    )
    resultat = await server.lire_word(chemin="lettre.docx")
    assert "Madame, Monsieur," in resultat
    assert "Collins | Paris" in resultat


# --- Excel -------------------------------------------------------------
async def test_lire_excel_toutes_les_feuilles(racine):
    creer_xlsx(
        racine / "budget.xlsx",
        {"Janvier": [["Loyer", 800]], "Février": [["Loyer", 820]]},
    )
    resultat = await server.lire_excel(chemin="budget.xlsx")
    assert "['Janvier', 'Février']" in resultat
    assert "Loyer | 800" in resultat and "Loyer | 820" in resultat


async def test_lire_excel_une_feuille(racine):
    creer_xlsx(racine / "b.xlsx", {"A": [["un"]], "B": [["deux"]]})
    resultat = await server.lire_excel(chemin="b.xlsx", feuille="B")
    assert "deux" in resultat and "un" not in resultat.split("---", 1)[1]


async def test_lire_excel_feuille_introuvable(racine):
    creer_xlsx(racine / "b.xlsx", {"A": [["un"]]})
    resultat = await server.lire_excel(chemin="b.xlsx", feuille="Z")
    assert resultat.startswith("Feuille introuvable")


async def test_lire_excel_plafonne(racine):
    creer_xlsx(racine / "grand.xlsx", {"Data": [[i] for i in range(1_500)]})
    resultat = await server.lire_excel(chemin="grand.xlsx", max_lignes=10**6)
    assert f"limité à {server.MAX_LIGNES_EXCEL} lignes" in resultat


# --- PowerPoint --------------------------------------------------------
async def test_lire_powerpoint(racine):
    creer_pptx(racine / "pres.pptx", ["Introduction", "Conclusion"])
    resultat = await server.lire_powerpoint(chemin="pres.pptx")
    assert "[Diapositive 1]" in resultat and "Introduction" in resultat
    assert "[Diapositive 2]" in resultat and "Conclusion" in resultat
