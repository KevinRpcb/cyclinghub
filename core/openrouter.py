"""Client OpenRouter robuste : erreurs explicites, multi-modèles, synthèse."""
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE_URL = "https://openrouter.ai/api/v1"

CURATED_MODELS = [
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-4.1",
    "google/gemini-2.5-flash",
    "x-ai/grok-3-mini",
    "deepseek/deepseek-chat-v3-0324",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
    "qwen/qwen3-235b-a22b:free",
]

DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def _headers(key: str) -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://wt-performance-hub.streamlit.app",
        "X-Title": "WT Performance Hub",
    }


def chat(model: str, messages: list, key: str, temperature: float = 0.4,
         max_tokens: int = 2500, timeout: int = 120):
    """Retourne (ok, contenu | message d'erreur). N'échoue jamais silencieusement."""
    if not key:
        return False, "Clé OpenRouter absente — ajoutez OPENROUTER_API_KEY dans les Secrets Streamlit."
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    try:
        r = requests.post(f"{BASE_URL}/chat/completions", headers=_headers(key),
                          json=payload, timeout=timeout)
    except requests.RequestException as e:
        return False, f"Erreur réseau : {e}"
    try:
        data = r.json()
    except ValueError:
        return False, f"Réponse illisible (HTTP {r.status_code})."
    if r.status_code != 200:
        msg = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
            elif isinstance(err, str):
                msg = err
        return False, msg or f"HTTP {r.status_code} — {str(data)[:200]}"
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return False, f"Réponse inattendue de l'API : {str(data)[:200]}"
    if not content:
        return False, "Le modèle a renvoyé une réponse vide."
    return True, content


def list_models(key: str):
    """Diagnostic de clé : liste les modèles accessibles. Retourne (ok, ids | erreur)."""
    if not key:
        return False, "Clé OpenRouter absente (Secrets)."
    try:
        r = requests.get(f"{BASE_URL}/models", headers=_headers(key), timeout=20)
    except requests.RequestException as e:
        return False, f"Erreur réseau : {e}"
    if r.status_code == 401:
        return False, "Clé invalide (HTTP 401)."
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}."
    try:
        ids = [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return False, "Réponse inattendue du endpoint /models."
    return True, ids


def multi_consult(models: list, system: str, user: str, key: str,
                  temperature: float = 0.4, max_tokens: int = 2200, max_workers: int = 4) -> list:
    """Interroge plusieurs IA en parallèle. Retourne [{model, ok, content}]."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(chat, m, messages, key, temperature, max_tokens): m for m in models}
        for fut in as_completed(futures):
            model = futures[fut]
            try:
                ok, content = fut.result()
            except Exception as e:
                ok, content = False, f"Exception : {e}"
            results.append({"model": model, "ok": ok, "content": content if ok else f"❌ {content}"})
    order = {m: i for i, m in enumerate(models)}
    results.sort(key=lambda r: order.get(r["model"], 99))
    return results


def synthesize(results: list, system: str, user: str, key: str, model: str,
               temperature: float = 0.3, max_tokens: int = 2500):
    """Fait synthétiser par un modèle les réponses convergentes/divergentes des autres."""
    blocks = []
    for r in results:
        label = r["model"] if r["ok"] else f"{r['model']} (échec)"
        blocks.append(f"### Réponse de {label}\n{r['content']}")
    corpus = "\n\n---\n\n".join(blocks)
    synth_user = (
        f"Question initiale posée à plusieurs IA spécialisées :\n\n{user}\n\n"
        f"Réponses obtenues :\n\n{corpus}\n\n"
        "Rédige une synthèse d'expert en français : 1) points de consensus, "
        "2) divergences et pourquoi, 3) recommandation finale opérationnelle pour le staff, "
        "4) niveau de confiance (faible/moyen/élevé) et ce qu'il faudrait mesurer pour trancher."
    )
    return chat(model, [{"role": "system", "content": system},
                        {"role": "user", "content": synth_user}],
                key, temperature=temperature, max_tokens=max_tokens)
