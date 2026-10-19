"""Narrow monkrel transport overrides and the historical shipping/upload patch.

Call account/public workflows inside Runtime.operation(), which holds the lock
across account discovery, refresh and every phase. No service workflows live here.
"""

from __future__ import annotations

import json
import math
import os
import stat
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape, quoteattr

import httpx
from curl_cffi import CurlMime
from curl_cffi import requests as creq
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from kleinanzeigen_api import Authenticator, KleinanzeigenAPI
from kleinanzeigen_api.client import ADS_FIELD_SELECTOR, API_HOST, GATEWAY_HOST

from .config import Settings
from .models import DomainError
from .transport import PacedSession, RequestBudget, SafeResponse, Session

_PICTURE_NS = "{http://www.ebayclassifiedsgroup.com/schema/picture/v1}picture"
# Keep the library's selector and request the raw enrichments it does not retain.
READ_FIELD_SELECTOR = ADS_FIELD_SELECTOR + ",shipping-options,contact-name,store-title"


def object_json(response: SafeResponse) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise DomainError("UPSTREAM_SCHEMA")
    return value


class CurlSession:
    """Typed facade for curl's narrower method literal and response annotations."""

    def __init__(self) -> None:
        self.session = creq.Session(impersonate="chrome")

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("allow_redirects", False)
        limit = kwargs.pop("max_response_bytes", 8 * 1024 * 1024)
        chunks = bytearray()
        exceeded = False

        def receive(chunk: bytes) -> int:
            nonlocal exceeded
            if len(chunks) + len(chunk) > limit:
                exceeded = True
                return CURL_WRITEFUNC_ERROR
            chunks.extend(chunk)
            return len(chunk)

        try:
            response = self.session.request(
                cast(Any, method), url, content_callback=receive, **kwargs
            )
        except creq.RequestsError:
            if exceeded:
                raise DomainError("LIMIT_EXCEEDED") from None
            raise
        response.content = bytes(chunks)
        return response

    def close(self) -> None:
        self.session.close()


class HttpxSession:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = (
            client if client is not None else httpx.Client(follow_redirects=False, trust_env=False)
        )

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        limit = kwargs.pop("max_response_bytes", 8 * 1024 * 1024)
        with self.client.stream(method, url, **kwargs) as response:
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > limit:
                    raise DomainError("LIMIT_EXCEEDED")
                data.extend(chunk)
            # iter_bytes has decoded Content-Encoding already; do not decode twice.
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(response.status_code, headers=headers, content=bytes(data))

    def close(self) -> None:
        self.client.close()


