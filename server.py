import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from enum import Enum
from pathlib import Path
from urllib.parse import urlencode, urlparse

import anyio
import pandas as pd
import yfinance as yf
from mcp.server.fastmcp import FastMCP
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("yahoo_finance_mcp")

TRANSPORTS = ("stdio", "sse", "streamable-http")

# How long an authorization code / access token issued by the OAuth endpoints below
# stays valid. Codes are exchanged within seconds during a live login; tokens are meant
# to last a client a while without needing a refresh-token dance.
OAUTH_CODE_TTL_SECONDS = 60
OAUTH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _sign_token(payload: dict, secret: str) -> str:
    """Encode payload as a compact, tamper-evident token: base64(json).base64(hmac).

    Deliberately not a real JWT library -- this only needs one algorithm (HMAC-SHA256)
    and this project has no other use for a JWT dependency. Verification is purely
    computational (no server-side lookup), so it survives Cloud Run cold starts and
    multiple instances without any shared state.
    """
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(
        b"="
    )
    sig = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return (body + b"." + sig_b64).decode()


def _verify_token(token: str, secret: str) -> dict | None:
    """Verify a token from _sign_token. Returns the payload, or None if invalid/expired."""
    try:
        body_b64, sig_b64 = token.encode().split(b".", 1)
    except ValueError:
        return None
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), body_b64, hashlib.sha256).digest()
    )
    expected_sig = expected_sig.rstrip(b"=")
    if not hmac.compare_digest(sig_b64, expected_sig):
        return None
    try:
        padded = body_b64 + b"=" * (-len(body_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# Paths that must stay reachable without a token -- they're how you get one, or how a
# client discovers where to ask. Their own logic (client_id/secret, PKCE, redirect_uri
# allow-list) is the security boundary here, not this middleware.
OAUTH_EXEMPT_PATHS = frozenset(
    {
        "/authorize",
        "/token",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    }
)


class ClientAuthMiddleware:
    """Gate every request behind a fixed client ID / secret pair, accepted either as
    HTTP Basic Auth or as a Bearer token issued by this server's own OAuth endpoints
    (see build_oauth_routes below). claude.ai's custom connector can use whichever it
    prefers -- a Request Headers value, or the OAuth Client ID/Secret fields.
    """

    def __init__(self, app: ASGIApp, client_id: str, client_secret: str) -> None:
        self._app = app
        self._client_id = client_id
        self._client_secret = client_secret
        credentials = f"{client_id}:{client_secret}".encode()
        self._expected_basic = base64.b64encode(credentials).decode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope["path"] in OAUTH_EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "basic" and hmac.compare_digest(credentials, self._expected_basic):
            await self._app(scope, receive, send)
            return
        if scheme.lower() == "bearer":
            payload = _verify_token(credentials, self._client_secret)
            if payload is not None and payload.get("client_id") == self._client_id:
                await self._app(scope, receive, send)
                return

        response = Response(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="yahoo-finance-mcp"'},
        )
        await response(scope, receive, send)


# --- Minimal OAuth 2.1 authorization server ---
#
# Just enough to satisfy claude.ai's custom connector "OAuth Client ID / Client
# Secret" fields: one pre-registered client (the same --client-id/--client-secret
# used for Basic Auth), the authorization-code + PKCE flow, no dynamic client
# registration, no refresh tokens, no persistent storage -- codes and access tokens
# are self-contained signed values (see _sign_token/_verify_token above), so this
# works correctly even if Cloud Run recycles the instance or scales to more than one.
#
# What this deliberately does not do: enforce single-use of an authorization code
# (there's no server-side state to mark one as spent), so a code could in principle
# be replayed within its 60-second window. Accepted trade-off for a personal,
# single-user server -- getting that far already requires knowing the client ID and
# reaching a redirect_uri on an allow-listed host.

_CONSENT_PAGE = """<!doctype html>
<html><head><title>Yahoo Finance MCP</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 28rem; margin: 4rem auto; padding: 0 1rem;">
<h1 style="font-size: 1.25rem;">Yahoo Finance MCP</h1>
<p>A client is requesting access to this server's tools.</p>
<form method="post">
  {hidden_fields}
  <button type="submit" style="padding: 0.5rem 1.5rem; font-size: 1rem;">Approve</button>
</form>
</body></html>"""


def _pkce_challenge_from_verifier(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_oauth_routes(
    client_id: str, client_secret: str, redirect_hosts: list[str]
) -> list[Route]:
    """Routes implementing the minimal OAuth flow described above."""

    allowed_hosts = {h.strip() for h in redirect_hosts if h.strip()}

    async def authorization_server_metadata(request: Request) -> Response:
        base = str(request.base_url).rstrip("/")
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                ],
            }
        )

    async def protected_resource_metadata(request: Request) -> Response:
        base = str(request.base_url).rstrip("/")
        return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})

    async def authorize(request: Request) -> Response:
        if request.method == "GET":
            params = dict(request.query_params)
        else:
            params = dict(await request.form())

        req_client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        response_type = params.get("response_type", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        state = params.get("state", "")

        # Validate redirect_uri before it's ever used in a redirect -- an unvalidated
        # redirect_uri is how an authorization code ends up handed to an attacker.
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            logger.warning(
                "OAuth /authorize rejected redirect_uri=%r (allowed hosts: %s). If this "
                "looks like a legitimate client, set YFINANCE_MCP_OAUTH_REDIRECT_HOSTS to "
                "include %r.",
                redirect_uri,
                sorted(allowed_hosts),
                parsed.hostname,
            )
            return PlainTextResponse(
                f"Invalid redirect_uri: host must be one of {sorted(allowed_hosts)}.",
                status_code=400,
            )

        if (
            req_client_id != client_id
            or response_type != "code"
            or code_challenge_method != "S256"
            or not code_challenge
        ):
            return RedirectResponse(
                f"{redirect_uri}?" + urlencode({"error": "invalid_request", "state": state}),
                status_code=302,
            )

        if request.method == "GET":
            hidden = "".join(
                f'<input type="hidden" name="{k}" value="{v}">' for k, v in params.items()
            )
            return HTMLResponse(_CONSENT_PAGE.format(hidden_fields=hidden))

        code = _sign_token(
            {
                "client_id": req_client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "exp": time.time() + OAUTH_CODE_TTL_SECONDS,
            },
            client_secret,
        )
        return RedirectResponse(
            f"{redirect_uri}?" + urlencode({"code": code, "state": state}), status_code=302
        )

    async def token(request: Request) -> Response:
        form = await request.form()
        grant_type = form.get("grant_type", "")
        code = form.get("code", "")
        redirect_uri = form.get("redirect_uri", "")
        code_verifier = form.get("code_verifier", "")

        req_client_id, req_client_secret = None, None
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                req_client_id, _, req_client_secret = decoded.partition(":")
            except Exception:
                pass
        if not req_client_id:
            req_client_id = form.get("client_id", "")
            req_client_secret = form.get("client_secret", "")

        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        if req_client_id != client_id or not hmac.compare_digest(
            req_client_secret or "", client_secret
        ):
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        payload = _verify_token(code, client_secret)
        if payload is None:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "code invalid or expired"},
                status_code=400,
            )
        if payload.get("redirect_uri") != redirect_uri:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                status_code=400,
            )
        expected_challenge = _pkce_challenge_from_verifier(code_verifier)
        if not hmac.compare_digest(expected_challenge, payload.get("code_challenge", "")):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "code_verifier mismatch"},
                status_code=400,
            )

        access_token = _sign_token(
            {"client_id": req_client_id, "exp": time.time() + OAUTH_TOKEN_TTL_SECONDS},
            client_secret,
        )
        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": OAUTH_TOKEN_TTL_SECONDS,
            }
        )

    return [
        Route(
            "/.well-known/oauth-authorization-server",
            authorization_server_metadata,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]
        ),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
    ]


