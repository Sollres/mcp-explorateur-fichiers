"""Le serveur ne doit jamais sortir du dossier autorisé."""
import pytest

import server
from conftest import lire_journal

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("chemin", ["..", "../..", "sous/../../..", "/etc", "/"])
def test_securiser_refuse_les_chemins_sortants(racine, chemin):
    with pytest.raises(ValueError, match="Accès refusé"):
        server.securiser(chemin)


def test_securiser_accepte_les_chemins_internes(racine):
    (racine / "a").mkdir()
    assert server.securiser("a/../a") == racine / "a"
    assert server.securiser("") == racine


async def test_outil_refuse_et_journalise(racine):
    with pytest.raises(ValueError, match="Accès refusé"):
        await server.lister_dossier(chemin="../..")
    entree = lire_journal()[-1]
    assert entree["outil"] == "lister_dossier"
    assert entree["statut"] == "refuse"
    assert entree["arguments"] == {"chemin": "../.."}


async def test_lien_symbolique_vers_l_exterieur(racine, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("mot de passe", encoding="utf-8")
    (racine / "lien.txt").symlink_to(secret)
    (racine / "normal.txt").write_text("rien", encoding="utf-8")

    # Ni lisible directement...
    with pytest.raises(ValueError, match="Accès refusé"):
        await server.lire_fichier(chemin="lien.txt")
    # ...ni visible dans les recherches.
    resultat = await server.rechercher_fichiers(extension="txt")
    assert resultat == "normal.txt"
    resultat = await server.rechercher_dans_contenu(mot="mot de passe")
    assert resultat.startswith("Aucun fichier")
