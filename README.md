# mcp-explorateur-fichiers

Serveur MCP en Python qui permet à un modèle d'IA d'explorer, de rechercher et de lire vos fichiers locaux (PDF, Word, Excel, PowerPoint, texte), avec accès limité à un dossier, authentification par jeton et journal d'audit. Fonctionne avec un modèle local via Ollama.

## Fonctionnalités

| Outil | Rôle |
|---|---|
| `lister_dossier` | Liste le contenu d'un dossier |
| `rechercher_fichiers` | Cherche des fichiers par leur nom et/ou leur extension |
| `rechercher_dans_contenu` | Cherche un mot à l'intérieur des fichiers (texte, PDF, Word, Excel, PowerPoint), avec un extrait autour du mot trouvé |
| `lire_fichier` | Lit un fichier texte (txt, md, csv, json, py…) |
| `lire_pdf` | Lit un PDF, éventuellement sur une plage de pages |
| `lire_word` | Lit un document `.docx`, tableaux inclus |
| `lire_excel` | Lit un classeur `.xlsx`, toutes les feuilles ou une seule |
| `lire_powerpoint` | Lit une présentation `.pptx`, diapositive par diapositive |

## Sécurité

- **Dossier unique** : le serveur n'a accès qu'à `~/Documents`. Les chemins du type `../..` et les liens symboliques qui en sortent sont refusés.
- **Authentification** (mode HTTP) : chaque requête doit porter un jeton `Authorization: Bearer …`, comparé à temps constant.
- **Journal d'audit** : chaque appel d'outil est enregistré dans `logs/audit.jsonl` (outil, arguments, statut, durée, nom associé au jeton). Le contenu des fichiers n'est jamais enregistré.
- **Limites** : les paramètres envoyés par le client sont plafonnés (50 000 caractères lus, 200 résultats, 1 000 lignes Excel), et les fichiers de plus de 10 Mo sont ignorés pendant les recherches.

## Installation

Python 3.11 recommandé.

```bash
pip install -r requirements.txt
```

Pour utiliser le client, il faut aussi [Ollama](https://ollama.com) et le modèle :

```bash
ollama pull qwen2.5:7b
```

## Utilisation

### Mode local (stdio)

Le client lance le serveur lui-même :

```bash
python client.py
```

Exemples de questions : « Trouve mes fichiers docx dans Collins », « Quels fichiers parlent de facture ? ».
La commande `/lister <chemin>` appelle directement l'outil, sans passer par le modèle. `quit` pour sortir.

### Mode HTTP (serveur distant)

Générer un jeton par utilisateur :

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Lancer le serveur :

```bash
export MCP_TOKENS="alice:JETON_1,bob:JETON_2"
MCP_TRANSPORT=http python server.py
```

Se connecter depuis un autre terminal :

```bash
export MCP_TOKEN="JETON_1"
python client.py http://127.0.0.1:8000/mcp
```

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` ou `http` |
| `MCP_HOST` | `127.0.0.1` | Adresse d'écoute en HTTP (`0.0.0.0` pour le réseau) |
| `MCP_PORT` | `8000` | Port d'écoute en HTTP |
| `MCP_TOKENS` | (aucun) | Jetons autorisés côté serveur, format `nom:jeton,nom2:jeton2` |
| `MCP_TOKEN` | (aucun) | Jeton utilisé par le client en HTTP |

> ⚠️ En mode `0.0.0.0`, le trafic n'est pas chiffré. Placez un reverse proxy HTTPS (Caddy, nginx) ou un tunnel (Tailscale, SSH) devant le serveur.

## Vérifier le journal

```bash
tail -n 20 logs/audit.jsonl
```
