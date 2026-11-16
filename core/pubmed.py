"""Recherche PubMed avec timeouts, gestion d'erreurs et clé API optionnelle."""
import socket

from Bio import Entrez

from .config import pubmed_api_key, pubmed_email


def search(query: str, retmax: int = 8) -> str:
    """Retourne les références + résumés en texte brut, ou un message d'erreur clair."""
    if not query.strip():
        return "Requête vide."
    try:
        socket.setdefaulttimeout(30)
        Entrez.email = pubmed_email()
        api_key = pubmed_api_key()
        if api_key:
            Entrez.api_key = api_key
        handle = Entrez.esearch(db="pubmed", term=query.strip(), retmax=retmax, sort="relevance")
        id_list = Entrez.read(handle).get("IdList", [])
        if not id_list:
            return "Aucun article trouvé pour cette requête."
        handle = Entrez.efetch(db="pubmed", id=",".join(id_list), rettype="abstract", retmode="text")
        raw = handle.read()
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
        return text if text.strip() else "Aucun contenu récupéré."
    except Exception as e:
        return f"Erreur PubMed : {type(e).__name__} — {e}"
