"""EchoPass 统一配置入口。

设计目标：
- 把可提交的默认/占位项放在 ``config/dev.yaml``，敏感项用 ``prod.yaml``（不入库）或环境变量覆盖；
- 保留所有历史 ``SPEAKER_*`` 环境变量的覆盖能力（生产/CI 用 env 一键改值）；
- 业务代码统一调 ``cfg("path.to.field", "ENV_NAME", default, cast=...)``，
  无需关心是 yaml 还是 env 取的。

加载顺序（自上而下，命中即返）：
    1. 环境变量（若存在且非空字符串）
    2. yaml 文件中对应路径的字段
    3. 调用方提供的 ``default``

切换文件：
    export ECHOPASS_CONFIG=/path/to/prod.yaml   # 默认 ``<repo>/config/dev.yaml``
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _REPO_ROOT / "config" / "dev.yaml"

logger = logging.getLogger("echopass.config")


def _resolve_path() -> Path:
    p = os.environ.get("ECHOPASS_CONFIG", "").strip()
    if not p:
        return _DEFAULT_PATH
    pp = Path(p)
    if not pp.is_absolute():
        pp = _REPO_ROOT / pp
    return pp


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        logger.warning("配置文件不存在: %s（仅使用 env + 内置默认值）", path)
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("PyYAML 未安装，无法读取 %s（请 pip install pyyaml）", path)
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("配置文件解析失败 %s: %s（仅使用 env + 内置默认值）", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("配置文件根节点不是 mapping: %s", path)
        return {}
    logger.info("配置文件已加载: %s", path)
    return data


_CFG_PATH: Path = _resolve_path()
_CFG: dict = _load_yaml(_CFG_PATH)


def _walk(d: dict, dotted: str) -> Any:
    cur: Any = d
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def to_bool(v: Any) -> bool:
    """统一的 bool 转换：兼容 yaml 原生 bool 与 env 字符串。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def cfg(
    path: str,
    env: Optional[str] = None,
    default: Any = None,
    cast: Optional[Callable[[Any], T]] = None,
    *,
    allow_empty: bool = False,
) -> Any:
    """按 env > yaml > default 的优先级返回配置值。

    Args:
        path: yaml 中的点分路径，例如 ``"llm.api_key"``。
        env: 对应的环境变量名；为 None 时跳过 env 查找。
        default: env 与 yaml 都没有时的回退值。
        cast: 可选转换函数，对最终值统一做类型转换；转换失败时返回 ``default``。
        allow_empty: 默认 False，空字符串视为未设置；置为 True 时空串本身也是合法值
            （用于 PG_DSN 这类 ``""`` = 显式禁用 的语义）。
    """
    raw: Any = None
    used_env = False
    if env:
        v = os.environ.get(env)
        if v is not None:
            stripped = v if allow_empty else v.strip()
            if allow_empty or stripped != "":
                raw = stripped
                used_env = True
    if not used_env:
        raw = _walk(_CFG, path)
        if isinstance(raw, str) and not allow_empty:
            stripped = raw.strip()
            raw = stripped if stripped != "" else None
    if raw is None or (not allow_empty and raw == ""):
        raw = default
    if cast is not None and raw is not None:
        try:
            return cast(raw)
        except (TypeError, ValueError):
            try:
                return cast(default) if default is not None else default
            except (TypeError, ValueError):
                return default
    return raw


def config_path() -> Path:
    """当前生效的配置文件路径（即使文件不存在也返回原始路径，便于排错）。"""
    return _CFG_PATH


def reload() -> None:
    """重新读取配置文件（测试用；运行中谨慎调用，业务模块通常已缓存了值）。"""
    global _CFG_PATH, _CFG
    _CFG_PATH = _resolve_path()
    _CFG = _load_yaml(_CFG_PATH)


def snapshot() -> dict:
    """返回当前 yaml 字段的浅拷贝（不含 env 覆盖；用于 /api/health 之类的诊断）。"""
    return dict(_CFG)