def _default_cache_dir() -> Path:
    """Directory yfinance may use for its timezone cache.

    Defaults to the XDG cache directory so the server also works when it runs as a
    system service whose home directory is read-only.
    """
    env_dir = os.getenv("YFINANCE_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    xdg_cache = os.getenv("XDG_CACHE_HOME")
    base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
    return base / "yahoo-finance-mcp"


def configure_cache(cache_dir: str | None = None) -> None:
    """Point yfinance at a writable cache directory, if there is one."""
    path = None
    try:
        path = Path(cache_dir) if cache_dir else _default_cache_dir()
        path.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(path))
        logger.debug("Using yfinance cache directory %s", path)
    except (OSError, RuntimeError) as e:
        # Not fatal: yfinance falls back to re-fetching timezone data every time.
        # RuntimeError covers Path.home() failing when HOME is unset, which happens
        # under some service managers.
        logger.warning("Could not use cache directory %s: %s", path or "(unresolved)", e)


# Define an enum for the type of financial statement
class FinancialType(str, Enum):
    income_stmt = "income_stmt"
    quarterly_income_stmt = "quarterly_income_stmt"
    balance_sheet = "balance_sheet"
    quarterly_balance_sheet = "quarterly_balance_sheet"
    cashflow = "cashflow"
    quarterly_cashflow = "quarterly_cashflow"


