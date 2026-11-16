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


def feed(queries: list, days: int = 45, retmax: int = 6) -> list:
    """Fil de veille : articles récents (triés par date) pour chaque requête.

    Retourne une liste de dicts {pmid, title, journal, date, query, abstract}
    triée du plus récent au plus ancien, dédupliquée par PMID.
    """
    from datetime import date, timedelta

    socket.setdefaulttimeout(30)
    Entrez.email = pubmed_email()
    api_key = pubmed_api_key()
    if api_key:
        Entrez.api_key = api_key
    to = date.today().strftime("%Y/%m/%d")
    since = (date.today() - timedelta(days=days)).strftime("%Y/%m/%d")
    seen, out = set(), []
    for q in queries:
        if not q.strip():
            continue
        try:
            handle = Entrez.esearch(db="pubmed", term=q.strip(), retmax=retmax,
                                    sort="pub_date", datetype="pdat",
                                    mindate=since, maxdate=to)
            id_list = Entrez.read(handle).get("IdList", [])
            if not id_list:
                continue
            handle = Entrez.efetch(db="pubmed", id=",".join(id_list),
                                   retmode="xml")
            records = Entrez.read(handle)
        except Exception as e:
            out.append({"pmid": None, "title": f"Erreur PubMed ({q}) : "
                        f"{type(e).__name__} — {e}", "journal": "", "date": "",
                        "query": q, "abstract": ""})
            continue
        for art in records.get("PubmedArticle", []):
            cit = art.get("MedlineCitation", {})
            art2 = cit.get("Article", {})
            pmid = str(cit.get("PMID", ""))
            if not pmid or pmid in seen:
                continue
            seen.add(pmid)
            title = str(art2.get("ArticleTitle", "")).strip()
            journal = str(art2.get("Journal", {}).get("Title", ""))
            y = art2.get("Journal", {}).get("JournalIssue", {}).get(
                "PubDate", {}).get("Year", "")
            m = art2.get("Journal", {}).get("JournalIssue", {}).get(
                "PubDate", {}).get("Month", "")
            abstract = " ".join(str(t) for t in
                                art2.get("Abstract", {}).get("AbstractText", []))
            out.append({"pmid": pmid, "title": title, "journal": journal,
                        "date": f"{m} {y}".strip(), "query": q,
                        "abstract": abstract})
    return out
