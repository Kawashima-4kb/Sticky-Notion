"""通信の共通設定。

- 証明書は truststore で Windows の証明書ストアを使って検証する
  （Python 3.13+ の厳格な検証では通らない証明書がある環境向け）。
- mcp.notion.com は既定の User-Agent（Python-urllib）を Cloudflare で拒否するため、独自の値を付ける。
"""

from notiontask import APP_NAME, __version__

USER_AGENT = f"{APP_NAME}/{__version__}"

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - 依存が無い環境では標準の検証を使う
    pass