class HolderType(str, Enum):
    major_holders = "major_holders"
    institutional_holders = "institutional_holders"
    mutualfund_holders = "mutualfund_holders"
    insider_transactions = "insider_transactions"
    insider_purchases = "insider_purchases"
    insider_roster_holders = "insider_roster_holders"


class RecommendationType(str, Enum):
    recommendations = "recommendations"
    upgrades_downgrades = "upgrades_downgrades"


# Initialize FastMCP server
yfinance_server = FastMCP(
    "yfinance",
    instructions="""
# Yahoo Finance MCP Server

This server is used to get information about a given ticker symbol from yahoo finance.

Available tools:
- get_historical_stock_prices: Get historical stock prices for a given ticker symbol from yahoo finance. Include the following information: Date, Open, High, Low, Close, Volume, Adj Close.
- get_stock_info: Get stock information for a given ticker symbol from yahoo finance. Include the following information: Stock Price & Trading Info, Company Information, Financial Metrics, Earnings & Revenue, Margins & Returns, Dividends, Balance Sheet, Ownership, Analyst Coverage, Risk Metrics, Other.
- get_yahoo_finance_news: Get news for a given ticker symbol from yahoo finance.
- get_stock_actions: Get stock dividends and stock splits for a given ticker symbol from yahoo finance.
- get_financial_statement: Get financial statement for a given ticker symbol from yahoo finance. You can choose from the following financial statement types: income_stmt, quarterly_income_stmt, balance_sheet, quarterly_balance_sheet, cashflow, quarterly_cashflow.
- get_holder_info: Get holder information for a given ticker symbol from yahoo finance. You can choose from the following holder types: major_holders, institutional_holders, mutualfund_holders, insider_transactions, insider_purchases, insider_roster_holders.
- get_option_expiration_dates: Fetch the available options expiration dates for a given ticker symbol.
- get_option_chain: Fetch the option chain for a given ticker symbol, expiration date, and option type.
- get_recommendations: Get recommendations or upgrades/downgrades for a given ticker symbol from yahoo finance. You can also specify the number of months back to get upgrades/downgrades for, default is 12.
""",
)


@yfinance_server.tool(
    name="get_historical_stock_prices",
    description="""Get historical stock prices for a given ticker symbol from yahoo finance. Include the following information: Date, Open, High, Low, Close, Volume, Adj Close.
Args:
    ticker: str
        The ticker symbol of the stock to get historical prices for, e.g. "AAPL"
    period : str
        Valid periods: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max
        Either Use period parameter or use start and end
        Default is "1mo"
    interval : str
        Valid intervals: 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo
        Intraday data cannot extend last 60 days
        Default is "1d"
""",
)
async def get_historical_stock_prices(
    ticker: str, period: str = "1mo", interval: str = "1d"
) -> str:
    """Get historical stock prices for a given ticker symbol

    Args:
        ticker: str
            The ticker symbol of the stock to get historical prices for, e.g. "AAPL"
        period : str
            Valid periods: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max
            Either Use period parameter or use start and end
            Default is "1mo"
        interval : str
            Valid intervals: 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo
            Intraday data cannot extend last 60 days
            Default is "1d"
    """
    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting historical stock prices for {ticker}: {e}")
        return f"Error: getting historical stock prices for {ticker}: {e}"

    # If the company is found, get the historical data
    hist_data = company.history(period=period, interval=interval)
    hist_data = hist_data.reset_index(names="Date")
    hist_data = hist_data.to_json(orient="records", date_format="iso")
    return hist_data


