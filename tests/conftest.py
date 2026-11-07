"""Configuration commune des tests.

Les tests n'utilisent JAMAIS ~/Documents ni le vrai logs/audit.jsonl :
- le journal est redirigé vers un dossier temporaire (MCP_LOGS_DIR),
  défini AVANT l'import de server ;
- chaque test reçoit son propre dossier racine temporaire (fixture `racine`).
"""
import json
import os
import tempfile

os.environ["MCP_LOGS_DIR"] = tempfile.mkdtemp(prefix="mcp-tests-logs-")

import pytest  # noqa: E402
from docx import Document  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def anyio_backend():
    """Les tests asynchrones (@pytest.mark.anyio) tournent sous asyncio."""
    return "asyncio"


@pytest.fixture
def racine(tmp_path, monkeypatch):
    """Dossier autorisé temporaire, avec un cache de texte vide."""
    dossier = (tmp_path / "Documents").resolve()
    dossier.mkdir()
    monkeypatch.setattr(server, "RACINE", dossier)
    monkeypatch.setattr(
        server, "cache_texte", server.CacheTexte(server.CACHE_MAX_CARACTERES)
    )
    return dossier


def lire_journal() -> list[dict]:
    """Toutes les entrées du journal d'audit des tests."""
    fichier = server.DOSSIER_LOGS / "audit.jsonl"
    if not fichier.exists():
        return []
    return [json.loads(l) for l in fichier.read_text(encoding="utf-8").splitlines()]


# ----------------------------------------------------------------------
# Fabrication de fichiers de test
# ----------------------------------------------------------------------
def creer_docx(chemin, paragraphes, tableau=None):
    doc = Document()
    for p in paragraphes:
        doc.add_paragraph(p)
    if tableau:
        t = doc.add_table(rows=len(tableau), cols=len(tableau[0]))
        for i, ligne in enumerate(tableau):
            for j, valeur in enumerate(ligne):
                t.cell(i, j).text = valeur
    doc.save(chemin)


def creer_xlsx(chemin, feuilles: dict):
    """feuilles = {"Nom": [[ligne1...], [ligne2...]]}"""
    classeur = Workbook()
    classeur.remove(classeur.active)
    for nom, lignes in feuilles.items():
        feuille = classeur.create_sheet(nom)
        for ligne in lignes:
            feuille.append(ligne)
    classeur.save(chemin)


def creer_pptx(chemin, textes):
    prs = Presentation()
    for texte in textes:
        diapo = prs.slides.add_slide(prs.slide_layouts[5])
        boite = diapo.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
        boite.text_frame.text = texte
    prs.save(chemin)


def creer_pdf(chemin, pages):
    """PDF minimal écrit à la main : une ligne de texte par page.
    Une page "" donne une page sans texte (comme un scan)."""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objets = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, texte in enumerate(pages):
        flux = f"BT /F1 12 Tf 72 720 Td ({texte}) Tj ET".encode("latin-1")
        objets.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objets.append(b"<< /Length %d >>\nstream\n" % len(flux) + flux + b"\nendstream")

    sortie = bytearray(b"%PDF-1.4\n")
    positions = []
    for k, obj in enumerate(objets, start=1):
        positions.append(len(sortie))
        sortie += b"%d 0 obj\n" % k + obj + b"\nendobj\n"
    debut_xref = len(sortie)
    sortie += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objets) + 1)
    for p in positions:
        sortie += b"%010d 00000 n \n" % p
    sortie += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objets) + 1,
        debut_xref,
    )
    chemin.write_bytes(bytes(sortie))