class ShippingKleinanzeigenAPI(KleinanzeigenAPI):
    def __init__(
        self,
        *,
        budget: RequestBudget,
        session: Session | None = None,
        settings: Settings | None = None,
        authenticator: Any = None,
        user_id: str | None = None,
    ):
        settings = settings or Settings()
        super().__init__(
            rate_limit=budget.interval,
            timeout=settings.request_timeout,  # type: ignore[arg-type]
            max_retries=1,
            authenticator=authenticator,
            user_id=user_id,
        )
        # M constructs a session but never uses it during construction.
        self._s.close()
        self.budget = budget
        self.settings = settings
        self._s: PacedSession = PacedSession(  # type: ignore[assignment]
            session if session is not None else CurlSession(),
            budget,
            owner=self,
            account=authenticator is not None,
        )

    def _require_auth(self) -> Any:
        if self._auth_provider is None:
            raise DomainError("AUTH_REQUIRED")
        return self._auth_provider

    def _throttle(self) -> None:
        # Same completion-based interval+jitter as M, using the shared clock.
        # Both hooks and session consult the same ready time: no doubled interval.
        with self.budget.operation():
            self.budget.pace()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> SafeResponse:  # type: ignore[override]
        with self.budget.operation():
            return self._s.get(
                url, params=params, headers=self._headers(), timeout=self.timeout, account=False
            )

    def _request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: str | None = None,
        content_type: str | None = None,
        authed: bool = True,
        gateway: bool = False,
    ) -> SafeResponse:
        with self.budget.operation():
            headers = self._headers(authed=authed, gateway=gateway)
            if content_type:
                headers["Content-Type"] = content_type
            body = data.encode("utf-8") if isinstance(data, str) else data
            return self._s.request(
                method,
                url,
                params=params,
                json=json,
                data=body,
                headers=headers,
                timeout=self.timeout,
                account=authed,
            )

    def _fetch_ad_json(self, ad_id: str) -> dict[str, Any] | None:
        try:
            return object_json(self._get(f"{API_HOST}/api/ads/{ad_id}.json"))
        except DomainError as exc:
            if exc.detail.code == "NOT_FOUND":
                return None
            raise

    def get_ad(self, ad_id: str) -> Any:
        data = self._fetch_ad_json(ad_id)
        if data is None:
            raise DomainError("NOT_FOUND")
        return self._parse_ad(self._unwrap_ad(data))

    def resolve_location(self, query: str) -> list[Any]:
        # Deliberately omit M's silent website fallback.
        with self.budget.operation():
            return list(self._resolve_location_api(query))

    def search_page_extended(
        self,
        *,
        query: str | None = None,
        category_id: str | None = None,
        location_id: str | None = None,
        zip_code: str | None = None,
        distance_km: float | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        sort_type: str | None = None,
        ad_type: str | None = "OFFERED",
        poster_type: str | None = None,
        attributes: dict[str, str | list[str]] | None = None,
        picture_required: bool = False,
        shippable: bool = False,
        buy_now_only: bool = False,
        include_top_ads: bool = False,
        page: int = 0,
        page_size: int = 25,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "_in": READ_FIELD_SELECTOR,
            "page": page,
            "size": page_size,
            "limitTotalResultCount": "true",
        }
        for key, value in {
            "q": query,
            "categoryId": category_id,
            "locationId": location_id,
            "zipcode": zip_code,
            "distance": distance_km,
            "minPrice": min_price,
            "maxPrice": max_price,
            "sortType": sort_type,
            "adType": ad_type,
            "posterType": poster_type,
        }.items():
            if value is not None:
                params[key] = value
        if distance_km is not None:
            params["distanceUnit"] = "KM"
        if min_price is not None or max_price is not None:
            params["priceCurrency"] = "EUR"
        for key, value in {
            "pictureRequired": picture_required,
            "shippable": shippable,
            "buyNowOnly": buy_now_only,
            "includeTopAds": include_top_ads,
        }.items():
            params[key] = str(value).lower()
        for key, attr_value in (attributes or {}).items():
            # The service validates multi-select against metadata before this hook.
            params[f"attr[{key}]"] = (
                ",".join(attr_value) if isinstance(attr_value, list) else attr_value
            )
        return object_json(self._get(f"{API_HOST}/api/ads.json", params=params))

    def category_filters_raw(self, category_id: str) -> dict[str, Any]:
        return object_json(self._get(f"{API_HOST}/api/ads/search-metadata/{category_id}.json"))

    def locations_raw(self, query: str) -> dict[str, Any]:
        return object_json(
            self._get(f"{API_HOST}/api/locations.json", params={"depth": 1, "q": query})
        )

    def my_ads_raw(self, page: int = 0, size: int = 25, q: str | None = None) -> dict[str, Any]:
        with self.budget.operation():
            params = {"_in": READ_FIELD_SELECTOR, "page": page, "size": size}
            if q is not None:
                params["q"] = q
            return object_json(
                self._request("GET", f"{API_HOST}/api/users/{self.user_id}/ads.json", params=params)
            )

    def get_my_ad_raw(self, ad_id: str) -> dict[str, Any]:
        with self.budget.operation():
            return object_json(
                self._request("GET", f"{API_HOST}/api/users/{self.user_id}/ads/{ad_id}.json")
            )

    def watchlist_raw(self, page: int = 0, size: int = 25) -> dict[str, Any]:
        with self.budget.operation():
            return object_json(
                self._request(
                    "GET",
                    f"{API_HOST}/api/users/{self.user_id}/watchlist.json",
                    params={"_in": READ_FIELD_SELECTOR, "page": page, "size": size},
                )
            )

    def conversations_raw(self, page: int = 0, size: int = 100) -> dict[str, Any]:
        # M conversations() coerces unavailable unread fields to false/zero.
        # Retain its exact request and normalize the raw response in the service.
        return object_json(
            self._request(
                "GET",
                f"{GATEWAY_HOST}/messagebox/api/users/{self.user_id}/conversations",
                params={"page": page, "size": size},
                gateway=True,
            )
        )

    def post_ad(self, *, shipping_options: list[str] | None = None, **kwargs: Any) -> str:
        with self.budget.operation():
            self._posting_shipping_options = shipping_options or []
            try:
                return str(super().post_ad(**kwargs))
            finally:
                del self._posting_shipping_options

    def update_ad(
        self,
        ad_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        price: Decimal | None = None,
        price_type: str | None = None,
        picture_urls: list[str] | None = None,
    ) -> None:
        """Reconstruct the ASSERTED selective XML PUT; live verification is pending.

        Omitted fields never enter this payload. Server-side retention and empty
        pictures semantics remain unverified; no replacement/fallback is attempted.
        """
        namespaces = " ".join(
            f'xmlns:{p}="http://www.ebayclassifiedsgroup.com/schema/{self._AD_NAMESPACES[p]}"'
            for p in ("ad", "types", "pic")
        )
        parts = ["<?xml version='1.0' encoding='UTF-8' standalone='yes'?>", f"<ad:ad {namespaces}>"]
        for tag, value in (("title", title), ("description", description)):
            if value is not None:
                parts.append(f"<ad:{tag}>{escape(value)}</ad:{tag}>")
        if price is not None or price_type is not None:
            parts.append("<ad:price>")
            if price_type is not None:
                pt = self._PRICE_TYPE_MAP[price_type]
                parts.append(
                    f"<types:price-type><types:value>{pt}</types:value></types:price-type>"
                )
            if price is not None and price_type != "FREE":
                parts.append(f"<types:amount>{format(price, 'f')}</types:amount>")
            parts.append("</ad:price>")
        if picture_urls is not None:
            parts.append("<pic:pictures>")
            for url in picture_urls:
                parts.append(
                    f'<pic:picture><pic:link rel="XXL" href={quoteattr(url)}/></pic:picture>'
                )
            parts.append("</pic:pictures>")
        parts.append("</ad:ad>")
        with self.budget.operation():
            self._request(
                "PUT",
                f"{API_HOST}/api/users/{self.user_id}/ads/{ad_id}.json",
                data="".join(parts),
                content_type="application/xml",
                authed=True,
            )

    def _build_ad_xml(self, *, shipping_options: list[str] | None = None, **kwargs: Any) -> str:  # type: ignore[override]
        xml: str = super()._build_ad_xml(**kwargs)
        options = (
            shipping_options
            if shipping_options is not None
            else getattr(self, "_posting_shipping_options", [])
        )
        if options:
            packages = "".join(
                f"<shipping:shipping-option id={quoteattr(option)}/>" for option in options
            )
            xml = xml.replace(
                "</ad:ad>",
                f"<shipping:shipping-options>{packages}</shipping:shipping-options></ad:ad>",
            )
        return xml

    def upload_picture(
        self, image_path: str, filename: str | None = None, content_type: str | None = None
    ) -> str:
        path, name, ctype = self._validate_image(image_path, filename, content_type)
        with self.budget.operation():
            mime = CurlMime()
            try:
                mime.addpart(name="file", content_type=ctype, filename=name, local_path=str(path))
                response = self._s.post(
                    f"{API_HOST}/api/pictures",
                    multipart=mime,
                    headers=self._headers(authed=True),
                    timeout=self.timeout,
                    account=True,
                    accepted=(200, 201),
                )
            finally:
                mime.close()
            body = response.json()
            wrap = body.get(_PICTURE_NS) if isinstance(body, dict) else None
            value = wrap.get("value") if isinstance(wrap, dict) else None
            links = (value.get("link") or {}) if isinstance(value, dict) else {}
            if isinstance(links, dict):
                links = [links]
            if not isinstance(links, list):
                links = []
            by_rel = {
                link.get("rel"): link.get("href")
                for link in links
                if isinstance(link, dict)
                and (link.get("rel") is None or isinstance(link.get("rel"), str))
                and isinstance(link.get("href"), str)
                and link.get("href")
            }
            url = by_rel.get("XXL") or by_rel.get("detail") or next(iter(by_rel.values()), None)
            if not url and isinstance(body, dict):
                url = body.get("url")
            if not isinstance(url, str) or not url:
                raise DomainError(
                    "PARTIAL_WRITE", context={"phase": "picture_uploaded_url_unresolved"}
                )
            return url

    def _validate_image(
        self, image_path: str, filename: str | None, content_type: str | None
    ) -> tuple[Path, str, str]:
        try:
            path = Path(image_path).expanduser().resolve()
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise DomainError("INVALID_ARGUMENT")
            if self.settings.upload_roots and not any(
                path.is_relative_to(root) for root in self.settings.upload_roots
            ):
                raise DomainError("INVALID_ARGUMENT")
            with path.open("rb") as f:
                head = f.read(16)
        except (OSError, ValueError):
            raise DomainError(
                "INVALID_ARGUMENT",
                hint="Choose a readable regular image within the configured upload roots.",
            ) from None
        ctype = ""
        if head.startswith(b"\xff\xd8\xff"):
            ctype = "image/jpeg"
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            ctype = "image/png"
        elif head.startswith((b"GIF87a", b"GIF89a")):
            ctype = "image/gif"
        elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            ctype = "image/webp"
        name = filename if filename is not None else path.name
        if not ctype or (content_type is not None and content_type != ctype):
            raise DomainError(
                "INVALID_ARGUMENT", hint="Use JPEG, PNG, WebP or GIF with a matching MIME type."
            )
        if (
            not name
            or len(name) > 255
            or any(unicodedata.category(c) == "Cc" or c in "/\\" for c in name)
        ):
            raise DomainError("INVALID_ARGUMENT")
        return path, name, ctype

    def close(self) -> None:
        self._s.close()