@yfinance_server.tool(
    name="get_stock_info",
    description="""Get stock information for a given ticker symbol from yahoo finance. Include the following information:
Stock Price & Trading Info, Company Information, Financial Metrics, Earnings & Revenue, Margins & Returns, Dividends, Balance Sheet, Ownership, Analyst Coverage, Risk Metrics, Other.

Args:
    ticker: str
        The ticker symbol of the stock to get information for, e.g. "AAPL"
""",
)
async def get_stock_info(ticker: str) -> str:
    """Get stock information for a given ticker symbol"""
    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting stock information for {ticker}: {e}")
        return f"Error: getting stock information for {ticker}: {e}"
    info = company.info
    return json.dumps(info)


@yfinance_server.tool(
    name="get_yahoo_finance_news",
    description="""Get news for a given ticker symbol from yahoo finance.

Args:
    ticker: str
        The ticker symbol of the stock to get news for, e.g. "AAPL"
""",
)
async def get_yahoo_finance_news(ticker: str) -> str:
    """Get news for a given ticker symbol

    Args:
        ticker: str
            The ticker symbol of the stock to get news for, e.g. "AAPL"
    """
    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting news for {ticker}: {e}")
        return f"Error: getting news for {ticker}: {e}"

    # If the company is found, get the news
    try:
        news = company.news
    except Exception as e:
        logger.error(f"Error: getting news for {ticker}: {e}")
        return f"Error: getting news for {ticker}: {e}"

    news_list = []
    for news in company.news:
        if news.get("content", {}).get("contentType", "") == "STORY":
            title = news.get("content", {}).get("title", "")
            summary = news.get("content", {}).get("summary", "")
            description = news.get("content", {}).get("description", "")
            url = news.get("content", {}).get("canonicalUrl", {}).get("url", "")
            news_list.append(
                f"Title: {title}\nSummary: {summary}\nDescription: {description}\nURL: {url}"
            )
    if not news_list:
        logger.warning(f"No news found for company that searched with {ticker} ticker.")
        return f"No news found for company that searched with {ticker} ticker."
    return "\n\n".join(news_list)


@yfinance_server.tool(
    name="get_stock_actions",
    description="""Get stock dividends and stock splits for a given ticker symbol from yahoo finance.

Args:
    ticker: str
        The ticker symbol of the stock to get stock actions for, e.g. "AAPL"
""",
)
async def get_stock_actions(ticker: str) -> str:
    """Get stock dividends and stock splits for a given ticker symbol"""
    try:
        company = yf.Ticker(ticker)
    except Exception as e:
        logger.error(f"Error: getting stock actions for {ticker}: {e}")
        return f"Error: getting stock actions for {ticker}: {e}"
    actions_df = company.actions
    actions_df = actions_df.reset_index(names="Date")
    return actions_df.to_json(orient="records", date_format="iso")


