"""Agent4Company 本地优先公司管理平台。"""

from .plugin_registry import PluginRegistry, load_registry
from .profiles import CompanyProfile, load_profile
from .runtime import RuntimeStore

__all__ = ["CompanyProfile", "PluginRegistry", "RuntimeStore", "load_profile", "load_registry"]
__version__ = "0.1.0"
