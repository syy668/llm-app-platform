"""向量化：hash=本地特征哈希(免密钥) / openai=OpenAI 兼容接口。

【多服务商热更新 新增】提供方与连接参数读取优先级：
系统配置页覆盖值（runtime_config，保存即生效、无需重启） > .env 默认值。
通过系统配置页 embedding_provider 可在 hash / openai 间在线切换；
OpenAI 兼容模式下可搭配服务商预设（DeepSeek / SiliconFlow / ModelScope）切换地址与模型。
"""
import hashlib
import math

from ..config import settings
from ..core import runtime_config


def _hash_embed(text: str, dim: int) -> list[float]:
    """特征哈希：对字符 n-gram 做签名哈希，得到 dim 维向量并 L2 归一化。

    仅用于无外部 Embedding 服务时的本地测试，不具备语义相似度，
    生产环境请使用 openai 提供方（或其它 OpenAI 兼容 Embedding 接口）。
    """
    vec = [0.0] * dim
    for n in (1, 2, 3):
        for i in range(len(text) - n + 1):
            gram = text[i:i + n]
            digest = hashlib.md5(gram.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化，返回与 texts 等长的向量列表。

    提供方每次调用时读取 runtime_config 生效值：后台切换后下一次调用即用新配置。
    """
    provider = str(runtime_config.get_value("embedding_provider") or settings.embedding_provider)
    if provider == "hash":
        return [_hash_embed(t, settings.embedding_dim) for t in texts]
    if provider == "openai":
        return _embed_openai(texts)
    raise RuntimeError(f"不支持的 Embedding 提供方: {provider}")


def _embed_openai(texts: list[str]) -> list[list[float]]:
    from openai import OpenAI

    # 热更新取值：覆盖值（配置页）> .env 默认；密钥走 get_secret（敏感字段）
    api_key = runtime_config.get_secret("embedding_api_key") or settings.embedding_api_key
    if not api_key:
        raise RuntimeError(
            "Embedding 提供方为 openai 但未配置密钥：请在系统配置页填写 Embedding API Key，"
            "或在 .env 中设置 EMBEDDING_API_KEY"
        )
    api_base = str(runtime_config.get_value("embedding_api_base") or "") or settings.embedding_api_base
    client = OpenAI(
        base_url=api_base or None,
        api_key=api_key,
    )
    model = str(runtime_config.get_value("embedding_model") or "") or settings.embedding_model or "text-embedding-3-small"
    resp = client.embeddings.create(model=model, input=texts)
    # 按返回顺序取向量
    return [item.embedding for item in resp.data]