@yfinance_server.tool(
    name="get_financial_statement",
    description="""Get financial statement for a given ticker symbol from yahoo finance. You can choose from the following financial statement types: income_stmt, quarterly_income_stmt, balance_sheet, quarterly_balance_sheet, cashflow, quarterly_cashflow.

Args:
    ticker: str
        The ticker symbol of the stock to get financial statement for, e.g. "AAPL"
    financial_type: str
        The type of financial statement to get. You can choose from the following financial statement types: income_stmt, quarterly_income_stmt, balance_sheet, quarterly_balance_sheet, cashflow, quarterly_cashflow.
""",
)
async def get_financial_statement(ticker: str, financial_type: str) -> str:
    """Get financial statement for a given ticker symbol"""

    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting financial statement for {ticker}: {e}")
        return f"Error: getting financial statement for {ticker}: {e}"

    if financial_type == FinancialType.income_stmt:
        financial_statement = company.income_stmt
    elif financial_type == FinancialType.quarterly_income_stmt:
        financial_statement = company.quarterly_income_stmt
    elif financial_type == FinancialType.balance_sheet:
        financial_statement = company.balance_sheet
    elif financial_type == FinancialType.quarterly_balance_sheet:
        financial_statement = company.quarterly_balance_sheet
    elif financial_type == FinancialType.cashflow:
        financial_statement = company.cashflow
    elif financial_type == FinancialType.quarterly_cashflow:
        financial_statement = company.quarterly_cashflow
    else:
        return f"Error: invalid financial type {financial_type}. Please use one of the following: {FinancialType.income_stmt}, {FinancialType.quarterly_income_stmt}, {FinancialType.balance_sheet}, {FinancialType.quarterly_balance_sheet}, {FinancialType.cashflow}, {FinancialType.quarterly_cashflow}."

    # Create a list to store all the json objects
    result = []

    # Loop through each column (date)
    for column in financial_statement.columns:
        if isinstance(column, pd.Timestamp):
            date_str = column.strftime("%Y-%m-%d")  # Format as YYYY-MM-DD
        else:
            date_str = str(column)

        # Create a dictionary for each date
        date_obj = {"date": date_str}

        # Add each metric as a key-value pair
        for index, value in financial_statement[column].items():
            # Add the value, handling NaN values
            date_obj[index] = None if pd.isna(value) else value

        result.append(date_obj)

    return json.dumps(result)


@yfinance_server.tool(
    name="get_holder_info",
    description="""Get holder information for a given ticker symbol from yahoo finance. You can choose from the following holder types: major_holders, institutional_holders, mutualfund_holders, insider_transactions, insider_purchases, insider_roster_holders.

Args:
    ticker: str
        The ticker symbol of the stock to get holder information for, e.g. "AAPL"
    holder_type: str
        The type of holder information to get. You can choose from the following holder types: major_holders, institutional_holders, mutualfund_holders, insider_transactions, insider_purchases, insider_roster_holders.
""",
)
async def get_holder_info(ticker: str, holder_type: str) -> str:
    """Get holder information for a given ticker symbol"""

    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting holder info for {ticker}: {e}")
        return f"Error: getting holder info for {ticker}: {e}"

    if holder_type == HolderType.major_holders:
        return company.major_holders.reset_index(names="metric").to_json(orient="records")
    elif holder_type == HolderType.institutional_holders:
        return company.institutional_holders.to_json(orient="records")
    elif holder_type == HolderType.mutualfund_holders:
        return company.mutualfund_holders.to_json(orient="records", date_format="iso")
    elif holder_type == HolderType.insider_transactions:
        return company.insider_transactions.to_json(orient="records", date_format="iso")
    elif holder_type == HolderType.insider_purchases:
        return company.insider_purchases.to_json(orient="records", date_format="iso")
    elif holder_type == HolderType.insider_roster_holders:
        return company.insider_roster_holders.to_json(orient="records", date_format="iso")
    else:
        return f"Error: invalid holder type {holder_type}. Please use one of the following: {HolderType.major_holders}, {HolderType.institutional_holders}, {HolderType.mutualfund_holders}, {HolderType.insider_transactions}, {HolderType.insider_purchases}, {HolderType.insider_roster_holders}."


@yfinance_server.tool(
    name="get_option_expiration_dates",
    description="""Fetch the available options expiration dates for a given ticker symbol.

Args:
    ticker: str
        The ticker symbol of the stock to get option expiration dates for, e.g. "AAPL"
""",
)
async def get_option_expiration_dates(ticker: str) -> str:
    """Fetch the available options expiration dates for a given ticker symbol."""

    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting option expiration dates for {ticker}: {e}")
        return f"Error: getting option expiration dates for {ticker}: {e}"
    return json.dumps(company.options)


