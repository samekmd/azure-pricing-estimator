"""Testa se o endpoint de save da calculadora Azure aceita autenticação via
Bearer token do Entra ID (em vez de cookie de sessão + CSRF token).

Fluxo:
  1. Obtém um access token via device code flow (você autentica como você mesmo).
  2. Decodifica e imprime os claims do token (tid / aud / appid / scp) para
     você conferir tenant e resource ANTES de disparar o POST.
  3. Faz o POST enviando apenas o header Authorization: Bearer <token>,
     sem cookie e sem token CSRF.
  4. Imprime status e as primeiras linhas do corpo da resposta.

Uso:
    uv run test_bearer_auth.py            # pausa para você confirmar antes do POST
    uv run test_bearer_auth.py --yes      # não pausa (assume já conferido)
"""

import json
import sys

import jwt  # vem como dependência do msal
import msal
import requests
from dotenv import load_dotenv
import os

load_dotenv()

# --- CONFIGURAÇÃO (confira contra o seu próprio JWT antes de rodar) ----------
TENANT_ID = os.environ.get("TENANT_ID")  # Entra ID (Azure AD) do seu tenant
RESOURCE =  os.environ.get("RESOURCE")  # App ID URI do backend da calculadora (ex: api://<guid>)
CLIENT_ID = os.environ.get("CLIENT_ID")  # Azure CLI (public client)

AUTHORITY = os.environ.get("AUTHORITY")  # ex: https://login.microsoftonline.com/<tenant_id>
SCOPES = os.environ.get("SCOPES").split(",")  # ex: api://<guid>/.default

ENDPOINT = os.environ.get("ENDPOINT")  # ex: https://<host>/api/estimate/save
PAYLOAD_FILE = os.environ.get("PAYLOAD_FILE")  # ex: payload.json
BODY_PREVIEW_LINES = os.environ.get("BODY_PREVIEW_LINES", 10)  # quantas linhas do corpo da resposta mostrar
HTTP_TIMEOUT = os.environ.get("HTTP_TIMEOUT", 30)  # segundos, aplicado a TODA chamada HTTP (inclusive as do MSAL)
# -----------------------------------------------------------------------------


class TimeoutSession(requests.Session):
    """Sessão requests que injeta um timeout padrão em toda requisição.

    O MSAL, por padrão, não define timeout — se o discovery de authority ou o
    initiate_device_flow não conseguir sair (firewall/proxy/sem internet), a
    chamada pendura indefinidamente e NADA é impresso. Passando esta sessão
    como http_client, qualquer chamada estoura em HTTP_TIMEOUT com erro claro.
    """

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        return super().request(*args, **kwargs)


def check_connectivity() -> None:
    """Falha rápido (em vez de pendurar) se o Entra ID estiver inalcançável."""
    url = f"{AUTHORITY}/v2.0/.well-known/openid-configuration"
    print(f"[1/4] Verificando conectividade com o Entra ID ({url}) ...", flush=True)
    try:
        resp = requests.get(url, timeout=10)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            "Não consegui alcançar o endpoint do Entra ID. Provável causa do "
            "travamento sem saída: sem internet, firewall ou proxy bloqueando "
            f"login.microsoftonline.com.\n  Detalhe: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Discovery retornou HTTP {resp.status_code} — verifique o TENANT_ID "
            f"({TENANT_ID}). Corpo: {resp.text[:200]}"
        )
    print("       OK — Entra ID alcançável.", flush=True)


def get_token() -> str:
    check_connectivity()

    print("[2/4] Inicializando cliente MSAL (discovery de authority) ...", flush=True)
    app = msal.PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, http_client=TimeoutSession()
    )

    print("[3/4] Solicitando device code ao Entra ID ...", flush=True)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Falha ao iniciar device flow: {json.dumps(flow, indent=2)}")

    expires_in = flow.get("expires_in", "?")
    print("\n" + "=" * 70, flush=True)
    print("⏳  AGUARDANDO VOCÊ AUTENTICAR NO NAVEGADOR", flush=True)
    print(flow["message"], flush=True)  # link + código a digitar
    print(f"(o código expira em ~{expires_in}s; o script segue sozinho após o login)", flush=True)
    print("=" * 70 + "\n", flush=True)

    print("[4/4] Aguardando conclusão do login (polling) ...", flush=True)
    result = app.acquire_token_by_device_flow(flow)  # bloqueia até login OU expiração
    if "access_token" not in result:
        error = result.get("error")
        if error in ("expired_token", "authorization_pending", "code_expired"):
            raise RuntimeError(
                "O código de device flow expirou antes de você concluir o login. "
                "Rode de novo e conclua a autenticação no navegador dentro do prazo."
            )
        raise RuntimeError(
            f"Falha ao obter token: {error} / {result.get('error_description')}"
        )
    return result["access_token"]


def show_claims(token: str) -> None:
    """Decodifica o JWT SEM validar assinatura, só para conferência visual."""
    claims = jwt.decode(token, options={"verify_signature": False})
    interessantes = {k: claims.get(k) for k in ("tid", "aud", "appid", "iss", "scp", "roles")}
    print("\n--- Claims do access token (confira tid e aud) ---")
    print(json.dumps(interessantes, indent=2))
    print(f"  tenant esperado (tid): {TENANT_ID}")
    print(f"  resource esperado (aud): {RESOURCE}")
    print("--------------------------------------------------\n")


def post_estimate(token: str) -> None:
    with open(PAYLOAD_FILE, "rb") as f:
        body = f.read()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Deliberadamente SEM Cookie e SEM header CSRF (X-XSRF-TOKEN etc.)
    }

    resp = requests.post(ENDPOINT, data=body, headers=headers, timeout=30)

    print(f"HTTP {resp.status_code} {resp.reason}")
    print("--- Primeiras linhas do corpo ---")
    print("\n".join(resp.text.splitlines()[:BODY_PREVIEW_LINES]))


def main() -> None:
    auto_yes = "--yes" in sys.argv

    token = get_token()
    show_claims(token)

    if not auto_yes:
        if input("Enviar o POST com esses valores? [y/N] ").strip().lower() != "y":
            print("Abortado antes do POST.")
            return

    post_estimate(token)


if __name__ == "__main__":
    main()