class SellerClient:
    def __init__(
        self, budget: RequestBudget, *, headers: dict[str, str], client: httpx.Client | None = None
    ):
        self.headers = {
            k: v
            for k, v in headers.items()
            if k.lower() not in {"x-ebayk-userid-token", "x-ecg-authorization-user", "cookie"}
        }
        if not self.headers.get("Authorization", "").startswith("Basic "):
            self.headers.pop("Authorization", None)
        self.session = PacedSession(
            HttpxSession(client),
            budget,
        )

    def profile(self, seller_id: str) -> dict[str, Any]:
        return object_json(
            self.session.get(
                f"{API_HOST}/api/users/public/{seller_id}/profile.json",
                headers=self.headers,
                profile=True,
            )
        )

    def reputation(self, seller_id: str) -> dict[str, Any]:
        return object_json(
            self.session.get(
                f"{GATEWAY_HOST}/user-reputation-service/public/users/{seller_id}/reputation-summary",
                headers=self.headers,
            )
        )

    def close(self) -> None:
        self.session.close()


class Runtime:
    """Lazy process-local clients. All service phases belong inside operation()."""

    def __init__(
        self,
        settings: Settings,
        *,
        budget: RequestBudget | None = None,
        session_factory: Callable[[], Session] | None = None,
    ):
        self.settings = settings
        self.budget = budget or RequestBudget(
            interval=settings.request_interval,
            request_timeout=settings.request_timeout,
            operation_timeout=settings.operation_timeout,
        )
        self.session_factory = session_factory or CurlSession
        self._public: ShippingKleinanzeigenAPI | None = None
        self._account: ShippingKleinanzeigenAPI | None = None
        self._auth: Any = None
        self._auth_session: PacedSession | None = None
        self._seller: SellerClient | None = None
        self._failed_signature: tuple[int, int, int, int] | None = None
        self._auth_failed = False
        self._auth_cooldown = 0.0

    def public(self) -> ShippingKleinanzeigenAPI:
        with self.budget.operation():
            if self._public is None:
                self._public = ShippingKleinanzeigenAPI(
                    budget=self.budget, settings=self.settings, session=self.session_factory()
                )
            return self._public

    def _signature(self) -> tuple[int, int, int, int] | None:
        try:
            s = self.settings.token_path.lstat()
            return s.st_ino, s.st_mtime_ns, s.st_ctime_ns, s.st_size
        except FileNotFoundError:
            return None
        except OSError:
            raise DomainError("AUTH_STORAGE_ERROR") from None

    def _check_storage(self) -> None:
        path = self.settings.token_path
        try:
            if not path.exists() and not path.is_symlink():
                return
            s = path.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid():
                raise DomainError("AUTH_STORAGE_ERROR")
            if stat.S_IMODE(s.st_mode) != 0o600:
                path.chmod(0o600)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise DomainError("AUTH_STORAGE_ERROR")
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise DomainError("AUTH_STORAGE_ERROR")
            for field in ("refresh_token", "access_token"):
                if field in value and (not isinstance(value[field], str) or not value[field]):
                    raise DomainError("AUTH_STORAGE_ERROR")
            if "expires_at" in value:
                expiry = value["expires_at"]
                if (
                    isinstance(expiry, bool)
                    or not isinstance(expiry, (int, float))
                    or not math.isfinite(expiry)
                ):
                    raise DomainError("AUTH_STORAGE_ERROR")
            if value.get("email") is not None and not isinstance(value["email"], str):
                raise DomainError("AUTH_STORAGE_ERROR")
        except (OSError, ValueError, UnicodeError):
            raise DomainError("AUTH_STORAGE_ERROR") from None

    def account(self) -> ShippingKleinanzeigenAPI:
        with self.budget.operation():
            if self._auth_failed:
                if self._signature() == self._failed_signature:
                    raise DomainError("AUTH_EXPIRED")
                self._discard_account()
                self._auth_failed = False
            if self.budget.clock() < self._auth_cooldown:
                raise DomainError("UPSTREAM_UNAVAILABLE", retryable=True)
            if self._account is None:
                self._check_storage()
                self._auth_session = PacedSession(
                    self.session_factory(), self.budget, auth_exchange=True
                )
                self._auth = Authenticator(
                    token_path=str(self.settings.token_path), session=cast(Any, self._auth_session)
                )
                if not self._auth.logged_in:
                    self._discard_account()
                    raise DomainError("AUTH_REQUIRED")
                self._account = ShippingKleinanzeigenAPI(
                    budget=self.budget,
                    session=self.session_factory(),
                    settings=self.settings,
                    authenticator=self._auth,
                    user_id=self.settings.user_id,
                )
            return self._account

    @contextmanager
    def operation(
        self, *, account: bool = False, tool_name: str = "upstream"
    ) -> Iterator[ShippingKleinanzeigenAPI]:
        with self.budget.operation(tool_name=tool_name):
            try:
                yield self.account() if account else self.public()
            except DomainError as exc:
                if account and exc.detail.code == "AUTH_EXPIRED":
                    self._auth_failed = True
                    self._failed_signature = self._signature()
                elif account and exc.detail.code == "UPSTREAM_UNAVAILABLE":
                    self._auth_cooldown = self.budget.clock() + 5
                raise
            except (KeyError, ValueError, TypeError):
                raise DomainError("UPSTREAM_SCHEMA") from None
            except OSError:
                raise DomainError(
                    "AUTH_STORAGE_ERROR" if account else "UPSTREAM_UNAVAILABLE"
                ) from None

    def seller(self) -> SellerClient:
        with self.budget.operation():
            if self._seller is None:
                self._seller = SellerClient(self.budget, headers=self.public()._headers())
            return self._seller

    def cdn_get(self, url: str, *, request_slots: list[None] | None = None) -> SafeResponse:
        with self.budget.operation():
            for _ in range(4):
                try:
                    p = urlsplit(url)
                    port = p.port
                except ValueError:
                    raise DomainError("INVALID_ARGUMENT", hint="Invalid image CDN URL.") from None
                if (
                    p.scheme != "https"
                    or p.hostname != "img.kleinanzeigen.de"
                    or p.username
                    or p.password
                    or port not in (None, 443)
                    or len(url) > 2048
                    or any(ord(c) < 33 or ord(c) == 127 for c in url)
                ):
                    raise DomainError(
                        "INVALID_ARGUMENT", hint="Image URL must use the known HTTPS CDN."
                    )
                if request_slots is not None:
                    if not request_slots:
                        raise DomainError(
                            "LIMIT_EXCEEDED", hint="Image redirect request cap reached."
                        )
                    request_slots.pop()
                response = self.public()._s.get(
                    url,
                    headers={"Accept": "image/*"},
                    account=False,
                    allow_redirects=False,
                    accepted=(200, 301, 302, 303, 307, 308),
                    max_response_bytes=5 * 1024 * 1024,
                )
                if response.status_code == 200:
                    return response
                target = response.headers.get("Location") or response.headers.get("location")
                if not target:
                    raise DomainError("UPSTREAM_SCHEMA")
                url = urljoin(url, target)
            raise DomainError("UPSTREAM_UNAVAILABLE", hint="Image redirect limit reached.")

    def _discard_account(self) -> None:
        if self._account is not None:
            self._account.close()
        if self._auth_session is not None:
            self._auth_session.close()
        self._auth, self._account, self._auth_session = None, None, None

    def close(self) -> None:
        with self.budget.operation(check_cooldown=False):
            self._discard_account()
            if self._public is not None:
                self._public.close()
                self._public = None
            if self._seller is not None:
                self._seller.close()
                self._seller = None