@yfinance_server.tool(
    name="get_option_chain",
    description="""Fetch the option chain for a given ticker symbol, expiration date, and option type.

Args:
    ticker: str
        The ticker symbol of the stock to get option chain for, e.g. "AAPL"
    expiration_date: str
        The expiration date for the options chain (format: 'YYYY-MM-DD')
    option_type: str
        The type of option to fetch ('calls' or 'puts')
""",
)
async def get_option_chain(ticker: str, expiration_date: str, option_type: str) -> str:
    """Fetch the option chain for a given ticker symbol, expiration date, and option type.

    Args:
        ticker: The ticker symbol of the stock
        expiration_date: The expiration date for the options chain (format: 'YYYY-MM-DD')
        option_type: The type of option to fetch ('calls' or 'puts')

    Returns:
        str: JSON string containing the option chain data
    """

    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting option chain for {ticker}: {e}")
        return f"Error: getting option chain for {ticker}: {e}"

    # Check if the expiration date is valid
    if expiration_date not in company.options:
        return f"Error: No options available for the date {expiration_date}. You can use `get_option_expiration_dates` to get the available expiration dates."

    # Check if the option type is valid
    if option_type not in ["calls", "puts"]:
        return "Error: Invalid option type. Please use 'calls' or 'puts'."

    # Get the option chain
    option_chain = company.option_chain(expiration_date)
    if option_type == "calls":
        return option_chain.calls.to_json(orient="records", date_format="iso")
    elif option_type == "puts":
        return option_chain.puts.to_json(orient="records", date_format="iso")
    else:
        return f"Error: invalid option type {option_type}. Please use one of the following: calls, puts."


@yfinance_server.tool(
    name="get_recommendations",
    description="""Get recommendations or upgrades/downgrades for a given ticker symbol from yahoo finance. You can also specify the number of months back to get upgrades/downgrades for, default is 12.

Args:
    ticker: str
        The ticker symbol of the stock to get recommendations for, e.g. "AAPL"
    recommendation_type: str
        The type of recommendation to get. You can choose from the following recommendation types: recommendations, upgrades_downgrades.
    months_back: int
        The number of months back to get upgrades/downgrades for, default is 12.
""",
)
async def get_recommendations(ticker: str, recommendation_type: str, months_back: int = 12) -> str:
    """Get recommendations or upgrades/downgrades for a given ticker symbol"""
    company = yf.Ticker(ticker)
    try:
        if company.isin is None:
            logger.warning(f"Company ticker {ticker} not found.")
            return f"Company ticker {ticker} not found."
    except Exception as e:
        logger.error(f"Error: getting recommendations for {ticker}: {e}")
        return f"Error: getting recommendations for {ticker}: {e}"
    try:
        if recommendation_type == RecommendationType.recommendations:
            return company.recommendations.to_json(orient="records")
        elif recommendation_type == RecommendationType.upgrades_downgrades:
            # Get the upgrades/downgrades based on the cutoff date
            upgrades_downgrades = company.upgrades_downgrades.reset_index()
            cutoff_date = pd.Timestamp.now() - pd.DateOffset(months=months_back)
            upgrades_downgrades = upgrades_downgrades[
                upgrades_downgrades["GradeDate"] >= cutoff_date
            ]
            upgrades_downgrades = upgrades_downgrades.sort_values("GradeDate", ascending=False)
            # Get the first occurrence (most recent) for each firm
            latest_by_firm = upgrades_downgrades.drop_duplicates(subset=["Firm"])
            return latest_by_firm.to_json(orient="records", date_format="iso")
    except Exception as e:
        logger.error(f"Error: getting recommendations for {ticker}: {e}")
        return f"Error: getting recommendations for {ticker}: {e}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="yahoo-finance-mcp",
        description="MCP server exposing Yahoo Finance data.",
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=os.getenv("YFINANCE_MCP_TRANSPORT", "stdio"),
        help=(
            "Transport to serve on. Use stdio when the MCP client launches the server "
            "itself, or streamable-http/sse to run it as a network service "
            "(env: YFINANCE_MCP_TRANSPORT). Default: stdio"
        ),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("YFINANCE_MCP_HOST", "127.0.0.1"),
        help=(
            "Interface to bind for the http transports. Use 0.0.0.0 to accept "
            "connections from other machines on the network "
            "(env: YFINANCE_MCP_HOST). Default: 127.0.0.1"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("YFINANCE_MCP_PORT", "8000")),
        help="Port for the http transports (env: YFINANCE_MCP_PORT). Default: 8000",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help=(
            "Directory for the yfinance timezone cache "
            "(env: YFINANCE_CACHE_DIR). Default: ~/.cache/yahoo-finance-mcp"
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("YFINANCE_MCP_LOG_LEVEL", "INFO"),
        help="Logging level, e.g. DEBUG, INFO, WARNING (env: YFINANCE_MCP_LOG_LEVEL). Default: INFO",
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("YFINANCE_MCP_CLIENT_ID"),
        help=(
            "Require this client ID, paired with --client-secret, as HTTP Basic Auth "
            "credentials on the sse/streamable-http transports (env: "
            "YFINANCE_MCP_CLIENT_ID). Omit both to leave the endpoint unauthenticated. "
            "Prefer the env vars over the flags: process arguments are visible to other "
            "users on the same machine (e.g. via `ps`)."
        ),
    )
    parser.add_argument(
        "--client-secret",
        default=os.getenv("YFINANCE_MCP_CLIENT_SECRET"),
        help="Secret paired with --client-id (env: YFINANCE_MCP_CLIENT_SECRET).",
    )
    parser.add_argument(
        "--oauth-redirect-hosts",
        default=os.getenv("YFINANCE_MCP_OAUTH_REDIRECT_HOSTS", "claude.ai"),
        help=(
            "Comma-separated hostnames the OAuth /authorize endpoint will redirect "
            "back to (env: YFINANCE_MCP_OAUTH_REDIRECT_HOSTS). Only matters if "
            "--client-id/--client-secret are set. Default: claude.ai -- if the OAuth "
            "flow rejects claude.ai's redirect_uri, the server logs the exact host it "
            "saw; add that here."
        ),
    )
    args = parser.parse_args(argv)

    if bool(args.client_id) != bool(args.client_secret):
        parser.error("--client-id and --client-secret must be set together, or not at all")

    return args


async def _serve_http(
    transport: str,
    host: str,
    port: int,
    log_level: str,
    client_id: str | None,
    client_secret: str | None,
    oauth_redirect_hosts: str,
) -> None:
    """Serve the sse/streamable-http transport, bypassing FastMCP.run() so
    ClientAuthMiddleware and the OAuth routes can be attached before it starts."""
    import uvicorn

    app = yfinance_server.sse_app() if transport == "sse" else yfinance_server.streamable_http_app()
    if client_id:
        app.add_middleware(ClientAuthMiddleware, client_id=client_id, client_secret=client_secret)
        app.routes.extend(
            build_oauth_routes(client_id, client_secret, oauth_redirect_hosts.split(","))
        )
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level.lower())
    await uvicorn.Server(config).serve()


def main() -> None:
    args = parse_args()

    # Log to stderr: on the stdio transport, stdout carries the JSON-RPC stream and
    # anything else written there corrupts it.
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    configure_cache(args.cache_dir)

    if args.transport == "stdio":
        if args.client_id:
            logger.warning(
                "--client-id/--client-secret only apply to the sse and streamable-http "
                "transports; ignoring them for stdio."
            )
        logger.info("Starting Yahoo Finance MCP server on stdio...")
        yfinance_server.run(transport="stdio")
        return

    path = (
        yfinance_server.settings.sse_path
        if args.transport == "sse"
        else yfinance_server.settings.streamable_http_path
    )
    auth_state = "required (client credentials)" if args.client_id else "NONE (unauthenticated)"
    logger.info(
        "Starting Yahoo Finance MCP server at http://%s:%s%s (%s), authentication: %s...",
        args.host,
        args.port,
        path,
        args.transport,
        auth_state,
    )
    if not args.client_id:
        logger.warning(
            "No --client-id/--client-secret set: anything that can reach %s:%s can call "
            "every tool. See docs/raspberry-pi.md before exposing this beyond localhost.",
            args.host,
            args.port,
        )

    anyio.run(
        _serve_http,
        args.transport,
        args.host,
        args.port,
        args.log_level,
        args.client_id,
        args.client_secret,
        args.oauth_redirect_hosts,
    )


if __name__ == "__main__":
    main()
